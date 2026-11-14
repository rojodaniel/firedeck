#!/usr/bin/env python3
"""
FireDeck - control de Fire TV por ADB inalámbrico desde el navegador.

Sin dependencias externas: solo la biblioteca estándar de Python 3.9+.
Uso:  python app.py [--host 127.0.0.1] [--port 8080]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import html
import http.client
import io
import ipaddress
import json
import mimetypes
import os
import platform
import re
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import urllib.request
import zipfile
import zlib
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
LOCAL_TOOLS = ROOT / "platform-tools"
IS_WINDOWS = sys.platform.startswith("win")

ADB_LOCK = threading.RLock()
STATE = {"serial": None}
START_TIME = time.time()
DEVICES_FILE = ROOT / "devices.json"
DEVICES_LOCK = threading.RLock()
SERIAL_PATTERN = re.compile(r"^[A-Za-z0-9._:\[\]-]{1,120}$")

# Cada pestaña del navegador controla su propio Fire TV. El cliente manda un
# identificador estable en la cabecera X-FireDeck-Tab (o en ?tab=), y el
# servidor guarda la selección de esa pestaña sin tocar la de las demás.
TAB_HEADER = "X-FireDeck-Tab"
TAB_PATTERN = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
TAB_TTL = 6 * 3600
# Una pestaña cuenta como «abierta» si ha preguntado en este margen. Así las
# pestañas cerradas desaparecen de la lista sin esperar a que caduque su selección.
TAB_LIVE = 90
TABS: dict[str, dict] = {}
TABS_LOCK = threading.RLock()
REQUEST_TAB = threading.local()


# --------------------------------------------------------------------------
# Localizar / instalar adb
# --------------------------------------------------------------------------

def _adb_name() -> str:
    return "adb.exe" if IS_WINDOWS else "adb"


def find_adb() -> str | None:
    env = os.environ.get("ADB_PATH")
    if env and Path(env).is_file():
        return env
    local = LOCAL_TOOLS / "platform-tools" / _adb_name()
    if local.is_file():
        return str(local)
    return shutil.which("adb")


def download_platform_tools() -> str:
    """Descarga platform-tools oficiales si no hay adb en el sistema."""
    system = platform.system().lower()
    if system == "windows":
        url = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip"
    elif system == "darwin":
        url = "https://dl.google.com/android/repository/platform-tools-latest-darwin.zip"
    else:
        url = "https://dl.google.com/android/repository/platform-tools-latest-linux.zip"

    LOCAL_TOOLS.mkdir(parents=True, exist_ok=True)
    archive = LOCAL_TOOLS / "platform-tools.zip"
    print(f"[FireDeck] adb no encontrado. Descargando {url} ...")
    urllib.request.urlretrieve(url, archive)
    with zipfile.ZipFile(archive) as zf:
        zf.extractall(LOCAL_TOOLS)
    archive.unlink(missing_ok=True)
    target = LOCAL_TOOLS / "platform-tools" / _adb_name()
    if not target.is_file():
        raise RuntimeError("La descarga de platform-tools no produjo un adb válido.")
    if not IS_WINDOWS:
        target.chmod(0o755)
    print(f"[FireDeck] adb instalado en {target}")
    return str(target)


ADB = find_adb() or "adb"


def adb_key_dir() -> Path:
    """Directorio donde adb guarda su par de claves RSA."""
    custom = os.environ.get("ANDROID_USER_HOME")
    return Path(custom) if custom else Path.home() / ".android"


def adb_key_info() -> dict:
    keydir = adb_key_dir()
    files = []
    for name in ("adbkey", "adbkey.pub"):
        path = keydir / name
        exists = path.is_file()
        files.append(
            {
                "path": str(path),
                "exists": exists,
                "mtime": int(path.stat().st_mtime) if exists else None,
            }
        )
    return {"dir": str(keydir), "files": files}


# --------------------------------------------------------------------------
# Wrapper de ADB
# --------------------------------------------------------------------------

class AdbError(RuntimeError):
    hint: str | None = None


class AuthError(AdbError):
    """Hay conexión con el Fire TV, pero no confía en la clave de este PC."""

    hint = "auth"


def _decode(data: bytes) -> str:
    return data.decode("utf-8", "replace").strip()


def log_unexpected(context: str, exc: BaseException) -> None:
    """Deja en la consola el traceback completo de un fallo que no esperábamos.

    Los puntos que capturan ``Exception`` (hilos, peticiones, apagado) no deben
    tumbar el servidor, pero tampoco ocultar dónde falló.
    """
    detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    print(f"[FireDeck] {context}: {exc!r}\n{detail}", file=sys.stderr, flush=True)


def adb_run(args: list[str], timeout: int = 40) -> tuple[int, str, str]:
    global ADB
    try:
        with ADB_LOCK:
            proc = subprocess.run(
                [ADB, *args],
                capture_output=True,
                timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
            )
    except FileNotFoundError as exc:
        raise AdbError(
            "No se encontró el ejecutable adb. Instala Android Platform Tools "
            "o define la variable ADB_PATH."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise AdbError(f"adb tardó demasiado en responder ({timeout}s).") from exc
    return proc.returncode, _decode(proc.stdout), _decode(proc.stderr)


def adb_exec_out(args: list[str], timeout: int = 40) -> bytes:
    try:
        with ADB_LOCK:
            proc = subprocess.run(
                [ADB, *args],
                capture_output=True,
                timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
            )
    except FileNotFoundError as exc:
        raise AdbError("No se encontró adb.") from exc
    except subprocess.TimeoutExpired as exc:
        raise AdbError("La captura de pantalla tardó demasiado.") from exc
    if proc.returncode != 0:
        raise AdbError(_decode(proc.stderr) or "adb exec-out falló.")
    return proc.stdout


def list_devices() -> list[dict]:
    rc, out, err = adb_run(["devices", "-l"])
    if rc != 0:
        raise AdbError(err or "adb devices falló.")
    lines = out.splitlines()
    start = 0
    for index, line in enumerate(lines):
        if line.strip().startswith("List of devices attached"):
            start = index + 1
            break
    devices: list[dict] = []
    for line in lines[start:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        serial, state = parts[0], parts[1]
        if state not in ("device", "unauthorized", "offline", "recovery", "sideload", "bootloader"):
            continue
        model = ""
        for part in parts[2:]:
            if part.startswith("model:"):
                model = part[6:].replace("_", " ")
            elif part.startswith("device:"):
                model = model or part[7:].replace("_", " ")
        devices.append({"serial": serial, "state": state, "model": model or serial})
    return devices


# --------------------------------------------------------------------------
# Dispositivos guardados (nombre propio por Fire TV)
# --------------------------------------------------------------------------

def valid_serial(serial: str) -> bool:
    return bool(SERIAL_PATTERN.fullmatch(serial or ""))


def serial_host_port(serial: str) -> tuple[str, int]:
    host, sep, port = serial.rpartition(":")
    if sep and port.isdigit():
        return host, int(port)
    return serial, 5555


def clean_device_name(value) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 60:
        raise AdbError("El nombre del dispositivo no puede pasar de 60 caracteres.")
    return text


def _read_devices_payload() -> dict:
    try:
        raw = DEVICES_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"version": 1, "devices": []}
    except OSError as exc:
        print(f"[FireDeck] No pude leer {DEVICES_FILE.name}: {exc}")
        return {"version": 1, "devices": []}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(f"[FireDeck] {DEVICES_FILE.name} no es JSON válido; se ignora.")
        return {"version": 1, "devices": []}
    if isinstance(data, list):
        return {"version": 1, "devices": data}
    if not isinstance(data, dict):
        return {"version": 1, "devices": []}
    if not isinstance(data.get("devices"), list):
        data["devices"] = []
    return data


def load_saved_devices() -> list[dict]:
    items = _read_devices_payload().get("devices") or []
    saved: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        serial = str(item.get("serial", "")).strip()
        if not valid_serial(serial):
            continue
        try:
            last = int(item.get("last_connected") or 0)
        except (TypeError, ValueError):
            last = 0
        saved.append(
            {
                "serial": serial,
                "name": str(item.get("name") or ""),
                "model": str(item.get("model") or ""),
                "last_connected": last,
                "hardware": str(item.get("hardware") or ""),
                "last_ip": str(item.get("last_ip") or ""),
                "previous_ip": str(item.get("previous_ip") or ""),
            }
        )
    return saved


def _write_devices_payload(payload: dict):
    tmp = DEVICES_FILE.with_name(DEVICES_FILE.name + ".tmp")
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        tmp.write_text(text, encoding="utf-8")
        try:
            tmp.replace(DEVICES_FILE)
        except PermissionError:
            # OneDrive puede tener el archivo como marcador en la nube y rechazar
            # el reemplazo atómico; escribir directamente sí funciona.
            DEVICES_FILE.write_text(text, encoding="utf-8")
            tmp.unlink(missing_ok=True)
    except OSError as exc:
        raise AdbError(f"No pude guardar la lista de dispositivos: {exc}") from exc


def _write_saved_devices(items: list[dict], last_selected: str | None = None):
    payload = _read_devices_payload()
    payload["version"] = 1
    payload["devices"] = items
    if last_selected is not None:
        payload["last_selected"] = last_selected
    _write_devices_payload(payload)


def saved_selection() -> str:
    serial = str(_read_devices_payload().get("last_selected") or "")
    return serial if valid_serial(serial) else ""


def device_model(serial: str, devices: list[dict] | None = None) -> str:
    try:
        devices = devices if devices is not None else list_devices()
    except AdbError:
        return ""
    for device in devices:
        if device["serial"] == serial:
            return device.get("model") or ""
    return ""


def device_hardware_id(serial: str) -> str:
    """Número de serie del equipo (`ro.serialno`), o vacío si no se puede leer."""
    try:
        rc, out, err = adb_run(["-s", serial, "shell", "getprop ro.serialno"], timeout=6)
    except AdbError:
        return ""
    if rc != 0:
        return ""
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    value = lines[0] if lines else ""
    # Un valor con espacios o vacío no es un serie válido (p. ej. un mensaje de error).
    return value if value and " " not in value and len(value) <= 64 else ""


def remember_device(
    serial: str,
    name: str | None = None,
    model: str | None = None,
    selected: bool = False,
) -> dict | None:
    """Guarda el dispositivo y, si se indica, su nombre propio."""
    serial = str(serial or "").strip()
    if not valid_serial(serial):
        return None
    # La huella (número de serie del equipo) permite reconocerlo si cambia de IP.
    # Se consulta una sola vez y fuera del candado, porque adb puede tardar.
    hardware = ""
    known = next((item for item in load_saved_devices() if item["serial"] == serial), None)
    if not (known or {}).get("hardware"):
        hardware = device_hardware_id(serial)
    with DEVICES_LOCK:
        items = load_saved_devices()
        entry = next((item for item in items if item["serial"] == serial), None)
        host, _ = serial_host_port(serial)
        if entry is None:
            entry = {
                "serial": serial,
                "name": clean_device_name(name) if name is not None else "",
                "model": str(model or ""),
                "last_connected": int(time.time()),
                "hardware": hardware,
                "last_ip": host,
                "previous_ip": "",
            }
            items.append(entry)
        else:
            if name is not None:
                entry["name"] = clean_device_name(name)
            if model:
                entry["model"] = str(model)
            if hardware and not entry.get("hardware"):
                entry["hardware"] = hardware
            entry["last_connected"] = int(time.time())
            entry["last_ip"] = host
        try:
            _write_saved_devices(items, last_selected=serial if selected else None)
        except AdbError as exc:
            # Guardar es una comodidad: no debe romper una conexión ya establecida.
            print(f"[FireDeck] {exc}")
        return dict(entry)


def rename_saved_device(serial: str, name: str) -> dict:
    serial = str(serial or "").strip()
    clean = clean_device_name(name)
    with DEVICES_LOCK:
        items = load_saved_devices()
        entry = next((item for item in items if item["serial"] == serial), None)
        if entry is None:
            raise AdbError("Ese dispositivo no está en la lista guardada.")
        entry["name"] = clean
        _write_saved_devices(items)
        return dict(entry)


def forget_saved_device(serial: str) -> bool:
    serial = str(serial or "").strip()
    with DEVICES_LOCK:
        items = load_saved_devices()
        kept = [item for item in items if item["serial"] != serial]
        if len(kept) == len(items):
            return False
        _write_saved_devices(kept)
        return True


def remember_device_network(serial: str, hardware: str | None = None, ip: str | None = None):
    """Anota la huella del equipo y la última IP vista, sin tocar su nombre."""
    serial = str(serial or "").strip()
    if not valid_serial(serial):
        return
    host = str(ip or serial_host_port(serial)[0] or "")
    with DEVICES_LOCK:
        items = load_saved_devices()
        entry = next((item for item in items if item["serial"] == serial), None)
        if entry is None:
            return
        changed = False
        if hardware and entry.get("hardware") != hardware:
            entry["hardware"] = str(hardware)
            changed = True
        if host and entry.get("last_ip") != host:
            entry["last_ip"] = host
            changed = True
        if not changed:
            return
        try:
            _write_saved_devices(items)
        except AdbError as exc:
            print(f"[FireDeck] {exc}")


def move_saved_device(
    old_serial: str, new_serial: str, hardware: str = "", model: str = ""
) -> dict:
    """Un Fire TV guardado cambió de IP: conserva nombre y datos en la dirección nueva."""
    old_serial = str(old_serial or "").strip()
    new_serial = str(new_serial or "").strip()
    if not valid_serial(new_serial):
        raise AdbError("La nueva dirección del Fire TV no es válida.")
    with DEVICES_LOCK:
        payload = _read_devices_payload()
        items = [item for item in (payload.get("devices") or []) if isinstance(item, dict)]
        entry = next(
            (item for item in items if str(item.get("serial", "")).strip() == old_serial),
            None,
        )
        if entry is None:
            raise AdbError("El dispositivo que cambió de IP ya no está en la lista guardada.")
        duplicate = next(
            (
                item
                for item in items
                if item is not entry and str(item.get("serial", "")).strip() == new_serial
            ),
            None,
        )
        if duplicate is not None:
            # Si la IP nueva ya se había guardado sin nombre, heredamos el que sí lo tiene.
            if not str(entry.get("name") or "").strip() and str(duplicate.get("name") or "").strip():
                entry["name"] = duplicate["name"]
            if not str(entry.get("model") or "").strip() and str(duplicate.get("model") or "").strip():
                entry["model"] = duplicate["model"]
            items.remove(duplicate)
        old_host, _ = serial_host_port(old_serial)
        entry["serial"] = new_serial
        if hardware:
            entry["hardware"] = str(hardware)
        if model:
            entry["model"] = str(model)
        entry["last_ip"] = serial_host_port(new_serial)[0]
        entry["previous_ip"] = old_host
        entry["last_connected"] = int(time.time())
        payload["version"] = 1
        payload["devices"] = items
        if str(payload.get("last_selected") or "") == old_serial:
            payload["last_selected"] = new_serial
        _write_devices_payload(payload)
        # El registro de apps desactivadas se indexa por IP:puerto: sin esto,
        # «Restaurar desactivadas» dejaría de encontrar lo desactivado.
        move_debloat_records(old_serial, new_serial)
        return {
            "serial": new_serial,
            "name": str(entry.get("name") or ""),
            "model": str(entry.get("model") or ""),
            "previous": old_serial,
            "previous_ip": old_host,
            "hardware": str(entry.get("hardware") or ""),
        }


def saved_device_records(devices: list[dict] | None = None) -> list[dict]:
    """Lista guardada enriquecida con el estado actual de adb."""
    if devices is None:
        try:
            devices = list_devices()
        except AdbError:
            devices = []
    online = {device["serial"]: device for device in devices}
    records: list[dict] = []
    for item in load_saved_devices():
        serial = item["serial"]
        live = online.get(serial) or {}
        alias = item.get("name") or ""
        model = live.get("model") or item.get("model") or ""
        host, port = serial_host_port(serial)
        records.append(
            {
                **item,
                "alias": alias,
                "display_name": alias or model or serial,
                "model": model,
                "state": live.get("state") or "offline",
                "online": live.get("state") == "device",
                "host": host,
                "port": port,
            }
        )
    records.sort(key=lambda item: (not item["online"], item["display_name"].lower()))
    return records


def _new_tab_entry(serial: str = "") -> dict:
    with TABS_LOCK:
        seq = max([entry.get("seq", 0) for entry in TABS.values()] or [0]) + 1
    return {
        "serial": str(serial or ""),
        "explicit": False,
        "seen": time.time(),
        "seq": seq,
    }


def _prune_tabs_locked() -> None:
    cutoff = time.time() - TAB_TTL
    for key in [key for key, entry in TABS.items() if entry.get("seen", 0) < cutoff]:
        TABS.pop(key, None)


def current_tab_id() -> str:
    """Identificador de la pestaña que atiende este hilo (vacío si no hay)."""
    return str(getattr(REQUEST_TAB, "id", "") or "")


def bind_tab(tab: str) -> None:
    """Asocia la petición actual a la pestaña `tab` y la registra."""
    tab = str(tab or "").strip()
    if not tab or not TAB_PATTERN.match(tab):
        REQUEST_TAB.id = ""
        return
    REQUEST_TAB.id = tab
    with TABS_LOCK:
        _prune_tabs_locked()
        if len(TABS) > 32:
            oldest = min(TABS.items(), key=lambda item: item[1].get("seen", 0))[0]
            TABS.pop(oldest, None)
        entry = TABS.get(tab)
        if entry is None:
            entry = _new_tab_entry(STATE.get("serial") or saved_selection() or "")
            TABS[tab] = entry
        entry["seen"] = time.time()


def current_selection() -> str:
    """Fire TV elegido en esta pestaña; si no hay, el último global."""
    tab = current_tab_id()
    if tab:
        with TABS_LOCK:
            serial = str((TABS.get(tab) or {}).get("serial") or "")
        if valid_serial(serial):
            return serial
    serial = str(STATE.get("serial") or "").strip()
    if valid_serial(serial):
        return serial
    fallback = saved_selection()
    return fallback if valid_serial(fallback) else ""


def set_selected(serial: str) -> None:
    """Cambia el dispositivo de esta pestaña y recuerda el último global."""
    STATE["serial"] = serial
    tab = current_tab_id()
    if not tab:
        return
    with TABS_LOCK:
        entry = TABS.get(tab)
        if entry is None:
            entry = _new_tab_entry(serial)
            TABS[tab] = entry
        entry["serial"] = serial
        entry["explicit"] = True
        entry["seen"] = time.time()


def tab_summary(aliases: dict[str, str] | None = None) -> list[dict]:
    """Pestañas abiertas, para que la interfaz muestre cuál controla qué."""
    me = current_tab_id()
    aliases = aliases or {}
    cutoff = time.time() - TAB_LIVE
    with TABS_LOCK:
        rows = [
            {
                "id": key,
                "seq": int(entry.get("seq") or 0),
                "serial": str(entry.get("serial") or ""),
                "self": key == me,
            }
            for key, entry in TABS.items()
            if key == me or entry.get("seen", 0) >= cutoff
        ]
    for row in rows:
        row["label"] = f"Pestaña {row['seq']}" if row["seq"] else "Pestaña"
        row["name"] = aliases.get(row["serial"]) or row["serial"]
    rows.sort(key=lambda item: item["seq"])
    return rows


def pick_serial() -> str:
    devices = list_devices()
    online = [d for d in devices if d["state"] == "device"]
    if not online:
        unauthorized = [d for d in devices if d["state"] == "unauthorized"]
        if unauthorized:
            raise AdbError(
                "El Fire TV está conectado pero no autorizado. Acepta el aviso "
                "de depuración USB que aparece en la pantalla del televisor."
            )
        raise AdbError(
            "No hay ningún Fire TV conectado. Usa «Conectar» con la IP del "
            "dispositivo (Ajustes → Mi Fire TV → Red)."
        )
    tab = current_tab_id()
    if tab:
        with TABS_LOCK:
            entry = TABS.get(tab)
            if entry is None:
                entry = _new_tab_entry()
                TABS[tab] = entry
            explicit = bool(entry.get("explicit"))
            preferred = str(entry.get("serial") or "")
            if not preferred:
                preferred = str(STATE.get("serial") or saved_selection() or "")
            match = next((dev for dev in online if dev["serial"] == preferred), None)
            if match is None and explicit and preferred:
                raise AdbError(
                    f"El Fire TV elegido en esta pestaña ({preferred}) no está "
                    "disponible. Enciéndelo o elige otro en «Gestionar»."
                )
            if match is None:
                match = online[0]
            entry["serial"] = match["serial"]
            entry["seen"] = time.time()
            return match["serial"]
    for preferred in (STATE.get("serial"), saved_selection()):
        if not preferred:
            continue
        for dev in online:
            if dev["serial"] == preferred:
                STATE["serial"] = preferred
                return preferred
    STATE["serial"] = online[0]["serial"]
    return online[0]["serial"]


def device_shell(command: str, timeout: int = 40) -> str:
    serial = pick_serial()
    rc, out, err = adb_run(["-s", serial, "shell", command], timeout=timeout)
    text = out or err
    if rc != 0 and not out:
        raise AdbError(text or f"El comando falló (código {rc}).")
    return text


def device_shell_quiet(command: str, timeout: int = 40) -> tuple[int, str]:
    serial = pick_serial()
    rc, out, err = adb_run(["-s", serial, "shell", command], timeout=timeout)
    return rc, (out or err)


def current_device_info() -> dict | None:
    try:
        serial = pick_serial()
    except AdbError:
        return None
    props = {"serial": serial}
    for key, cmd in {
        "model": "getprop ro.product.model",
        "brand": "getprop ro.product.brand",
        "android": "getprop ro.build.version.release",
        "fireos": "getprop ro.build.version.name",
        "sdk": "getprop ro.build.version.sdk",
        "ip": "ip route | grep wlan0",
    }.items():
        try:
            rc, out, err = adb_run(["-s", serial, "shell", cmd], timeout=10)
            props[key] = out.strip()
        except AdbError:
            props[key] = ""
    match = re.search(r"src (\d+\.\d+\.\d+\.\d+)", props.get("ip", ""))
    props["ip"] = match.group(1) if match else ""
    try:
        props["abis"] = device_abis(serial)
    except AdbError:
        props["abis"] = []
    props["alias"] = next(
        (item["name"] for item in load_saved_devices() if item["serial"] == serial),
        "",
    )
    return props


# --------------------------------------------------------------------------
# Ficha del dispositivo
# --------------------------------------------------------------------------

PROFILE_SCRIPT = r"""
echo @@model@@; getprop ro.product.model
echo @@brand@@; getprop ro.product.manufacturer
echo @@device@@; getprop ro.product.device
echo @@board@@; getprop ro.board.platform
echo @@hardware@@; getprop ro.hardware
echo @@android@@; getprop ro.build.version.release
echo @@fireos@@; getprop ro.build.version.name
echo @@sdk@@; getprop ro.build.version.sdk
echo @@serial@@; getprop ro.serialno
echo @@cpuinfo@@; grep -m1 -E 'Hardware|model name' /proc/cpuinfo 2>/dev/null
echo @@cores@@; grep -c '^processor' /proc/cpuinfo 2>/dev/null
echo @@wm@@; wm size; wm density
echo @@mem@@; cat /proc/meminfo
echo @@stat@@; grep '^cpu ' /proc/stat; sleep 0.3; echo @@stat2@@; grep '^cpu ' /proc/stat
echo @@uptime@@; cat /proc/uptime
echo @@net@@; ip addr show wlan0 2>/dev/null
echo @@thermal@@; for z in /sys/class/thermal/thermal_zone*; do t=$(cat "$z/type" 2>/dev/null); v=$(cat "$z/temp" 2>/dev/null); [ -n "$v" ] && echo "$t|$v"; done
echo @@storage@@; df -k /data 2>/dev/null | tail -n 1
echo @@end@@
"""


def _profile_sections(text: str) -> dict[str, str]:
    """Parte la salida marcada con @@clave@@ en secciones."""
    sections: dict[str, list[str]] = {}
    current = None
    for raw in text.splitlines():
        line = raw.strip()
        match = re.fullmatch(r"@@([a-z0-9_]+)@@", line)
        if match:
            current = match.group(1)
            sections[current] = []
        elif current is not None:
            sections[current].append(raw.rstrip())
    return {key: "\n".join(value).strip() for key, value in sections.items()}


def _cpu_usage(first: str, second: str) -> int | None:
    def split(line: str) -> tuple[int, int] | None:
        parts = line.split()
        if len(parts) < 5 or parts[0] != "cpu":
            return None
        try:
            values = [int(value) for value in parts[1:9]]
        except ValueError:
            return None
        while len(values) < 8:
            values.append(0)
        total = sum(values)
        idle = values[3] + values[4]  # idle + iowait
        return total, idle

    start, end = split(first), split(second)
    if not start or not end:
        return None
    total = end[0] - start[0]
    idle = end[1] - start[1]
    if total <= 0:
        return None
    return max(0, min(100, round((1 - idle / total) * 100)))


def _thermal_celsius(raw: str) -> float | None:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    # La mayoría de kernels publican miligrados; algunos, décimas o grados.
    for divisor in (1000, 10, 1):
        candidate = value / divisor
        if 0 < candidate < 130:
            return round(candidate, 1)
    return None


def device_profile() -> dict:
    """Ficha completa: sistema, red, pantalla, CPU, memoria y temperatura."""
    serial = pick_serial()
    rc, out = device_shell_quiet(PROFILE_SCRIPT, timeout=35)
    data = _profile_sections(out)

    def one(key: str) -> str:
        return (data.get(key) or "").splitlines()[0].strip()

    width = height = None
    density = None
    physical = override = None
    physical_density = override_density = None
    for line in data.get("wm", "").splitlines():
        match = re.search(r"Physical size:\s*(\d+)x(\d+)", line)
        if match:
            physical = (int(match.group(1)), int(match.group(2)))
        match = re.search(r"Override size:\s*(\d+)x(\d+)", line)
        if match:
            override = (int(match.group(1)), int(match.group(2)))
        match = re.search(r"Physical density:\s*(\d+)", line)
        if match:
            physical_density = int(match.group(1))
        match = re.search(r"Override density:\s*(\d+)", line)
        if match:
            override_density = int(match.group(1))
    chosen = override or physical
    if chosen:
        width, height = chosen
    density = override_density or physical_density

    memory: dict[str, int] = {}
    for line in data.get("mem", "").splitlines():
        match = re.match(r"(\w+):\s+(\d+)\s*kB", line)
        if match:
            memory[match.group(1)] = int(match.group(2)) * 1024
    mem_total = memory.get("MemTotal") or 0
    mem_available = memory.get("MemAvailable")
    if mem_available is None:
        mem_available = memory.get("MemFree", 0) + memory.get("Buffers", 0) + memory.get("Cached", 0)
    mem_used = max(0, mem_total - mem_available)

    storage = {"total": 0, "used": 0, "available": 0}
    parts = data.get("storage", "").split()
    if len(parts) >= 4:
        try:
            storage = {
                "total": int(parts[1]) * 1024,
                "used": int(parts[2]) * 1024,
                "available": int(parts[3]) * 1024,
            }
        except ValueError:
            pass

    net = data.get("net", "")
    ip_match = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)", net)
    mac_match = re.search(r"link/ether\s+([0-9a-fA-F:]{17})", net)

    zones = []
    for line in data.get("thermal", "").splitlines():
        name, _, raw = line.partition("|")
        celsius = _thermal_celsius(raw)
        if celsius is None:
            continue
        label = name.strip() or "zona"
        if label in [zone["name"] for zone in zones]:
            continue
        zones.append({"name": label, "celsius": celsius})
    zones.sort(key=lambda zone: zone["celsius"], reverse=True)

    uptime = 0.0
    try:
        uptime = float((data.get("uptime") or "0").split()[0])
    except (ValueError, IndexError):
        pass

    cpuinfo = data.get("cpuinfo", "")
    cpuinfo = cpuinfo.split(":", 1)[1].strip() if ":" in cpuinfo else cpuinfo.strip()

    return {
        "serial": serial,
        "alias": next(
            (item["name"] for item in load_saved_devices() if item["serial"] == serial),
            "",
        ),
        "model": one("model"),
        "brand": one("brand"),
        "device": one("device"),
        "android": one("android"),
        "fireos": one("fireos"),
        "sdk": one("sdk"),
        "serial_number": one("serial"),
        "ip": ip_match.group(1) if ip_match else "",
        "mac": mac_match.group(1).upper() if mac_match else "",
        "resolution": {"width": width or 0, "height": height or 0},
        "density": density or 0,
        "cpu": {
            "model": cpuinfo or one("board") or one("hardware"),
            "board": one("board"),
            "cores": int(one("cores") or 0),
            "usage": _cpu_usage(data.get("stat", ""), data.get("stat2", "")),
        },
        "memory": {"total": mem_total, "used": mem_used, "available": mem_available},
        "storage": storage,
        "uptime": round(uptime),
        "temperature": {
            "max": zones[0]["celsius"] if zones else None,
            "zones": zones,
        },
    }


def sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


# --------------------------------------------------------------------------
# Arquitecturas (ABI): causa de INSTALL_FAILED_NO_MATCHING_ABIS
# --------------------------------------------------------------------------

ABI_FRIENDLY = {
    "armeabi-v7a": "ARM 32 bits",
    "armeabi": "ARM 32 bits (heredada)",
    "arm64-v8a": "ARM 64 bits",
    "x86": "Intel/AMD 32 bits",
    "x86_64": "Intel/AMD 64 bits",
    "mips": "MIPS",
    "mips64": "MIPS 64 bits",
}

ABI_CACHE = {}
ABI_CACHE_TTL = 600


def abi_label(abi):
    return ABI_FRIENDLY.get(abi, abi)


def fmt_abi_list(abis):
    return ", ".join("%s (%s)" % (abi, abi_label(abi)) for abi in abis) or "desconocidas"


def device_abis(serial=None):
    """ABIs que admite el Fire TV, preguntadas al propio dispositivo."""
    serial = serial or pick_serial()
    cached = ABI_CACHE.get(serial)
    if cached and time.time() - cached[0] < ABI_CACHE_TTL:
        return cached[1]
    abis = []
    for prop in ("ro.product.cpu.abilist", "ro.product.cpu.abi"):
        try:
            rc, out, err = adb_run(["-s", serial, "shell", "getprop " + prop], timeout=10)
        except AdbError:
            break
        raw = (out or "").strip()
        if raw and "not found" not in raw.lower():
            abis = [item.strip() for item in raw.split(",") if item.strip()]
            break
    ABI_CACHE[serial] = (time.time(), abis)
    return abis


def apk_native_abis(path):
    """ABIs con codigo nativo dentro del APK (lib/<abi>/*.so).

    Lista vacia = APK sin bibliotecas nativas, sirve en cualquier dispositivo.
    """
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError) as exc:
        raise AdbError("El archivo no parece un APK válido (ZIP dañado o incompleto).") from exc
    found = set()
    for name in names:
        parts = name.split("/")
        if len(parts) >= 3 and parts[0] == "lib" and parts[-1].endswith(".so"):
            found.add(parts[1])
    return sorted(found)


def abi_mismatch_message(libs, abis):
    return (
        "Este APK no se puede instalar en tu Fire TV por la arquitectura del "
        "procesador.\n\nEl APK trae código nativo solo para: " + fmt_abi_list(libs) + ".\n"
        "Tu Fire TV admite: " + fmt_abi_list(abis) + ".\n\n"
        "El Fire TV Stick Lite ejecuta Android de 32 bits (armeabi-v7a): un APK que "
        "solo incluya arm64-v8a o x86 nunca instalará, y ese es exactamente el error "
        "INSTALL_FAILED_NO_MATCHING_ABIS.\n\n"
        "Qué hacer:\n"
        "- Busca la variante universal o armeabi-v7a del APK (APKMirror tiene filtro de "
        "arquitectura; APKPure la marca como universal).\n"
        "- Si la app publica APK divididos, usa la versión universal de esa versión.\n"
        "- Prueba una versión anterior: muchas apps abandonaron el 32 bits.\n"
        "- Si solo existe arm64-v8a, esa app ya no soporta tu modelo."
    )


INSTALL_HINTS = (
    ("no_matching_abis", "El APK no incluye la arquitectura de tu Fire TV."),
    ("older_sdk", "El APK exige una versión de Android más nueva que la de tu Fire TV."),
    ("deprecated_sdk_version", "El APK es demasiado antiguo para esta versión de Fire OS."),
    ("insufficient_storage", "No queda espacio en el Fire TV. Libera caché o desinstala apps."),
    ("update_incompatible", "Ya hay una app con el mismo paquete y firma distinta. Desinstálala primero."),
    ("version_downgrade", "La versión instalada es más reciente. Desinstala la actual antes."),
    ("parse_failed", "El APK está dañado o incompleto. Vuelve a descargarlo."),
    ("invalid_apk", "El archivo no es un APK válido. Vuelve a descargarlo."),
    ("duplicate_permission", "Otra app ya declara el mismo permiso. Desinstala la que conflictúa."),
    ("user_restricted", "Fire OS bloqueó la instalación: revisa Opciones de desarrollador > Instalar apps desconocidas."),
    ("verification_failure", "Fire OS rechazó la verificación de la app. Desactiva la verificación o usa otra fuente."),
)


def install_failure_message(text, libs, abis):
    low = text.lower()
    if "no_matching_abis" in low and libs and abis:
        return abi_mismatch_message(libs, abis)
    for marker, hint in INSTALL_HINTS:
        if marker in low:
            return (hint + "\n\n" + text).strip()
    return text or "La instalación falló."



# --------------------------------------------------------------------------
# Comandos de alto nivel
# --------------------------------------------------------------------------

KEYCODES = {
    "home": 3,
    "back": 4,
    "up": 19,
    "down": 20,
    "left": 21,
    "right": 22,
    "center": 23,
    "ok": 23,
    "volume_up": 24,
    "volume_down": 25,
    "power": 26,
    "menu": 82,
    "search": 84,
    "play_pause": 85,
    "stop": 86,
    "next": 87,
    "previous": 88,
    "rewind": 89,
    "fast_forward": 90,
    "mute": 164,
    "settings": 176,
    "sleep": 223,
    "wakeup": 224,
    "tv": 170,
    "guide": 172,
    "info": 165,
    "captions": 175,
}


def send_key(name: str) -> str:
    key = name.strip().lower()
    code = KEYCODES.get(key)
    if code is None:
        if not key.isdigit():
            raise AdbError(f"Tecla desconocida: {name}")
        code = int(key)
    return device_shell(f"input keyevent {code}", timeout=15)


def send_text(text: str) -> str:
    # input text interpreta %s como espacio; el shell del dispositivo recibe
    # la cadena entre comillas simples.
    safe = text.replace(" ", "%s")
    return device_shell(f"input text {sh_quote(safe)}", timeout=20)


def display_size() -> tuple[int, int]:
    """Resolución lógica actual; si hay override, `wm size` lo imprime al final."""
    rc, out = device_shell_quiet("wm size", timeout=12)
    sizes = re.findall(r"(\d{3,5})x(\d{3,5})", out)
    if not sizes:
        raise AdbError("No pude leer la resolución del Fire TV.")
    width, height = sizes[-1]
    return int(width), int(height)


def _unit(value, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise AdbError(f"Coordenada {label} inválida.") from exc
    if not 0 <= number <= 1:
        raise AdbError(f"La coordenada {label} debe estar entre 0 y 1.")
    return number


def device_input(payload: dict) -> str:
    """Convierte un toque o un gesto del navegador en `input tap` / `input swipe`."""
    data = payload or {}
    action = str(data.get("action") or "tap").lower()
    width, height = display_size()
    if action == "tap":
        x = int(round(_unit(data.get("x"), "x") * (width - 1)))
        y = int(round(_unit(data.get("y"), "y") * (height - 1)))
        return device_shell(f"input tap {x} {y}", timeout=20)
    if action == "swipe":
        x1 = int(round(_unit(data.get("x"), "x") * (width - 1)))
        y1 = int(round(_unit(data.get("y"), "y") * (height - 1)))
        x2 = int(round(_unit(data.get("x2"), "x2") * (width - 1)))
        y2 = int(round(_unit(data.get("y2"), "y2") * (height - 1)))
        try:
            duration = int(data.get("duration") or 300)
        except (TypeError, ValueError):
            duration = 300
        duration = max(50, min(2000, duration))
        return device_shell(
            f"input swipe {x1} {y1} {x2} {y2} {duration}", timeout=25
        )
    raise AdbError("Acción táctil no soportada.")


def list_packages(include_system: bool = False) -> list[str]:
    flag = "" if include_system else "-3 "
    rc, out = device_shell_quiet(f"pm list packages {flag}".strip(), timeout=60)
    packages = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("package:"):
            packages.append(line[len("package:"):])
    return sorted(set(packages), key=str.lower)


# --------------------------------------------------------------------------
# Nombres legibles de las apps
#
# El nombre real de una app vive dentro de su APK (resources.arsc), y leerlo
# exigiría descargar cada APK por Wi-Fi. En su lugar combinamos un catálogo de
# las apps habituales en Fire TV con una reconstrucción del nombre a partir del
# identificador: «com.disney.disneyplus» -> «Disney Plus».
# --------------------------------------------------------------------------

APP_CATALOG: dict[str, tuple[str, str]] = {
    # Amazon / Fire OS
    "com.amazon.tv.launcher": ("Inicio de Fire TV", "Sistema"),
    "com.amazon.tv.settings": ("Ajustes de Fire TV", "Sistema"),
    "com.amazon.venezia": ("Amazon Appstore", "Sistema"),
    "com.amazon.avod.thirdpartyclient": ("Prime Video", "Vídeo"),
    "com.amazon.amazonvideo.livingroom": ("Prime Video", "Vídeo"),
    "com.amazon.avod": ("Prime Video", "Vídeo"),
    "com.amazon.firetv.youtube": ("YouTube", "Vídeo"),
    "com.amazon.cloud9": ("Amazon Silk", "Navegador"),
    "com.amazon.mp3": ("Amazon Music", "Música"),
    "com.amazon.kindle": ("Kindle", "Lectura"),
    "com.amazon.dee.app": ("Alexa", "Sistema"),
    "com.amazon.device.software.ota": ("Actualizaciones de Fire OS", "Sistema"),
    "com.amazon.dcp": ("Servicios de Amazon", "Sistema"),
    "com.amazon.webview.chromium": ("Motor web de Amazon", "Sistema"),
    "com.amazon.bueller.music": ("Amazon Music", "Música"),
    "com.amazon.bueller.photos": ("Amazon Photos", "Otros"),
    "com.amazon.gamehub": ("Amazon Game Hub", "Juegos"),
    "com.amazon.shoptv.client": ("Amazon Shopping", "Compras"),
    "com.amazon.shoptv.firetv.client": ("Amazon Shopping", "Compras"),
    "com.amazon.website_launcher": ("Amazon (web)", "Compras"),
    "com.amazon.firebat": ("Mando Bluetooth", "Sistema"),
    "com.amazon.device.bluetoothpa": ("Bluetooth del Fire TV", "Sistema"),
    "com.amazon.connectivitycontroller": ("Control de red", "Sistema"),
    "com.amazon.wifilocker": ("Wi-Fi del Fire TV", "Sistema"),
    "com.amazon.firehomestarter": ("Inicio de Fire TV", "Sistema"),
    "com.amazon.tv.quicksettings.ui": ("Menú rápido", "Sistema"),
    "com.amazon.device.settings": ("Ajustes internos", "Sistema"),
    "com.android.systemui": ("Interfaz del sistema", "Sistema"),
    "com.android.packageinstaller": ("Instalador de APK", "Sistema"),
    "com.android.permissioncontroller": ("Permisos de las apps", "Sistema"),
    "com.android.providers.settings": ("Ajustes del sistema", "Sistema"),
    "com.android.shell": ("Intérprete ADB", "Sistema"),
    # Android / Google
    "com.android.vending": ("Google Play Store", "Sistema"),
    "com.google.android.gms": ("Servicios de Google Play", "Sistema"),
    "com.google.android.youtube.tv": ("YouTube", "Vídeo"),
    "com.google.android.videos": ("Google TV", "Vídeo"),
    "com.android.chrome": ("Chrome", "Navegador"),
    "com.android.documentsui": ("Archivos", "Sistema"),
    "com.android.settings": ("Ajustes de Android", "Sistema"),
    "com.android.tv.settings": ("Ajustes de Android TV", "Sistema"),
    "com.google.android.leanbacklauncher": ("Inicio de Android TV", "Sistema"),
    # Vídeo y streaming
    "com.netflix.ninja": ("Netflix", "Vídeo"),
    "com.disney.disneyplus": ("Disney+", "Vídeo"),
    "com.hbo.hbonow": ("HBO Max", "Vídeo"),
    "com.wbd.stream": ("Max", "Vídeo"),
    "com.hulu.plus": ("Hulu", "Vídeo"),
    "com.cbs.ott": ("Paramount+", "Vídeo"),
    "com.peacocktv.peacockandroid": ("Peacock", "Vídeo"),
    "com.crunchyroll.crunchyroid": ("Crunchyroll", "Vídeo"),
    "com.pluto.tv": ("Pluto TV", "TV en vivo"),
    "com.tubitv": ("Tubi", "Vídeo"),
    "tv.twitch.android.app": ("Twitch", "Vídeo"),
    "com.apple.atve.androidtv.appletv": ("Apple TV", "Vídeo"),
    "com.dazn": ("DAZN", "Deportes"),
    "com.redbull.tv": ("Red Bull TV", "Deportes"),
    "com.plexapp.android": ("Plex", "Vídeo"),
    "org.xbmc.kodi": ("Kodi", "Vídeo"),
    "org.videolan.vlc": ("VLC", "Vídeo"),
    "com.mxtech.videoplayer.ad": ("MX Player", "Vídeo"),
    "com.stremio.one": ("Stremio", "Vídeo"),
    "org.jellyfin.androidtv": ("Jellyfin", "Vídeo"),
    "com.mb.android": ("Emby", "Vídeo"),
    "com.liskovsoft.smarttubetv": ("SmartTube", "Vídeo"),
    "com.liskovsoft.smarttubetv.beta": ("SmartTube (beta)", "Vídeo"),
    "com.teamsmart.videomanager.tv": ("SmartTube", "Vídeo"),
    "io.gh.reisxd.tizentube.cobalt": ("TizenTube Cobalt", "Vídeo"),
    "ar.tvplayer.tv": ("TiviMate", "TV en vivo"),
    "com.mxtech.videoplayer.pro": ("MX Player Pro", "Vídeo"),
    # Música
    "com.spotify.tv.android": ("Spotify", "Música"),
    "com.deezer.android.tv": ("Deezer", "Música"),
    "com.pandora.android.atv": ("Pandora", "Música"),
    # Utilidades
    "com.brave.browser": ("Brave", "Navegador"),
    "org.mozilla.firefox": ("Firefox", "Navegador"),
    "com.estrongs.android.pop": ("ES File Explorer", "Herramientas"),
    "com.mixplorer": ("MiXplorer", "Herramientas"),
    "com.esaba.downloader": ("Downloader", "Herramientas"),
    "com.analiti.fastest.android": ("Analiti", "Herramientas"),
    "org.zwanoo.android.speedtest": ("Speedtest", "Herramientas"),
    "com.cloudmosa.puffinTV": ("Puffin TV", "Navegador"),
    "com.phlox.tvwebbrowser": ("TV Bro", "Navegador"),
}

CATEGORY_ICONS = {
    "Vídeo": "🎬",
    "TV en vivo": "📡",
    "Música": "🎵",
    "Deportes": "⚽",
    "Juegos": "🎮",
    "Navegador": "🌐",
    "Herramientas": "🛠️",
    "Sistema": "⚙️",
    "Niños": "🧸",
    "Lectura": "📚",
    "Compras": "🛒",
    "Red": "🔒",
    "Otros": "📦",
}

# Prefijos de dominio inverso que no aportan nada al nombre.
_TLDS = {
    "com", "org", "net", "io", "tv", "co", "me", "app", "xyz", "dev", "info",
    "biz", "us", "es", "mx", "ar", "br", "cl", "pe", "ve", "ec", "uy", "py",
    "bo", "de", "fr", "uk", "it", "nl", "pl", "tr", "ru", "ua", "in", "id",
    "my", "th", "vn", "ph", "sg", "jp", "kr", "cn", "hk", "tw", "au", "nz",
    "ca", "ch", "se", "no", "dk", "fi", "at", "be", "pt", "gr", "cz", "hu",
    "ro", "il", "za", "ae", "sa",
}

# Palabras que aparecen en el identificador pero no en el nombre de la app.
_NOISE_WORDS = {
    "android", "mobile", "app", "apps", "client", "phone", "tablet", "release",
    "prod", "production", "official", "the", "and", "for", "of", "hd", "full",
    "github", "gitlab", "gh", "git",
}

_ACRONYMS = {
    "tv", "hd", "uhd", "4k", "8k", "iptv", "vpn", "apk", "fm", "ai", "os",
    "pc", "usb", "dlna", "hdr", "vr", "ar", "xl", "sms", "gps", "nfc",
}

_CATEGORY_KEYWORDS = (
    ("TV en vivo", ("iptv", "livetv", "live", "tdt", "channel", "canal", "epg", "m3u")),
    ("Música", ("music", "musica", "audio", "radio", "podcast", "spotify", "deezer", "pandora", "sound")),
    ("Juegos", ("game", "games", "juego", "arcade", "emulator", "retro", "xbox", "playstation")),
    ("Navegador", ("browser", "chrome", "firefox", "silk", "opera", "webview")),
    ("Deportes", ("sport", "deporte", "dazn", "espn", "nba", "nfl", "futbol", "soccer")),
    ("Herramientas", ("vpn", "proxy", "file", "explorer", "cleaner", "speedtest", "tool",
                      "utility", "downloader", "installer", "manager", "settings")),
    ("Niños", ("kid", "kids", "child", "junior", "infantil")),
    ("Vídeo", ("video", "movie", "film", "player", "stream", "cinema", "plex", "kodi",
               "vlc", "netflix", "tv", "tizentube", "cobalt", "youtube")),
)


def _split_words(segment: str) -> list[str]:
    segment = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", segment)
    segment = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", segment)
    return [word for word in re.split(r"\s+", segment) if word]


def _title_word(word: str) -> str:
    low = word.lower()
    if low in _ACRONYMS:
        return low.upper()
    if re.fullmatch(r"\d+[kp]", low):
        return low.upper()
    return low[:1].upper() + low[1:]


def pretty_name(package: str) -> str:
    """Reconstruye un nombre legible a partir del identificador del paquete."""
    parts = [part for part in re.split(r"[._\-]+", package) if part]
    while len(parts) > 1 and parts[0].lower() in _TLDS:
        parts.pop(0)

    words: list[str] = []
    for part in parts:
        words.extend(_split_words(part))

    # Quita palabras de relleno del principio y del final, nunca todas.
    while len(words) > 1 and words[0].lower() in _NOISE_WORDS:
        words.pop(0)
    while len(words) > 1 and words[-1].lower() in _NOISE_WORDS:
        words.pop()

    # «disney disneyplus» -> «disney plus»: si una palabra contiene a la
    # anterior, la anterior es un prefijo redundante.
    collapsed: list[str] = []
    for word in words:
        low = word.lower()
        if collapsed:
            last = collapsed[-1]
            last_low = last.lower()
            if low.startswith(last_low) and len(low) > len(last):
                remainder = word[len(last):].strip("-_ ")
                if remainder:
                    collapsed.append(remainder)
                continue
            if last_low.startswith(low):
                continue
        collapsed.append(word)

    if not collapsed:
        return package

    # Cuatro palabras ya son demasiadas para una lista.
    label = " ".join(_title_word(word) for word in collapsed[:4])
    return label or package


def guess_category(package: str) -> str:
    low = package.lower()
    for category, keywords in _CATEGORY_KEYWORDS:
        if any(keyword in low for keyword in keywords):
            return category
    if low.startswith(("com.amazon", "com.android", "com.google.android")):
        return "Sistema"
    return "Otros"


def describe_app(package: str) -> dict:
    known = APP_CATALOG.get(package)
    if known:
        name, category = known
    else:
        name, category = pretty_name(package), guess_category(package)
    return {
        "package": package,
        "name": name,
        "known": bool(known),
        "category": category,
        "icon": CATEGORY_ICONS.get(category, "📦"),
    }


_PACKAGE_RE = re.compile(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)*")


def normalize_package(value: str) -> str:
    """Devuelve solo el identificador si llega una ruta tipo «.../base.apk=com.app».

    Fire OS moderno incluye el hash de instalación en la ruta y ese hash puede
    contener «=», así que el paquete es siempre lo que va tras el ÚLTIMO «=».
    """
    value = str(value or "").strip()
    if "=" in value:
        value = value.rsplit("=", 1)[1].strip()
    return value


def valid_package(package: str) -> bool:
    return bool(package and _PACKAGE_RE.fullmatch(package))


def split_package_line(body: str) -> tuple[str, str]:
    """Separa la ruta del APK y el paquete de una línea de `pm list packages -f`."""
    body = body.strip()
    if "=" not in body:
        return "", normalize_package(body)
    apk, _, package = body.rpartition("=")
    return apk.strip(), normalize_package(package)


def list_apps(include_system: bool = False) -> list[dict]:
    """Apps instaladas con nombre legible y ruta del APK."""
    flag = "" if include_system else "-3 "
    rc, out = device_shell_quiet(f"pm list packages -f {flag}".strip(), timeout=90)
    apps: list[dict] = []
    seen: set[str] = set()
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("package:"):
            continue
        apk, package = split_package_line(line[len("package:"):])
        if not valid_package(package) or package in seen:
            continue
        seen.add(package)
        app = describe_app(package)
        app["apk"] = apk
        apps.append(app)

    if not apps:
        # Algún `pm` antiguo no acepta -f junto a -3: se reintenta sin rutas.
        for package in list_packages(include_system):
            app = describe_app(package)
            app["apk"] = ""
            apps.append(app)

    apps.sort(key=lambda app: (app["name"].casefold(), app["package"]))
    return apps


# --------------------------------------------------------------------------
# Iconos reales: se extraen del APK y se guardan en caché local
# --------------------------------------------------------------------------

ICON_DIR = ROOT / ".cache" / "icons"
ICON_SEMAPHORE = threading.Semaphore(2)
ICON_LOCKS: dict[str, threading.Lock] = {}
ICON_LOCKS_GUARD = threading.Lock()
ICON_MISSES: dict[str, float] = {}
ICON_MISS_TTL = 900
ICON_MAX_APK = 250 * 1024 * 1024


def _icon_lock(package: str) -> threading.Lock:
    with ICON_LOCKS_GUARD:
        return ICON_LOCKS.setdefault(package, threading.Lock())


def _image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Lee ancho y alto sin decodificar la imagen (PNG o WebP)."""
    if len(data) >= 24 and data.startswith(PNG_SIG) and data[12:16] == b"IHDR":
        return (
            int.from_bytes(data[16:20], "big"),
            int.from_bytes(data[20:24], "big"),
        )
    if len(data) >= 30 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        chunk = data[12:16]
        if chunk == b"VP8X":
            return (
                1 + int.from_bytes(data[24:27], "little"),
                1 + int.from_bytes(data[27:30], "little"),
            )
        if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
            return (
                int.from_bytes(data[26:28], "little") & 0x3FFF,
                int.from_bytes(data[28:30], "little") & 0x3FFF,
            )
        if chunk == b"VP8L" and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            return (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1
    if len(data) >= 4 and data[:2] == b"\xff\xd8":
        # JPEG: algunos APK guardan el icono como .png pero con datos JPEG.
        pos = 2
        while pos + 9 <= len(data):
            if data[pos] != 0xFF:
                pos += 1
                continue
            marker = data[pos + 1]
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                pos += 2
                continue
            length = int.from_bytes(data[pos + 2:pos + 4], "big")
            if length < 2:
                break
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                height = int.from_bytes(data[pos + 5:pos + 7], "big")
                width = int.from_bytes(data[pos + 7:pos + 9], "big")
                return width, height
            pos += 2 + length
    return None


_DENSITY_SCORE = {
    "ldpi": 4,
    "mdpi": 8,
    "hdpi": 14,
    "xhdpi": 20,
    "xxhdpi": 26,
    "xxxhdpi": 32,
    "nodpi": 6,
    "anydpi": 0,
}

ICON_CACHE_VERSION = "icons-v3"


# --- Lectura de recursos Android (AXML y resources.arsc) sin dependencias ---


def _u16(data: bytes, off: int) -> int:
    return struct.unpack_from("<H", data, off)[0]


def _u32(data: bytes, off: int) -> int:
    return struct.unpack_from("<I", data, off)[0]


def _i32(data: bytes, off: int) -> int:
    return struct.unpack_from("<i", data, off)[0]


def _parse_string_pool(data: bytes, off: int) -> list[str]:
    header_size = _u16(data, off + 2)
    count = _u32(data, off + 8)
    flags = _u32(data, off + 16)
    strings_start = _u32(data, off + 20)
    utf8 = bool(flags & 0x100)
    base = off + strings_start
    result = []
    for index in range(count):
        pos = base + _u32(data, off + header_size + index * 4)
        if utf8:
            pos += 1
            if data[pos - 1] & 0x80:
                pos += 1
            length = data[pos]
            pos += 1
            if length & 0x80:
                length = ((length & 0x7F) << 8) | data[pos]
                pos += 1
            result.append(data[pos:pos + length].decode("utf-8", "replace"))
        else:
            length = _u16(data, pos)
            pos += 2
            if length & 0x8000:
                length = ((length & 0x7FFF) << 16) | _u16(data, pos)
                pos += 2
            result.append(data[pos:pos + length * 2].decode("utf-16-le", "replace"))
    return result


def _parse_binary_xml(data: bytes) -> list[dict]:
    """Devuelve los elementos de un XML binario de Android con sus atributos."""
    if len(data) < 8 or _u16(data, 0) != 0x0003:
        return []
    total = min(_u32(data, 4), len(data))
    strings: list[str] = []
    events = []
    off = _u16(data, 2)
    while off + 8 <= total:
        chunk_type = _u16(data, off)
        header_size = _u16(data, off + 2)
        chunk_size = _u32(data, off + 4)
        if chunk_size < 8:
            break
        if chunk_type == 0x0001:
            strings = _parse_string_pool(data, off)
        elif chunk_type == 0x0102:
            ext = off + header_size
            name_i = _i32(data, ext + 4)
            attr_start = _u16(data, ext + 8)
            attr_size = _u16(data, ext + 10)
            attr_count = _u16(data, ext + 12)
            attrs = {}
            for index in range(attr_count):
                attr = ext + attr_start + index * attr_size
                attr_name_i = _i32(data, attr + 4)
                raw = _i32(data, attr + 8)
                dtype = data[attr + 15]
                ddata = _u32(data, attr + 16)
                if 0 <= attr_name_i < len(strings):
                    attrs[strings[attr_name_i]] = (raw, dtype, ddata)
            name = strings[name_i] if 0 <= name_i < len(strings) else None
            events.append({"name": name, "attrs": attrs, "strings": strings})
        elif chunk_type == 0x0103:
            ext = off + header_size
            name_i = _i32(data, ext + 4)
            name = strings[name_i] if 0 <= name_i < len(strings) else None
            events.append({"name": name, "attrs": {}, "strings": strings, "end": True})
        off += chunk_size
    return events


def _manifest_icon_id(manifest: bytes) -> int | str | None:
    for event in _parse_binary_xml(manifest):
        if event["name"] != "application":
            continue
        attr = event["attrs"].get("icon")
        if not attr:
            continue
        raw, dtype, ddata = attr
        if dtype == 0x01:
            return ddata
        if dtype == 0x03:
            index = raw if raw >= 0 else ddata
            if 0 <= index < len(event["strings"]):
                return event["strings"][index]
    return None


class _ResourceTable:
    """Lector mínimo de resources.arsc: id de recurso -> ruta del archivo."""

    def __init__(self, data: bytes):
        self.data = data
        self.global_strings: list[str] = []
        self.entries: dict[int, list[tuple[int, int, int]]] = {}
        self._parse()

    def _parse(self) -> None:
        if len(self.data) < 12 or _u16(self.data, 0) != 0x0002:
            return
        total = min(_u32(self.data, 4), len(self.data))
        off = _u16(self.data, 2)
        while off + 8 <= total:
            chunk_type = _u16(self.data, off)
            header_size = _u16(self.data, off + 2)
            chunk_size = _u32(self.data, off + 4)
            if chunk_size < 8:
                break
            if chunk_type == 0x0001:
                self.global_strings = _parse_string_pool(self.data, off)
            elif chunk_type == 0x0200:
                self._parse_package(off, header_size, chunk_size)
            off += chunk_size

    def _parse_package(self, off: int, header_size: int, chunk_size: int) -> None:
        package_id = _u32(self.data, off + 8)
        pos = off + header_size
        end = off + chunk_size
        while pos + 8 <= end:
            chunk_type = _u16(self.data, pos)
            chunk_size = _u32(self.data, pos + 4)
            if chunk_size < 8:
                break
            if chunk_type == 0x0201:
                self._parse_type(pos, _u16(self.data, pos + 2), chunk_size, package_id)
            pos += chunk_size

    def _parse_type(self, off: int, header_size: int, chunk_size: int, package_id: int) -> None:
        type_id = self.data[off + 8]
        flags = self.data[off + 9]
        count = _u32(self.data, off + 12)
        entries_start = _u32(self.data, off + 16)
        config = off + 20
        density = _u16(self.data, config + 14) if header_size >= 36 else 0
        offsets = []
        if flags & 0x01:  # entradas dispersas
            for index in range(count):
                entry_index = _u16(self.data, off + header_size + index * 4)
                entry_off = _u16(self.data, off + header_size + index * 4 + 2) * 4
                offsets.append((entry_index, entry_off))
        elif flags & 0x02:  # offsets de 16 bits
            for index in range(count):
                entry_off = _u16(self.data, off + header_size + index * 2) * 4
                if entry_off:
                    offsets.append((index, entry_off))
        else:
            for index in range(count):
                entry_off = _u32(self.data, off + header_size + index * 4)
                if entry_off != 0xFFFFFFFF:
                    offsets.append((index, entry_off))
        limit = off + chunk_size
        for entry_index, entry_off in offsets:
            entry = off + entries_start + entry_off
            if entry + 8 > limit:
                continue
            entry_size = _u16(self.data, entry)
            entry_flags = _u16(self.data, entry + 2)
            if entry_flags & 0x0001 or entry_size < 8:
                continue  # recurso complejo (bag/estilo)
            value = entry + entry_size
            if value + 8 > limit:
                continue
            dtype = self.data[value + 3]
            ddata = _u32(self.data, value + 4)
            res_id = (package_id << 24) | (type_id << 16) | entry_index
            self.entries.setdefault(res_id, []).append((density, dtype, ddata))

    def resolve(self, res_id: int, depth: int = 0) -> str | None:
        values = self.entries.get(res_id)
        if not values or depth > 8:
            return None
        for _density, dtype, ddata in sorted(values, key=lambda item: item[0], reverse=True):
            if dtype == 0x03:
                if 0 <= ddata < len(self.global_strings):
                    return self.global_strings[ddata]
            elif dtype == 0x01:
                found = self.resolve(ddata, depth + 1)
                if found:
                    return found
        return None

    def resolve_color(self, res_id: int, depth: int = 0) -> int | None:
        values = self.entries.get(res_id)
        if not values or depth > 8:
            return None
        for _density, dtype, ddata in sorted(values, key=lambda item: item[0], reverse=True):
            if dtype in (0x1C, 0x1D, 0x1E, 0x1F):
                return ddata
            if dtype == 0x01:
                found = self.resolve_color(ddata, depth + 1)
                if found is not None:
                    return found
        return None


def _is_image(data: bytes) -> str | None:
    if data.startswith(PNG_SIG):
        return ".png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if len(data) >= 3 and data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    return None


def _attr_float(event: dict, name: str, default: float = 0.0) -> float:
    attr = event["attrs"].get(name)
    if not attr or attr[1] != 0x04:
        return default
    return struct.unpack("<f", struct.pack("<I", attr[2] & 0xFFFFFFFF))[0]


def _attr_color(event: dict, name: str) -> int | None:
    attr = event["attrs"].get(name)
    if not attr or attr[1] not in (0x1C, 0x1D, 0x1E, 0x1F):
        return None
    return attr[2]


def _attr_string(event: dict, name: str) -> str | None:
    attr = event["attrs"].get(name)
    if not attr or attr[1] != 0x03:
        return None
    index = attr[0] if attr[0] >= 0 else attr[2]
    if not 0 <= index < len(event["strings"]):
        return None
    return event["strings"][index]


def _color_css(value: int) -> tuple[str, float]:
    alpha = (value >> 24) & 0xFF
    if value <= 0xFFFFFF:
        alpha = 255
    return f"#{value & 0xFFFFFF:06x}", alpha / 255


def _group_transform(event: dict) -> str:
    tx = _attr_float(event, "translateX")
    ty = _attr_float(event, "translateY")
    scale_x = _attr_float(event, "scaleX", 1.0)
    scale_y = _attr_float(event, "scaleY", 1.0)
    rotation = _attr_float(event, "rotation")
    pivot_x = _attr_float(event, "pivotX")
    pivot_y = _attr_float(event, "pivotY")
    return (
        f"translate({tx + pivot_x:.6g} {ty + pivot_y:.6g}) "
        f"rotate({rotation:.6g}) scale({scale_x:.6g} {scale_y:.6g}) "
        f"translate({-pivot_x:.6g} {-pivot_y:.6g})"
    )


def _vector_body(events: list[dict]) -> str:
    parts = []
    for event in events:
        name = event["name"]
        if event.get("end"):
            if name == "group":
                parts.append("</g>")
            continue
        if name == "group":
            parts.append(f'<g transform="{_group_transform(event)}">')
        elif name == "path":
            path_data = _attr_string(event, "pathData")
            if not path_data:
                continue
            attrs = []
            fill = _attr_color(event, "fillColor")
            if fill is None:
                attrs.append('fill="none"')
            else:
                css, alpha = _color_css(fill)
                alpha *= _attr_float(event, "fillAlpha", 1.0)
                attrs.append(f'fill="{css}"')
                if alpha < 1:
                    attrs.append(f'fill-opacity="{alpha:.4g}"')
            stroke = _attr_color(event, "strokeColor")
            if stroke is not None:
                css, alpha = _color_css(stroke)
                alpha *= _attr_float(event, "strokeAlpha", 1.0)
                attrs.append(f'stroke="{css}"')
                if alpha < 1:
                    attrs.append(f'stroke-opacity="{alpha:.4g}"')
                width = _attr_float(event, "strokeWidth")
                if width:
                    attrs.append(f'stroke-width="{width:.6g}"')
            attrs.append(f'd="{html.escape(path_data, quote=True)}"')
            parts.append("<path " + " ".join(attrs) + "/>")
    return "".join(parts)


def _svg_layer(
    zf: zipfile.ZipFile,
    table: "_ResourceTable",
    res_id: int,
    foreground: bool = False,
    depth: int = 0,
) -> str | None:
    """Convierte una capa de icono adaptativo en SVG (imagen o vector)."""
    if depth > 4:
        return None
    path = table.resolve(res_id)
    if not path:
        return None
    try:
        data = zf.read(path)
    except (KeyError, RuntimeError, zipfile.BadZipFile, OSError):
        return None
    extension = _is_image(data)
    if extension:
        encoded = base64.b64encode(data).decode("ascii")
        fit = "xMidYMid meet" if foreground else "xMidYMid slice"
        return (
            f'<image x="0" y="0" width="108" height="108" '
            f'preserveAspectRatio="{fit}" '
            f'href="data:image/{extension[1:]};base64,{encoded}"/>'
        )
    if not data.startswith(b"\x03\x00"):
        return None
    events = _parse_binary_xml(data)
    root = next((event for event in events if not event.get("end")), None)
    if not root or root["name"] != "vector":
        return None
    view_w = _attr_float(root, "viewportWidth", 100.0) or 100.0
    view_h = _attr_float(root, "viewportHeight", 100.0) or 100.0
    body = _vector_body(events)
    return f'<g transform="scale({108 / view_w:.6g} {108 / view_h:.6g})">{body}</g>'


def _svg_from_resource(zf: zipfile.ZipFile, table: "_ResourceTable", res_id: int) -> str | None:
    """Genera un SVG para iconos vectoriales/adaptativos, sin rasterizarlos."""
    path = table.resolve(res_id)
    if not path:
        return None
    try:
        data = zf.read(path)
    except (KeyError, RuntimeError, zipfile.BadZipFile, OSError):
        return None
    if _is_image(data) or not data.startswith(b"\x03\x00"):
        return None
    events = _parse_binary_xml(data)
    root = next((event for event in events if not event.get("end")), None)
    if not root:
        return None
    if root["name"] == "vector":
        view_w = _attr_float(root, "viewportWidth", 100.0) or 100.0
        view_h = _attr_float(root, "viewportHeight", 100.0) or 100.0
        body = _vector_body(events)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" '
            f'viewBox="0 0 {view_w:.6g} {view_h:.6g}">{body}</svg>'
        )
    if root["name"] == "adaptive-icon":
        background_ref = None
        foreground_ref = None
        for event in events:
            if event.get("end"):
                continue
            if event["name"] == "background":
                attr = event["attrs"].get("drawable")
                if attr and attr[1] == 0x01:
                    background_ref = attr[2]
            elif event["name"] == "foreground":
                attr = event["attrs"].get("drawable")
                if attr and attr[1] == 0x01:
                    foreground_ref = attr[2]
        background = _svg_layer(zf, table, background_ref) if background_ref else None
        if not background and background_ref:
            color = table.resolve_color(background_ref)
            if color is not None:
                css, alpha = _color_css(color)
                background = (
                    f'<rect x="0" y="0" width="108" height="108" fill="{css}"'
                    + (f' fill-opacity="{alpha:.4g}"' if alpha < 1 else "")
                    + "/>"
                )
        foreground = _svg_layer(zf, table, foreground_ref, foreground=True) if foreground_ref else None
        if not background and not foreground:
            return None
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" '
            f'viewBox="0 0 108 108">{background or ""}{foreground or ""}</svg>'
        )
    if root["name"] == "layer-list":
        layers = []
        for event in events:
            if event.get("end") or event["name"] != "item":
                continue
            attr = event["attrs"].get("drawable")
            if not attr or attr[1] != 0x01:
                continue
            layer = _svg_layer(zf, table, attr[2], foreground=True)
            if layer:
                layers.append(layer)
        if not layers:
            return None
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" width="256" height="256" '
            f'viewBox="0 0 108 108">{"".join(layers)}</svg>'
        )
    return None


def _icon_from_resource(zf: zipfile.ZipFile, table: _ResourceTable, res_id: int, depth: int = 0) -> tuple[bytes, str] | None:
    """Sigue una referencia de resources.arsc hasta el PNG/WebP final."""
    if depth > 6:
        return None
    path = table.resolve(res_id)
    if not path:
        return None
    try:
        data = zf.read(path)
    except (KeyError, RuntimeError, zipfile.BadZipFile, OSError):
        return None
    ext = _is_image(data)
    if ext:
        return data, ext
    if not data.startswith(b"\x03\x00"):
        return None
    try:
        events = _parse_binary_xml(data)
    except (struct.error, IndexError, ValueError):
        return None
    adaptive = any(event["name"] == "adaptive-icon" for event in events)
    refs = []
    for event in events:
        if event["name"] == "background":
            continue
        for key in ("foreground", "src", "drawable"):
            attr = event["attrs"].get(key)
            if attr and attr[1] == 0x01:
                refs.append(attr[2])
    if not adaptive:
        for event in events:
            if event["name"] != "background":
                continue
            attr = event["attrs"].get("drawable")
            if attr and attr[1] == 0x01:
                refs.append(attr[2])
    for ref in refs:
        found = _icon_from_resource(zf, table, ref, depth + 1)
        if found:
            return found
    return None


def _extract_manifest_icon(zf: zipfile.ZipFile) -> tuple[tuple[bytes, str] | None, bool]:
    """Resuelve el android:icon del manifiesto. Devuelve (icono, bloqueado).

    ``bloqueado`` indica que el icono es un XML (adaptativo/vectorial) sin
    ninguna capa raster utilizable, así que conviene mostrar el monograma de
    la app en vez de elegir una imagen cualquiera del APK.
    """
    try:
        manifest = zf.read("AndroidManifest.xml")
        table = _ResourceTable(zf.read("resources.arsc"))
        icon = _manifest_icon_id(manifest)
    except (KeyError, RuntimeError, zipfile.BadZipFile, OSError, struct.error, IndexError, ValueError):
        return None, False
    if not isinstance(icon, int):
        return None, False
    try:
        found = _icon_from_resource(zf, table, icon)
    except (struct.error, IndexError, ValueError):
        return None, False
    if found:
        return found, False
    try:
        svg = _svg_from_resource(zf, table, icon)
    except (struct.error, IndexError, ValueError, KeyError):
        svg = None
    if svg:
        return (svg.encode("utf-8"), ".svg"), False
    try:
        path = table.resolve(icon)
        data = zf.read(path) if path else b""
    except (KeyError, RuntimeError, zipfile.BadZipFile, OSError):
        data = b""
    return None, data.startswith(b"\x03\x00")


def _icon_name_score(name: str) -> int:
    low = name.lower()
    base = low.rsplit("/", 1)[-1]
    score = 0
    if "ic_launcher" in base:
        score += 120
    elif "launcher" in base:
        score += 70
    elif "app_icon" in base or base in ("icon.png", "icon.webp", "ic_icon.png", "ic_app"):
        score += 60
    elif "icon" in base:
        score += 35
    if "/mipmap" in low:
        score += 12
    if low.startswith("res/"):
        score += 8
    for density, bonus in _DENSITY_SCORE.items():
        if f"-{density}" in low or f"/{density}" in low:
            score += bonus
            break
    if low.endswith(".9.png"):
        score -= 40
    if "round" in base:
        score -= 8
    if "foreground" in base:
        score -= 6
    if "background" in base:
        score -= 14
    if "notification" in base or "notif" in base:
        score -= 20
    if "banner" in base or "splash" in base:
        score -= 20
    return score


def _png_stats(data: bytes) -> dict | None:
    """Analiza un PNG 8 bits y devuelve métricas para descartar imágenes vacías."""
    if not data.startswith(PNG_SIG):
        return None
    pos = 8
    width = height = bit_depth = color_type = None
    idat = []
    palette = None
    transparency = None
    while pos + 8 <= len(data):
        length = struct.unpack_from(">I", data, pos)[0]
        chunk_type = data[pos + 4:pos + 8]
        if pos + 12 + length > len(data):
            break
        chunk = data[pos + 8:pos + 8 + length]
        if chunk_type == b"IHDR" and length >= 13:
            width, height, bit_depth, color_type, _comp, _filt, interlace = struct.unpack(">IIBBBBB", chunk[:13])
            if interlace or bit_depth != 8 or color_type not in (0, 2, 3, 4, 6):
                return None
        elif chunk_type == b"PLTE":
            palette = chunk
        elif chunk_type == b"tRNS":
            transparency = chunk
        elif chunk_type == b"IDAT":
            idat.append(chunk)
        elif chunk_type == b"IEND":
            break
        pos += 12 + length
    if not width or not height or width * height > 4_000_000 or not idat:
        return None
    try:
        raw = zlib.decompress(b"".join(idat))
    except zlib.error:
        return None
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color_type]
    stride = width * channels
    if len(raw) < height * (stride + 1):
        return None
    previous = bytearray(stride)
    step = max(1, int((width * height / 4096) ** 0.5))
    samples = []
    offset = 0
    for y in range(height):
        filter_type = raw[offset]
        offset += 1
        line = bytearray(raw[offset:offset + stride])
        offset += stride
        if filter_type == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 255
        elif filter_type == 2:
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 255
        elif filter_type == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 255
        elif filter_type == 4:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                up = previous[i]
                up_left = previous[i - channels] if i >= channels else 0
                estimate = left + up - up_left
                dist_left = abs(estimate - left)
                dist_up = abs(estimate - up)
                dist_up_left = abs(estimate - up_left)
                if dist_left <= dist_up and dist_left <= dist_up_left:
                    predictor = left
                elif dist_up <= dist_up_left:
                    predictor = up
                else:
                    predictor = up_left
                line[i] = (line[i] + predictor) & 255
        elif filter_type != 0:
            previous = line
            continue
        if y % step:
            previous = line
            continue
        for x in range(0, width, step):
            i = x * channels
            if color_type == 6:
                r, g, b, a = line[i], line[i + 1], line[i + 2], line[i + 3]
            elif color_type == 2:
                r, g, b = line[i], line[i + 1], line[i + 2]
                a = 255
            elif color_type == 0:
                r = g = b = line[i]
                a = 255
            elif color_type == 4:
                r = g = b = line[i]
                a = line[i + 1]
            else:
                index = line[i]
                r, g, b = palette[index * 3:index * 3 + 3] if palette else (0, 0, 0)
                a = transparency[index] if transparency and index < len(transparency) else 255
            samples.append((r, g, b, a))
        previous = line
    if not samples:
        return None
    opaque = [p for p in samples if p[3] >= 200]
    total = len(samples)
    if not opaque:
        return {"opaque": 0.0, "transparent": 1.0, "buckets": 0, "variance": 0.0, "white": 0.0, "green": 0.0}
    mean_r = sum(p[0] for p in opaque) / len(opaque)
    mean_g = sum(p[1] for p in opaque) / len(opaque)
    mean_b = sum(p[2] for p in opaque) / len(opaque)
    variance = sum((p[0] - mean_r) ** 2 + (p[1] - mean_g) ** 2 + (p[2] - mean_b) ** 2 for p in opaque) / len(opaque)
    buckets = {(p[0] >> 4, p[1] >> 4, p[2] >> 4) for p in opaque}
    white = sum(1 for p in opaque if p[0] >= 240 and p[1] >= 240 and p[2] >= 240) / len(opaque)
    green = sum(1 for p in opaque if 120 <= p[0] <= 190 and 170 <= p[1] <= 225 and 40 <= p[2] <= 130) / len(opaque)
    return {
        "opaque": len(opaque) / total,
        "transparent": sum(1 for p in samples if p[3] < 40) / total,
        "buckets": len(buckets),
        "variance": variance,
        "white": white,
        "green": green,
    }


def _png_content_adjust(stats: dict | None) -> float:
    if not stats:
        return 0.0
    if stats["opaque"] < 0.05:
        return -120.0
    score = 0.0
    if stats["green"] > 0.35 and stats["transparent"] > 0.15:
        score -= 100.0  # clásico robot verde de Android como recurso de relleno
    if stats["buckets"] <= 2:
        score -= 80.0
    elif stats["buckets"] <= 5:
        score -= 35.0
    else:
        score += min(25.0, stats["buckets"] / 4)
    if stats["variance"] < 250:
        score -= 40.0
    elif stats["variance"] > 2500:
        score += 15.0
    if stats["white"] > 0.8 and stats["buckets"] <= 6:
        score -= 35.0
    score += min(15.0, stats["opaque"] * 15)
    if stats["transparent"] > 0.9:
        score -= 30.0
    return score


def _extract_best_icon(zf: zipfile.ZipFile) -> tuple[bytes, str] | None:
    """Segunda pasada: elige la mejor imagen por nombre, tamaño y contenido."""
    candidates = []
    skip_roots = ("assets/", "lib/", "meta-inf/", "kotlin/", "okhttp3/", "org/", "com/", "androidx/")
    for info in zf.infolist():
        name = info.filename
        low = name.lower()
        if info.is_dir() or info.file_size < 300 or info.file_size > 3_000_000:
            continue
        if low.startswith(skip_roots) or low.endswith(".9.png"):
            continue
        try:
            with zf.open(info) as handle:
                head = handle.read(24)
        except (KeyError, RuntimeError, zipfile.BadZipFile, OSError):
            continue
        extension = _is_image(head)
        if not extension:
            continue
        candidates.append((_icon_name_score(name), name, extension))
    candidates.sort(reverse=True)

    best: tuple[float, bytes, str] | None = None
    for base_score, name, extension in candidates[:60]:
        try:
            data = zf.read(name)
        except (KeyError, RuntimeError, zipfile.BadZipFile, OSError):
            continue
        dims = _image_dimensions(data)
        if not dims:
            continue
        width, height = dims
        if width < 24 or height < 24:
            continue
        final = float(base_score)
        if width == height:
            final += 14
        if 48 <= width <= 512:
            final += 10
        elif width > 512:
            final -= 12
        else:
            final -= 8
        final += min(width, 256) / 16
        if extension == ".png":
            stats = _png_stats(data)
            final += _png_content_adjust(stats)
            if stats and stats["green"] > 0.35 and stats["transparent"] > 0.15:
                continue
            if stats and stats["buckets"] <= 2 and stats["variance"] < 100:
                continue
        if best is None or final > best[0]:
            best = (final, data, extension)
    if best:
        return best[1], best[2]
    return None


def _icon_from_zip(zf: zipfile.ZipFile) -> tuple[bytes, str] | None:
    resolved, _blocked = _extract_manifest_icon(zf)
    if resolved:
        return resolved
    # Si el manifiesto apunta a un icono adaptativo/vectorial sin capa
    # raster, todavía puede haber un PNG del lanzador en res/. Es mejor
    # intentarlo antes de quedarnos con el monograma.
    return _extract_best_icon(zf)


def extract_app_icon(apk_path: Path) -> tuple[bytes, str] | None:
    """Busca el icono del lanzador dentro del APK sin herramientas externas."""
    try:
        with zipfile.ZipFile(apk_path) as zf:
            return _icon_from_zip(zf)
    except (zipfile.BadZipFile, OSError) as exc:
        print(f"[FireDeck] No pude leer el APK para el icono: {exc}")
    return None


def _adb_pull(serial: str, remote: str, local: Path, timeout: int = 300) -> tuple[int, str]:
    """Pull sin el lock global: una descarga larga no debe congelar el mando."""
    try:
        proc = subprocess.run(
            [ADB, "-s", serial, "pull", remote, str(local)],
            capture_output=True,
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise AdbError("La descarga desde el Fire TV falló o tardó demasiado.") from exc
    return proc.returncode, _decode(proc.stdout + b"\n" + proc.stderr)


# --- Lectura parcial del APK: solo se pide al Fire TV lo que el ZIP necesita ---

ICON_RANGE_BLOCK = 64 * 1024
ICON_RANGE_BUDGET = 48 * 1024 * 1024
ICON_RANGE_MAX_REQUESTS = 40
ICON_RANGE_MIN_READAHEAD = 4
ICON_RANGE_MAX_READAHEAD = 64
ICON_RANGE_DISABLED = False


class RemoteReadError(AdbError):
    """La lectura por rangos no sirvió; hay que volver a descargar el archivo entero.

    ``integrity`` marca el caso en que adb devolvió menos o más bytes de los
    pedidos (típico del adb de Windows con ``exec-out``): no merece reintento.
    """

    def __init__(self, message: str, integrity: bool = False):
        super().__init__(message)
        self.integrity = integrity


class RemoteRangeFile(io.RawIOBase):
    """Archivo de solo lectura que va pidiendo trozos de un fichero remoto.

    ``zipfile`` solo necesita el final del ZIP (directorio central) y las
    entradas que se le piden, así que leer un icono cuesta unos cientos de KB
    en lugar de todo el APK. Los trozos se piden alineados a ``block`` bytes,
    con lectura adelantada creciente cuando el acceso es secuencial.
    """

    def __init__(self, size: int, fetch, block: int = ICON_RANGE_BLOCK,
                 budget: int = ICON_RANGE_BUDGET, max_requests: int = ICON_RANGE_MAX_REQUESTS):
        super().__init__()
        self._size = size
        self._fetch = fetch
        self._block = block
        self._budget = budget
        self._max_requests = max_requests
        self._blocks: dict[int, bytes] = {}
        self._pos = 0
        self._readahead = ICON_RANGE_MIN_READAHEAD
        self._next_block: int | None = None
        self.fetched = 0
        self.requests = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            target = offset
        elif whence == io.SEEK_CUR:
            target = self._pos + offset
        elif whence == io.SEEK_END:
            target = self._size + offset
        else:
            raise ValueError("whence inválido")
        if target < 0:
            raise OSError(22, "Posición negativa")
        self._pos = target
        return target

    def read(self, size: int | None = -1) -> bytes:
        if size is None or size < 0:
            size = max(0, self._size - self._pos)
        end = min(self._pos + size, self._size)
        if end <= self._pos:
            return b""
        first = self._pos // self._block
        last = (end - 1) // self._block
        self._load(first, last)
        data = b"".join(self._blocks[index] for index in range(first, last + 1))
        start = self._pos - first * self._block
        chunk = data[start:start + (end - self._pos)]
        self._pos = end
        return chunk

    def _load(self, first: int, last: int) -> None:
        total_blocks = -(-self._size // self._block)
        index = first
        while index <= last:
            if index in self._blocks:
                index += 1
                continue
            run_end = index
            while run_end + 1 <= last and run_end + 1 not in self._blocks:
                run_end += 1
            if self._next_block == index:
                self._readahead = min(self._readahead * 2, ICON_RANGE_MAX_READAHEAD)
            else:
                self._readahead = ICON_RANGE_MIN_READAHEAD
            if run_end == total_blocks - 1 and not self._blocks:
                # Primer acceso al final del ZIP: el directorio central está
                # justo antes, así que se pide de una vez.
                start = min(index, max(0, total_blocks - ICON_RANGE_MIN_READAHEAD))
                stop = total_blocks
            else:
                start = index
                stop = min(max(run_end + 1, index + self._readahead), total_blocks)
                for probe in range(index + 1, stop):
                    if probe in self._blocks:
                        stop = probe
                        break
            self._fetch_blocks(start, stop)
            self._next_block = stop

    def _fetch_blocks(self, start: int, stop: int) -> None:
        offset = start * self._block
        length = min(stop * self._block, self._size) - offset
        if self.requests >= self._max_requests:
            raise RemoteReadError("Demasiadas lecturas parciales del APK.")
        if self.fetched + length > self._budget:
            raise RemoteReadError("La lectura parcial del APK pide demasiados datos.")
        data = self._fetch(offset, length)
        if len(data) != length:
            raise RemoteReadError(
                f"adb devolvió {len(data)} bytes en vez de {length}.", integrity=True
            )
        self.requests += 1
        self.fetched += length
        for position in range(start, stop):
            piece = data[(position - start) * self._block:(position - start + 1) * self._block]
            self._blocks[position] = piece


def _adb_range_fetcher(serial: str, remote: str, block: int, timeout: int = 60):
    """Devuelve fetch(offset, length) que lee bytes del Fire TV con ``dd``."""

    def fetch(offset: int, length: int) -> bytes:
        if offset % block:
            raise RemoteReadError("Lectura parcial no alineada.")
        count = -(-length // block)
        command = (
            f"dd if={sh_quote(remote)} bs={block} skip={offset // block} "
            f"count={count} 2>/dev/null"
        )
        try:
            proc = subprocess.run(
                [ADB, "-s", serial, "exec-out", command],
                capture_output=True,
                timeout=timeout,
                creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise RemoteReadError("La lectura parcial del APK falló o tardó demasiado.") from exc
        if proc.returncode != 0:
            raise RemoteReadError(_decode(proc.stderr) or "adb no pudo leer el APK por partes.")
        return proc.stdout

    return fetch


def icon_from_remote_apk(serial: str, remote: str, size: int) -> tuple[bytes, str] | None:
    """Extrae el icono sin bajar el APK. Lanza RemoteReadError si hay que bajarlo entero."""
    source = RemoteRangeFile(size, _adb_range_fetcher(serial, remote, ICON_RANGE_BLOCK))
    try:
        with zipfile.ZipFile(source) as zf:
            return _icon_from_zip(zf)
    except zipfile.BadZipFile as exc:
        raise RemoteReadError(f"No pude leer el ZIP por partes: {exc}") from exc


def remote_file_size(remote: str) -> int | None:
    try:
        rc, text = device_shell_quiet(f"wc -c < {sh_quote(remote)}", timeout=10)
    except AdbError:
        return None
    text = text.strip()
    return int(text) if rc == 0 and text.isdigit() else None


def app_icon(package: str) -> tuple[bytes, str] | None:
    """Devuelve (bytes, extensión) del icono, usando caché en disco."""
    global ICON_RANGE_DISABLED
    package = normalize_package(package)
    if not valid_package(package):
        raise AdbError("Nombre de paquete inválido.")
    missed = ICON_MISSES.get(package)
    if missed and time.time() - missed < ICON_MISS_TTL:
        return None

    with _icon_lock(package):
        missed = ICON_MISSES.get(package)
        if missed and time.time() - missed < ICON_MISS_TTL:
            return None
        serial = pick_serial()
        try:
            rc, out = device_shell_quiet(f"pm path {sh_quote(package)}", timeout=15)
            paths = [
                line.split(":", 1)[1].strip()
                for line in out.splitlines()
                if line.startswith("package:") and ":" in line
            ]
            if not paths:
                raise AdbError("No encontré el APK de esa app.")
            apk = next((path for path in paths if path.endswith("/base.apk")), paths[0])
            digest = hashlib.sha1(
                (apk + "|" + ICON_CACHE_VERSION).encode("utf-8", "replace")
            ).hexdigest()[:10]
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", package)
            ICON_DIR.mkdir(parents=True, exist_ok=True)
            for ext in (".png", ".webp", ".jpg", ".svg"):
                cached = ICON_DIR / f"{safe}-{digest}{ext}"
                if cached.is_file():
                    return cached.read_bytes(), ext

            size = remote_file_size(apk)
            extracted = None
            partial = False
            if size and not ICON_RANGE_DISABLED:
                # Primero se intenta leer solo los trozos necesarios del APK.
                try:
                    with ICON_SEMAPHORE:
                        extracted = icon_from_remote_apk(serial, apk, size)
                    partial = True
                except RemoteReadError as exc:
                    if exc.integrity:
                        ICON_RANGE_DISABLED = True
                    print(f"[FireDeck] Icono de {package}: lectura parcial descartada ({exc})")
            if not partial:
                if size and size > ICON_MAX_APK:
                    raise AdbError("El APK es demasiado grande para extraer el icono.")
                with tempfile.TemporaryDirectory(prefix="firedeck_icon_") as tmpdir:
                    local = Path(tmpdir) / "icon.apk"
                    with ICON_SEMAPHORE:
                        rc, text = _adb_pull(serial, apk, local, timeout=180)
                    if rc != 0 or not local.is_file() or local.stat().st_size == 0:
                        raise AdbError(text or "No pude descargar el APK para leer el icono.")
                    extracted = extract_app_icon(local)
            if not extracted:
                raise AdbError("El APK no contiene un icono utilizable.")
            data, ext = extracted
            try:
                (ICON_DIR / f"{safe}-{digest}{ext}").write_bytes(data)
            except OSError:
                pass
            ICON_MISSES.pop(package, None)
            return data, ext
        except (AdbError, OSError, zipfile.BadZipFile) as exc:
            print(f"[FireDeck] Icono de {package}: {exc}")
            ICON_MISSES[package] = time.time()
            return None


def pull_package_apks(package: str, dest: Path) -> list[Path]:
    """Descarga el APK base y sus splits (si los hay) a una carpeta temporal."""
    package = normalize_package(package)
    if not valid_package(package):
        raise AdbError("Nombre de paquete inválido.")
    serial = pick_serial()
    rc, out = device_shell_quiet(f"pm path {sh_quote(package)}", timeout=15)
    paths = [
        line.split(":", 1)[1].strip()
        for line in out.splitlines()
        if line.startswith("package:") and ":" in line
    ]
    if not paths:
        raise AdbError("No encontré el APK de esa app. Puede ser una app del sistema protegida.")
    paths.sort(key=lambda path: (not path.endswith("/base.apk"), path))
    files: list[Path] = []
    for index, remote in enumerate(paths):
        name = "base.apk" if remote.endswith("/base.apk") else f"split-{index}.apk"
        local = dest / name
        rc, text = _adb_pull(serial, remote, local, timeout=900)
        if rc != 0 or not local.is_file() or local.stat().st_size == 0:
            raise AdbError(text or f"No pude descargar {remote}.")
        files.append(local)
    return files


def human_size(kilobytes: float) -> str:
    size = float(kilobytes) * 1024
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            precision = 0 if unit == "B" else 1
            return f"{size:.{precision}f} {unit}"
        size /= 1024
    return ""


def app_details(package: str) -> dict:
    """Ficha de una app: versión, fecha de instalación, tamaño y APK."""
    package = normalize_package(package)
    if not valid_package(package):
        raise AdbError("Nombre de paquete inválido.")

    details = describe_app(package)
    details.update(
        {"version": "", "first_install": "", "last_update": "", "size": "",
         "target_sdk": "", "min_sdk": "", "apk": "", "activity": "",
         "split_count": 0}
    )

    try:
        rc, out = device_shell_quiet(f"pm path {sh_quote(package)}", timeout=15)
        apks = [
            line.split(":", 1)[1].strip()
            for line in out.splitlines()
            if line.startswith("package:") and ":" in line
        ]
    except AdbError:
        apks = []
    if apks:
        details["apk"] = next((path for path in apks if path.endswith("/base.apk")), apks[0])
        details["split_count"] = len(apks)

    try:
        rc, dump = device_shell_quiet(f"dumpsys package {sh_quote(package)}", timeout=40)
    except AdbError:
        dump = ""

    def grab(pattern: str) -> str:
        match = re.search(pattern, dump)
        return match.group(1).strip() if match else ""

    details["version"] = grab(r"versionName=(\S+)")
    details["first_install"] = grab(r"firstInstallTime=([^\n]+)")
    details["last_update"] = grab(r"lastUpdateTime=([^\n]+)")
    details["target_sdk"] = grab(r"targetSdk=(\d+)")
    details["min_sdk"] = grab(r"minSdk=(\d+)")

    if details["apk"]:
        directory = details["apk"].rsplit("/", 1)[0]
        try:
            rc, du = device_shell_quiet(f"du -sk {sh_quote(directory)} 2>/dev/null", timeout=25)
            match = re.match(r"(\d+)", du.strip())
            if match:
                details["size"] = human_size(int(match.group(1)))
        except AdbError:
            pass

    try:
        rc, out = device_shell_quiet(
            "cmd package resolve-activity --brief -a android.intent.action.MAIN "
            "-c android.intent.category.LAUNCHER " + sh_quote(package),
            timeout=15,
        )
        candidates = [
            line.strip() for line in out.splitlines()
            if line.strip() and "/" in line and not line.strip().startswith("priority=")
        ]
        if candidates:
            details["activity"] = candidates[-1]
    except AdbError:
        pass

    return details


def launch_package(package: str) -> str:
    rc, out = device_shell_quiet(
        f"monkey -p {sh_quote(package)} -c android.intent.category.LAUNCHER 1",
        timeout=25,
    )
    if rc != 0 or "No activities found" in out or "Error" in out:
        raise AdbError(out.strip() or f"No se pudo abrir {package}.")
    return out.strip()


def app_action(package: str, action: str, confirm_data: bool = False) -> str:
    package = normalize_package(package)
    if not valid_package(package):
        raise AdbError("Nombre de paquete inválido.")
    if action == "stop":
        return device_shell(f"am force-stop {package}")
    if action == "clear-data":
        if not confirm_data:
            raise AdbError("La operación borra todos los datos de la app y requiere confirmación.")
        rc, out = device_shell_quiet(f"pm clear {package}")
        if rc != 0 or "Success" not in out:
            raise AdbError(out or "No se pudieron borrar los datos.")
        return out
    if action == "uninstall":
        rc, out = device_shell_quiet(f"pm uninstall {package}", timeout=60)
        if rc != 0 or "Success" not in out:
            raise AdbError(out or "No se pudo desinstalar (¿es una app del sistema?).")
        return out
    if action == "info":
        return device_shell(
            f"dumpsys package {package} | grep -E 'versionName|firstInstallTime|lastUpdateTime'"
        )
    raise AdbError("Acción no soportada.")


def storage_info() -> dict:
    rc, out = device_shell_quiet("df -h /data", timeout=15)
    total = used = avail = ""
    for line in out.splitlines():
        if line.startswith("/dev") or " /data" in line:
            parts = line.split()
            if len(parts) >= 4:
                total, used, avail = parts[1], parts[2], parts[3]
    return {"raw": out, "total": total, "used": used, "available": avail}


def trim_caches(size: str = "512G") -> str:
    if not re.fullmatch(r"\d+[KMG]?", size):
        raise AdbError("Tamaño inválido. Ejemplo: 512G")
    rc, out = device_shell_quiet(f"pm trim-caches {size}", timeout=120)
    if rc != 0 or re.search(r"\b(error|unknown|exception|denied)\b", out, re.I):
        rc, out = device_shell_quiet(f"cmd package trim-caches {size}", timeout=120)
    if rc != 0 or re.search(r"\b(error|unknown|exception|denied)\b", out, re.I):
        raise AdbError(out or "El dispositivo no permite liberar caché por ADB.")
    return out or "Caché liberada. Android decidió qué apps recortar."


# --------------------------------------------------------------------------
# Apps del sistema: desactivar (debloat), reactivar y restaurar
#
# `pm disable-user` no borra ningún APK: solo marca el paquete como desactivado
# para el usuario 0. Es reversible con `pm enable`, pero desactivar la app
# equivocada puede dejar el Fire TV sin inicio o sin ajustes, así que FireDeck
# bloquea una lista de paquetes esenciales y guarda lo que ha desactivado en
# debloat.json (por dispositivo) para poder restaurarlo.
# --------------------------------------------------------------------------

DEBLOAT_FILE = ROOT / "debloat.json"
DEBLOAT_LOCK = threading.RLock()

CRITICAL_SYSTEM_PACKAGES = {
    "android",
    "com.android.systemui",
    "com.android.settings",
    "com.android.providers.settings",
    "com.android.packageinstaller",
    "com.google.android.packageinstaller",
    "com.android.permissioncontroller",
    "com.google.android.permissioncontroller",
    "com.android.shell",
    "com.android.phone",
    "com.android.bluetooth",
    "com.amazon.tv.launcher",
    "com.amazon.tv.settings",
    "com.amazon.tv.settings.core",
    "com.amazon.tv.settings.v2",
    "com.amazon.tv.quicksettings.ui",
    "com.amazon.tv.quicksettings.ui.sheldon.overlay",
    "com.amazon.firehomestarter",
    "com.amazon.webview.chromium",
    "com.amazon.device.settings",
    "com.amazon.firebat",
    "com.amazon.device.bluetoothpa",
    "com.amazon.wifilocker",
    "com.amazon.connectivitycontroller",
    "com.amazon.dcp",
}

CRITICAL_SYSTEM_REASONS = {
    "android": "es el marco de Android; sin él no arranca nada.",
    "com.android.systemui": "dibuja la interfaz y la barra del sistema.",
    "com.android.settings": "contiene los ajustes de Android.",
    "com.android.providers.settings": "guarda la configuración del sistema.",
    "com.android.packageinstaller": "instala y actualiza las apps.",
    "com.google.android.packageinstaller": "instala y actualiza las apps.",
    "com.android.permissioncontroller": "gestiona los permisos de las apps.",
    "com.google.android.permissioncontroller": "gestiona los permisos de las apps.",
    "com.android.shell": "es el intérprete que usa ADB; desactivarlo corta el control remoto.",
    "com.android.phone": "gestiona la conectividad del sistema.",
    "com.android.bluetooth": "conecta el mando Bluetooth del Fire TV.",
    "com.amazon.tv.launcher": "es el inicio del Fire TV.",
    "com.amazon.tv.settings": "contiene los ajustes del Fire TV.",
    "com.amazon.tv.settings.core": "contiene los ajustes del Fire TV.",
    "com.amazon.tv.settings.v2": "contiene los ajustes del Fire TV.",
    "com.amazon.tv.quicksettings.ui": "dibuja el menú rápido del mando.",
    "com.amazon.tv.quicksettings.ui.sheldon.overlay": "dibuja el menú rápido del mando.",
    "com.amazon.firehomestarter": "arranca el inicio del Fire TV.",
    "com.amazon.webview.chromium": "es el motor web que usan muchas apps.",
    "com.amazon.device.settings": "gestiona la configuración interna del dispositivo.",
    "com.amazon.firebat": "gestiona el mando Bluetooth del Fire TV.",
    "com.amazon.device.bluetoothpa": "gestiona el Bluetooth del Fire TV.",
    "com.amazon.wifilocker": "guarda las redes Wi-Fi configuradas.",
    "com.amazon.connectivitycontroller": "gestiona la conexión de red.",
    "com.amazon.dcp": "gestiona la cuenta y los servicios de Amazon.",
}

# Apps opcionales: desactivarlas no rompe el sistema. El valor explica por qué.
SAFE_SYSTEM_PACKAGES = {
    "com.amazon.bueller.music": "Reproductor de Amazon Music; el Fire TV funciona sin él.",
    "com.amazon.bueller.photos": "Galería de Amazon Photos; no es necesaria para el sistema.",
    "com.amazon.cloud9": "Navegador Amazon Silk; puedes usar otro navegador.",
    "com.amazon.gamehub": "Tienda y lanzador de juegos de Amazon; opcional.",
    "com.amazon.shoptv.client": "Compras de Amazon; no afecta al sistema.",
    "com.amazon.shoptv.firetv.client": "Compras de Amazon; no afecta al sistema.",
    "com.amazon.website_launcher": "Lanzador de la web de Amazon; opcional.",
    "com.amazon.mp3": "Reproductor de Amazon Music; opcional.",
    "com.amazon.kindle": "Lector Kindle; opcional.",
    "com.amazon.photos": "Galería de Amazon Photos; opcional.",
    "com.amazon.firetv.youtube": "YouTube preinstalado; puedes usar otra app.",
    "com.amazon.avod": "Prime Video; opcional si no usas el servicio.",
    "com.amazon.avod.thirdpartyclient": "Prime Video; opcional si no usas el servicio.",
    "com.amazon.amazonvideo.livingroom": "Prime Video; opcional si no usas el servicio.",
}

CAUTION_SYSTEM_REASONS = {
    "com.amazon.venezia": "Es la tienda de apps: sin ella no podrás instalar desde la tienda.",
    "com.amazon.device.software.ota": "Gestiona las actualizaciones de Fire OS.",
    "com.amazon.device.software.ota.override": "Ajusta las actualizaciones de Fire OS.",
    "com.amazon.dialservice": "Permite enviar contenido desde el móvil (casting).",
    "com.amazon.dee.app": "Asistente Alexa; opcional, pero integrado en el sistema.",
    "com.amazon.device.messaging": "Mensajería push de Amazon (ADM).",
    "com.amazon.tv.devicecontrolsettings": "Ajustes de control del televisor y HDMI-CEC.",
}


def system_app_safety(package: str) -> tuple[str, str]:
    """Devuelve el nivel de riesgo y una explicación para una app del sistema."""
    if package in CRITICAL_SYSTEM_PACKAGES:
        reason = CRITICAL_SYSTEM_REASONS.get(
            package, "Sin este paquete el Fire TV puede dejar de responder."
        )
        return "critical", reason
    if package in SAFE_SYSTEM_PACKAGES:
        return "safe", SAFE_SYSTEM_PACKAGES[package]
    if package in CAUTION_SYSTEM_REASONS:
        return "caution", CAUTION_SYSTEM_REASONS[package]
    return (
        "caution",
        "Servicio del sistema. Desactívalo solo si sabes exactamente para qué sirve.",
    )


def _read_debloat_payload() -> dict:
    try:
        raw = DEBLOAT_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"version": 1, "devices": {}}
    except OSError as exc:
        print(f"[FireDeck] No pude leer {DEBLOAT_FILE.name}: {exc}")
        return {"version": 1, "devices": {}}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(f"[FireDeck] {DEBLOAT_FILE.name} no es JSON válido; se ignora.")
        return {"version": 1, "devices": {}}
    if not isinstance(data, dict):
        return {"version": 1, "devices": {}}
    if not isinstance(data.get("devices"), dict):
        data["devices"] = {}
    return data


def _write_debloat_payload(payload: dict):
    tmp = DEBLOAT_FILE.with_name(DEBLOAT_FILE.name + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(DEBLOAT_FILE)
    except OSError as exc:
        raise AdbError(f"No pude guardar el registro de apps desactivadas: {exc}") from exc


def debloat_records(serial: str | None = None) -> dict[str, dict]:
    """Apps que FireDeck desactivó en este dispositivo, indexadas por paquete."""
    serial = serial or pick_serial()
    with DEBLOAT_LOCK:
        devices = _read_debloat_payload().get("devices") or {}
        records = devices.get(serial) or {}
        if not isinstance(records, dict):
            return {}
        return {
            package: dict(record) if isinstance(record, dict) else {}
            for package, record in records.items()
            if valid_package(package)
        }


def remember_debloat_package(package: str, name: str = "", serial: str | None = None):
    serial = serial or pick_serial()
    with DEBLOAT_LOCK:
        payload = _read_debloat_payload()
        devices = payload.setdefault("devices", {})
        records = devices.get(serial)
        if not isinstance(records, dict):
            records = {}
            devices[serial] = records
        records[package] = {"name": name, "at": int(time.time())}
        _write_debloat_payload(payload)


def move_debloat_records(old_serial: str, new_serial: str) -> int:
    """Un Fire TV cambió de IP: traslada lo desactivado a su dirección nueva.

    Devuelve cuántas apps se trasladaron. Si la dirección nueva ya tenía un
    registro de la misma app, se conserva el que había.
    """
    old_serial = str(old_serial or "").strip()
    new_serial = str(new_serial or "").strip()
    if not old_serial or not new_serial or old_serial == new_serial:
        return 0
    with DEBLOAT_LOCK:
        payload = _read_debloat_payload()
        devices = payload.setdefault("devices", {})
        old = devices.get(old_serial)
        if not isinstance(old, dict) or not old:
            return 0
        new = devices.get(new_serial)
        if not isinstance(new, dict):
            new = {}
        moved = 0
        for package, record in old.items():
            if package not in new:
                new[package] = record
                moved += 1
        devices[new_serial] = new
        devices.pop(old_serial, None)
        try:
            _write_debloat_payload(payload)
        except AdbError as exc:
            print(f"[FireDeck] {exc}")
            return 0
        return moved


def forget_debloat_package(package: str, serial: str | None = None):
    serial = serial or pick_serial()
    with DEBLOAT_LOCK:
        payload = _read_debloat_payload()
        devices = payload.setdefault("devices", {})
        records = devices.get(serial) or {}
        if package not in records:
            return
        records.pop(package, None)
        if not records:
            devices.pop(serial, None)
        _write_debloat_payload(payload)


def _disabled_packages() -> set[str]:
    rc, out = device_shell_quiet("pm list packages -d", timeout=60)
    if rc != 0:
        raise AdbError(out or "No pude consultar las apps desactivadas.")
    disabled = set()
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("package:"):
            package = normalize_package(line[len("package:"):])
            if valid_package(package):
                disabled.add(package)
    return disabled


def list_system_apps() -> dict:
    """Apps del sistema con su estado activo/desactivado y su nivel de riesgo."""
    serial = pick_serial()
    rc, out = device_shell_quiet("pm list packages -s", timeout=90)
    if rc != 0:
        raise AdbError(out or "No pude leer las apps del sistema.")

    packages = []
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("package:"):
            package = normalize_package(line[len("package:"):])
            if valid_package(package):
                packages.append(package)
    packages = sorted(set(packages), key=str.lower)

    disabled = _disabled_packages()
    tracked = debloat_records(serial)

    apps = []
    for package in packages:
        app = describe_app(package)
        safety, reason = system_app_safety(package)
        is_disabled = package in disabled
        apps.append(
            {
                **app,
                "safety": safety,
                "safety_reason": reason,
                "state": "disabled" if is_disabled else "active",
                "tracked": package in tracked,
            }
        )

    apps.sort(key=lambda app: (app["name"].casefold(), app["package"]))

    counts = {
        "total": len(apps),
        "safe": sum(1 for app in apps if app["safety"] == "safe"),
        "critical": sum(1 for app in apps if app["safety"] == "critical"),
        "caution": sum(1 for app in apps if app["safety"] == "caution"),
        "disabled": sum(1 for app in apps if app["state"] == "disabled"),
        "tracked": sum(1 for app in apps if app["tracked"] and app["state"] == "disabled"),
    }
    return {"apps": apps, "counts": counts, "serial": serial}


def set_system_app_state(package: str, enabled: bool, confirm: bool = False) -> str:
    """Activa o desactiva una app del sistema, con protección de paquetes críticos."""
    package = normalize_package(package)
    if not valid_package(package):
        raise AdbError("Nombre de paquete inválido.")

    name = describe_app(package)["name"]
    disabled = _disabled_packages()

    if enabled:
        if package not in disabled:
            forget_debloat_package(package)
            return f"{name} ya estaba activa."
        rc, out = device_shell_quiet(f"pm enable {package}", timeout=30)
        text = out.strip()
        if package in _disabled_packages():
            raise AdbError(text or f"No se pudo activar {name} ({package}).")
        forget_debloat_package(package)
        return f"{name} activada. Ya vuelve a estar disponible."

    if not confirm:
        raise AdbError("Desactivar una app del sistema requiere confirmación.")
    if package in CRITICAL_SYSTEM_PACKAGES:
        reason = CRITICAL_SYSTEM_REASONS.get(package, "es esencial para el sistema")
        raise AdbError(
            f"«{name}» está protegida por FireDeck: {reason} "
            "Desactivarla podría dejar el Fire TV inutilizable."
        )
    if package in disabled:
        remember_debloat_package(package, name)
        return f"{name} ya estaba desactivada."

    rc, out = device_shell_quiet(f"pm disable-user --user 0 {package}", timeout=30)
    text = out.strip()
    if package not in _disabled_packages():
        # Fire OS antiguos no aceptan `--user`; se reintenta la forma clásica.
        rc, out = device_shell_quiet(f"pm disable-user {package}", timeout=30)
        text = (text + "\n" + out.strip()).strip()
    if package not in _disabled_packages():
        raise AdbError(text or f"No se pudo desactivar {name} ({package}).")

    remember_debloat_package(package, name)
    return (
        f"{name} desactivada. El APK sigue en el Fire TV: puedes reactivarla "
        "desde esta misma pestaña."
    )


def restore_system_apps() -> dict:
    """Reactiva todas las apps que FireDeck desactivó en el dispositivo actual."""
    serial = pick_serial()
    tracked = debloat_records(serial)
    if not tracked:
        raise AdbError("No hay apps desactivadas desde FireDeck en este dispositivo.")

    enabled = []
    failed = []
    for package in sorted(tracked, key=str.lower):
        try:
            rc, out = device_shell_quiet(f"pm enable {package}", timeout=30)
            if package in _disabled_packages():
                failed.append({"package": package, "error": out.strip() or "No se activó."})
                continue
            forget_debloat_package(package, serial)
            enabled.append(package)
        except AdbError as exc:
            failed.append({"package": package, "error": str(exc)})

    if not enabled and failed:
        raise AdbError(
            "No se pudo reactivar ninguna app: "
            + "; ".join(f"{item['package']}: {item['error']}" for item in failed[:3])
        )
    message = f"{len(enabled)} app(s) reactivada(s)."
    if failed:
        message += f" {len(failed)} no se pudo(ron) reactivar."
    return {"message": message, "enabled": enabled, "failed": failed}


# --------------------------------------------------------------------------
# Gestor de archivos dentro de /sdcard
# --------------------------------------------------------------------------

FILE_ROOT = "/sdcard"


def normalize_remote_path(value) -> str:
    """Resuelve la ruta y la mantiene dentro de /sdcard (sin permitir «..»)."""
    text = str(value or "").strip().replace("\\", "/")
    if "\x00" in text:
        raise AdbError("La ruta contiene un carácter no válido.")
    if not text:
        return FILE_ROOT
    if not text.startswith("/"):
        text = f"{FILE_ROOT}/{text}"
    parts: list[str] = []
    for piece in text.split("/"):
        if piece in ("", "."):
            continue
        if piece == "..":
            if not parts:
                raise AdbError("La ruta sale de /sdcard.")
            parts.pop()
            continue
        parts.append(piece)
    resolved = "/" + "/".join(parts)
    if resolved != FILE_ROOT and not resolved.startswith(FILE_ROOT + "/"):
        raise AdbError("Solo puedo gestionar archivos dentro de /sdcard.")
    return resolved


def clean_file_name(name) -> str:
    text = str(name or "").strip()
    if not text:
        raise AdbError("Falta el nombre del archivo.")
    if text in (".", "..") or "/" in text or "\\" in text or "\x00" in text:
        raise AdbError("El nombre no puede contener barras.")
    if len(text) > 200 or any(ord(char) < 32 for char in text):
        raise AdbError("Ese nombre no es válido.")
    return text


def parent_remote_path(path: str) -> str | None:
    if path == FILE_ROOT:
        return None
    return path.rsplit("/", 1)[0] or FILE_ROOT


def _remote_exists(path: str) -> bool:
    rc, out = device_shell_quiet(f"[ -e {sh_quote(path)} ] && echo firedeck-si", timeout=15)
    return "firedeck-si" in out


def _remote_size_bytes(path: str) -> int | None:
    rc, out = device_shell_quiet(f"stat -c %s {sh_quote(path)} 2>/dev/null", timeout=15)
    text = (out or "").strip()
    return int(text) if rc == 0 and text.isdigit() else None


def _remote_is_dir(path: str) -> bool:
    rc, out = device_shell_quiet(f"[ -d {sh_quote(path)} ] && echo firedeck-dir", timeout=15)
    return "firedeck-dir" in out


def _parse_ls_line(line: str, parent: str) -> dict | None:
    parts = line.split(None, 7)
    if len(parts) < 8:
        return None
    mode, _links, _owner, _group, size_text, date_text, time_text, raw_name = parts
    name = raw_name
    link_target = ""
    if mode.startswith("l") and " -> " in name:
        name, link_target = name.split(" -> ", 1)
    if name in (".", "..") or not name:
        return None
    is_dir = mode.startswith("d")
    is_link = mode.startswith("l")
    try:
        size = int(size_text)
    except ValueError:
        size = 0
    date = f"{date_text} {time_text}"
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})", date)
    if match:
        year, month, day, hour, minute = match.groups()
        label = f"{day}/{month}/{year} {hour}:{minute}"
        iso = f"{year}-{month}-{day}T{hour}:{minute}"
    else:
        label = date
        iso = date
    return {
        "name": name,
        "path": f"{parent.rstrip('/')}/{name}" if parent != "/" else f"/{name}",
        "dir": is_dir,
        "link": is_link,
        "target": link_target,
        "size": size,
        "date": label,
        "mtime": iso,
    }


def list_files(path=None) -> dict:
    resolved = normalize_remote_path(path)
    listing_path = resolved if resolved.endswith("/") else resolved + "/"
    rc, out = device_shell_quiet(f"ls -la {sh_quote(listing_path)}", timeout=30)
    text = out or ""
    if rc != 0 or text.lstrip().startswith("ls:") or "No such file" in text:
        raise AdbError(
            f"No pude leer {resolved}. "
            + (text.strip() or "Comprueba que la carpeta exista y sea accesible.")
        )
    entries = []
    for line in text.splitlines():
        if not line.strip() or line.startswith("total "):
            continue
        entry = _parse_ls_line(line, resolved)
        if entry:
            entries.append(entry)
    entries.sort(key=lambda item: (not item["dir"], item["name"].casefold()))
    return {"path": resolved, "parent": parent_remote_path(resolved), "entries": entries}


def download_remote_file(path) -> tuple[Path, str]:
    resolved = normalize_remote_path(path)
    if resolved == FILE_ROOT:
        raise AdbError("Elige un archivo, no la carpeta raíz.")
    if _remote_is_dir(resolved):
        raise AdbError("Esa ruta es una carpeta. Entra en ella y descarga sus archivos.")
    serial = pick_serial()
    filename = resolved.rsplit("/", 1)[-1] or "archivo"
    fd, tmp_name = tempfile.mkstemp(prefix="firedeck_file_")
    os.close(fd)
    local = Path(tmp_name)
    rc, text = _adb_pull(serial, resolved, local, timeout=900)
    if rc != 0 or not local.is_file():
        local.unlink(missing_ok=True)
        raise AdbError(text or "No pude descargar el archivo del Fire TV.")
    return local, filename


def upload_remote_file(directory, filename, data: bytes, overwrite: bool = False) -> dict:
    resolved_dir = normalize_remote_path(directory)
    name = clean_file_name(filename)
    if not _remote_exists(resolved_dir):
        raise AdbError("La carpeta de destino ya no existe. Actualiza la lista.")
    target = f"{resolved_dir.rstrip('/')}/{name}"
    if _remote_is_dir(target):
        raise AdbError(
            f"Ya existe una carpeta llamada «{name}» en esta carpeta. Elige otro nombre."
        )
    if _remote_exists(target) and not overwrite:
        raise AdbError(
            f"Ya existe «{name}» en esta carpeta. Renómbralo o confirma que quieres reemplazarlo."
        )
    serial = pick_serial()
    suffix = Path(name).suffix[:12]
    fd, tmp_name = tempfile.mkstemp(prefix="firedeck_up_", suffix=suffix)
    os.close(fd)
    local = Path(tmp_name)
    try:
        local.write_bytes(data)
        timeout = min(900, max(120, len(data) // 40_000))
        rc, out, err = adb_run(
            ["-s", serial, "push", str(local), target], timeout=timeout
        )
        text = (out + "\n" + err).strip()
        if rc != 0 or "1 file pushed" not in text.lower():
            raise AdbError(text or "No pude subir el archivo al Fire TV.")
        remote_size = _remote_size_bytes(target)
        if remote_size is not None and remote_size != len(data):
            raise AdbError(
                f"La copia llegó incompleta ({remote_size} de {len(data)} bytes). "
                "Vuelve a intentarlo."
            )
    finally:
        local.unlink(missing_ok=True)
    return {"path": target, "size": len(data), "name": name}


def make_remote_directory(path) -> str:
    resolved = normalize_remote_path(path)
    if resolved == FILE_ROOT:
        raise AdbError("La carpeta raíz ya existe.")
    if _remote_exists(resolved):
        raise AdbError("Ya existe un archivo o carpeta con ese nombre.")
    parent = parent_remote_path(resolved)
    if not parent or not _remote_exists(parent):
        raise AdbError("La carpeta contenedora ya no existe. Actualiza la lista.")
    rc, out = device_shell_quiet(f"mkdir -p {sh_quote(resolved)}", timeout=30)
    if rc != 0 or not _remote_exists(resolved):
        raise AdbError(out or "No pude crear la carpeta.")
    return resolved


def rename_remote_path(path, new_name) -> str:
    resolved = normalize_remote_path(path)
    if resolved == FILE_ROOT:
        raise AdbError("No se puede renombrar la carpeta raíz.")
    name = clean_file_name(new_name)
    parent = parent_remote_path(resolved) or FILE_ROOT
    target = f"{parent.rstrip('/')}/{name}"
    if target == resolved:
        return resolved
    if not _remote_exists(resolved):
        raise AdbError("Ese archivo ya no existe. Actualiza la lista.")
    if _remote_exists(target):
        raise AdbError("Ya existe otro archivo o carpeta con ese nombre.")
    rc, out = device_shell_quiet(
        f"mv {sh_quote(resolved)} {sh_quote(target)}", timeout=60
    )
    if rc != 0 or not _remote_exists(target):
        raise AdbError(out or "No pude renombrar.")
    return target


def delete_remote_path(path, confirm: bool = False) -> str:
    resolved = normalize_remote_path(path)
    if resolved == FILE_ROOT:
        raise AdbError("No se puede borrar la carpeta raíz de /sdcard.")
    if not confirm:
        raise AdbError("Borrar requiere confirmación.")
    if not _remote_exists(resolved):
        raise AdbError("Ese archivo ya no existe. Actualiza la lista.")
    rc, out = device_shell_quiet(f"rm -rf {sh_quote(resolved)}", timeout=120)
    if rc != 0:
        raise AdbError(out or "No pude borrar. Puede que Android proteja esa carpeta.")
    if _remote_exists(resolved):
        raise AdbError(
            out or "Android no permitió borrar ese elemento (puede estar protegido)."
        )
    return resolved


# --------------------------------------------------------------------------
# Instalación de APK y paquetes divididos (.apks / XAPK / APKM)
# --------------------------------------------------------------------------

INSTALL_EXTENSIONS = (".apk", ".apks", ".xapk", ".apkm", ".zip")
INSTALL_MAX_UPLOAD = 3 * 1024 * 1024 * 1024       # 3 GiB por archivo subido
INSTALL_MAX_UNCOMPRESSED = 4 * 1024 * 1024 * 1024  # 4 GiB ya extraídos
INSTALL_MAX_ENTRY = 2 * 1024 * 1024 * 1024        # 2 GiB por APK
INSTALL_MAX_APKS = 200
APK_SIG_BLOCK_MAGIC = b"APK Sig Block 42"
# v2, v3 y v3.1 del esquema de firma APK.
APK_SIG_SCHEMES = (0x7109871A, 0xF05368C0, 0x1B93AD61)


def _manifest_attr_text(event: dict, name: str) -> str:
    attr = event.get("attrs", {}).get(name)
    if not attr:
        return ""
    raw, dtype, data = attr
    strings = event.get("strings") or []
    if dtype == 0x03:
        index = raw if raw >= 0 else data
        if 0 <= index < len(strings):
            return strings[index]
    if 0 <= raw < len(strings):
        return strings[raw]
    return ""


def _manifest_attr_int(event: dict, name: str) -> int | None:
    attr = event.get("attrs", {}).get(name)
    if not attr:
        return None
    raw, dtype, data = attr
    if dtype in (0x10, 0x11, 0x12, 0x13):
        return int(data)
    if raw >= 0:
        return int(raw)
    return None


def apk_manifest_info(path: Path) -> dict:
    """Lee paquete, split, versión y minSdk del manifiesto binario del APK."""
    try:
        with zipfile.ZipFile(path) as zf:
            info = zf.getinfo("AndroidManifest.xml")
            if info.file_size > 20 * 1024 * 1024:
                raise AdbError("El AndroidManifest.xml del APK es demasiado grande.")
            manifest = zf.read(info)
    except (KeyError, zipfile.BadZipFile, OSError) as exc:
        raise AdbError("El APK no contiene un AndroidManifest.xml válido.") from exc
    try:
        events = _parse_binary_xml(manifest)
    except (struct.error, IndexError, ValueError) as exc:
        raise AdbError("No pude leer el AndroidManifest.xml del APK.") from exc
    # minSdkVersion no va en <manifest> sino en su hijo <uses-sdk>.
    min_sdk = next(
        (
            _manifest_attr_int(event, "minSdkVersion")
            for event in events
            if event.get("name") == "uses-sdk" and not event.get("end")
        ),
        None,
    )
    for event in events:
        if event.get("name") != "manifest" or event.get("end"):
            continue
        package = _manifest_attr_text(event, "package").strip()
        if not package:
            break
        return {
            "package": package,
            "split": _manifest_attr_text(event, "split").strip(),
            "version_name": _manifest_attr_text(event, "versionName").strip(),
            "version_code": _manifest_attr_int(event, "versionCode"),
            "min_sdk": min_sdk,
        }
    raise AdbError("El APK no tiene un manifiesto de Android reconocible.")


def _der_element(data: bytes, pos: int) -> tuple[int, int, int, int]:
    """Devuelve (tag, inicio del contenido, fin del contenido, siguiente)."""
    if pos + 2 > len(data):
        raise ValueError("DER truncado")
    tag = data[pos]
    pos += 1
    length = data[pos]
    pos += 1
    if length & 0x80:
        count = length & 0x7F
        if count == 0 or pos + count > len(data):
            raise ValueError("longitud DER inválida")
        length = int.from_bytes(data[pos:pos + count], "big")
        pos += count
    end = pos + length
    if end > len(data):
        raise ValueError("DER truncado")
    return tag, pos, end, end


def _der_children(data: bytes, start: int, end: int):
    pos = start
    while pos < end:
        tag, child_start, child_end, next_pos = _der_element(data, pos)
        yield tag, child_start, child_end, pos, next_pos
        pos = next_pos


def _v1_signer_certificate(der: bytes) -> bytes:
    """Extrae el primer certificado de un PKCS#7 de META-INF (firma v1)."""
    tag, start, end, _ = _der_element(der, 0)
    if tag != 0x30:
        return b""
    children = list(_der_children(der, start, end))
    if len(children) < 2 or children[1][0] != 0xA0:
        return b""
    tag, signed_start, signed_end, _ = _der_element(der, children[1][1])
    if tag != 0x30:
        return b""
    for child_tag, child_start, child_end, _, _ in _der_children(der, signed_start, signed_end):
        if child_tag != 0xA0:
            continue
        for cert_tag, _, _, cert_start, cert_end in _der_children(der, child_start, child_end):
            if cert_tag == 0x30:
                return der[cert_start:cert_end]
    return b""


def _apk_signing_block(path: Path) -> dict[int, bytes]:
    """Pares id->valor del APK Signing Block (firmas v2/v3/v3.1)."""
    size = path.stat().st_size
    if size < 32:
        return {}
    tail_size = min(size, 1024 * 1024)
    with path.open("rb") as fh:
        fh.seek(size - tail_size)
        tail = fh.read(tail_size)
    eocd = tail.rfind(b"PK\x05\x06")
    if eocd < 0 or eocd + 22 > len(tail):
        return {}
    comment = struct.unpack_from("<H", tail, eocd + 20)[0]
    if eocd + 22 + comment != len(tail):
        return {}
    cd_offset = struct.unpack_from("<I", tail, eocd + 16)[0]
    if cd_offset < 32 or cd_offset > size:
        return {}
    with path.open("rb") as fh:
        fh.seek(cd_offset - 24)
        footer = fh.read(24)
        if len(footer) != 24 or footer[8:] != APK_SIG_BLOCK_MAGIC:
            return {}
        block_size = struct.unpack_from("<Q", footer, 0)[0]
        if block_size < 24 or block_size > 256 * 1024 * 1024:
            return {}
        block_start = cd_offset - block_size - 8
        if block_start < 0:
            return {}
        fh.seek(block_start)
        block = fh.read(block_size + 8)
    if len(block) != block_size + 8:
        return {}
    if struct.unpack_from("<Q", block, 0)[0] != block_size:
        return {}
    pairs: dict[int, bytes] = {}
    pos = 8
    end = len(block) - 24
    while pos + 12 <= end:
        pair_size = struct.unpack_from("<Q", block, pos)[0]
        if pair_size < 4 or pos + 8 + pair_size > len(block):
            break
        pair_id = struct.unpack_from("<I", block, pos + 8)[0]
        pairs[pair_id] = block[pos + 12:pos + 8 + pair_size]
        pos += 8 + pair_size
    return pairs


def _length_prefixed(data: bytes, pos: int) -> tuple[bytes, int]:
    if pos + 4 > len(data):
        raise ValueError("bloque de firma truncado")
    length = struct.unpack_from("<I", data, pos)[0]
    start = pos + 4
    end = start + length
    if end > len(data):
        raise ValueError("bloque de firma truncado")
    return data[start:end], end


def _v2_signer_certificate(value: bytes) -> bytes:
    signers, _ = _length_prefixed(value, 0)
    signer, _ = _length_prefixed(signers, 0)
    signed_data, _ = _length_prefixed(signer, 0)
    _, pos = _length_prefixed(signed_data, 0)          # digests
    certificates, _ = _length_prefixed(signed_data, pos)
    certificate, _ = _length_prefixed(certificates, 0)
    return certificate


def apk_signer_sha256(path: Path) -> str:
    """Huella del certificado que firma el APK, o cadena vacía si no se pudo leer."""
    try:
        blocks = _apk_signing_block(path)
        for scheme in APK_SIG_SCHEMES:
            value = blocks.get(scheme)
            if value:
                certificate = _v2_signer_certificate(value)
                if certificate:
                    return hashlib.sha256(certificate).hexdigest()
        with zipfile.ZipFile(path) as zf:
            candidates = [
                name for name in zf.namelist()
                if name.startswith("META-INF/")
                and name.upper().endswith((".RSA", ".DSA", ".EC"))
            ]
            if not candidates:
                return ""
            certificate = _v1_signer_certificate(zf.read(candidates[0]))
        return hashlib.sha256(certificate).hexdigest() if certificate else ""
    except (OSError, ValueError, struct.error, zipfile.BadZipFile, IndexError):
        return ""


def _declared_abi(filename: str) -> str:
    low = filename.lower().replace("-", "_")
    if "arm64_v8a" in low:
        return "arm64-v8a"
    if "armeabi_v7a" in low:
        return "armeabi-v7a"
    if "x86_64" in low:
        return "x86_64"
    if "x86" in low:
        return "x86"
    return ""


def device_sdk(serial: str | None = None) -> int | None:
    serial = serial or pick_serial()
    rc, out, err = adb_run(
        ["-s", serial, "shell", "getprop ro.build.version.sdk"], timeout=10
    )
    text = (out or err).strip()
    return int(text) if text.isdigit() else None


def _zip_apk_entries(zf: zipfile.ZipFile) -> list[tuple[str, zipfile.ZipInfo]]:
    entries = []
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename.replace("\\", "/")
        if name.lower().endswith(".apk"):
            entries.append((name, info))
    return entries


def _manifest_apk_names(zf: zipfile.ZipFile) -> tuple[list[str], str]:
    """Lista de APK declarada por un XAPK/APKM y el paquete, si existe."""
    names = []
    package = ""
    for manifest_name in ("manifest.json", "info.json"):
        try:
            info = zf.getinfo(manifest_name)
            if info.file_size > 2 * 1024 * 1024:
                continue
            data = json.loads(zf.read(info).decode("utf-8", "replace"))
        except (KeyError, ValueError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        package = str(data.get("package_name") or data.get("package") or package)
        base = data.get("base")
        if isinstance(base, dict) and base.get("file"):
            names.append(str(base["file"]))
        for key in ("split_apks", "splits", "apks"):
            items = data.get(key)
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and item.get("file"):
                    names.append(str(item["file"]))
                elif isinstance(item, str):
                    names.append(item)
        if names:
            break
    return names, package


def _resolve_zip_entries(
    entries: list[tuple[str, zipfile.ZipInfo]], names: list[str]
) -> list[tuple[str, zipfile.ZipInfo]]:
    by_name = {name: (name, info) for name, info in entries}
    by_base: dict[str, list[tuple[str, zipfile.ZipInfo]]] = {}
    for entry in entries:
        by_base.setdefault(entry[0].rsplit("/", 1)[-1].lower(), []).append(entry)
    chosen = []
    for raw in names:
        name = raw.replace("\\", "/").lstrip("/")
        entry = by_name.get(name)
        if entry is None:
            matches = by_base.get(name.rsplit("/", 1)[-1].lower(), [])
            entry = matches[0] if len(matches) == 1 else None
        if entry and entry not in chosen:
            chosen.append(entry)
    return chosen


def _select_bundle_entries(
    zf: zipfile.ZipFile, filename: str, abis: list[str]
) -> list[tuple[str, zipfile.ZipInfo]]:
    entries = _zip_apk_entries(zf)
    if not entries:
        return []
    declared, package = _manifest_apk_names(zf)
    if declared:
        chosen = _resolve_zip_entries(entries, declared)
        if chosen:
            # Algunos XAPK no listan el APK base; se añade si está claro.
            bases = [
                entry for entry in entries
                if entry[0].rsplit("/", 1)[-1].lower() in ("base.apk", "base-master.apk")
                or (package and entry[0].rsplit("/", 1)[-1].lower() == f"{package}.apk")
            ]
            for base in bases:
                if base not in chosen:
                    chosen.insert(0, base)
            entries = chosen

    splits = [entry for entry in entries if entry[0].startswith("splits/")]
    if splits:
        return splits

    universal = [entry for entry in entries if entry[0].lower().endswith("universal.apk")]
    if universal:
        return universal[:1]

    standalones = [entry for entry in entries if entry[0].startswith("standalones/")]
    if standalones:
        for abi in abis:
            wanted = abi.replace("-", "_")
            matches = [
                entry for entry in standalones
                if wanted in entry[0].lower().replace("-", "_")
            ]
            if len(matches) == 1:
                return matches
        return standalones[:1]
    return entries


def _extract_bundle_apks(
    zf: zipfile.ZipFile,
    entries: list[tuple[str, zipfile.ZipInfo]],
    destination: Path,
) -> list[Path]:
    if len(entries) > INSTALL_MAX_APKS:
        raise AdbError(
            f"El paquete contiene demasiados APK ({len(entries)}). "
            f"El límite es {INSTALL_MAX_APKS}."
        )
    total = 0
    paths = []
    for index, (name, info) in enumerate(entries):
        if info.flag_bits & 0x1:
            raise AdbError("El paquete está cifrado y no se puede leer.")
        if info.file_size > INSTALL_MAX_ENTRY:
            raise AdbError(f"«{name}» es demasiado grande para instalarlo.")
        total += info.file_size
        if total > INSTALL_MAX_UNCOMPRESSED:
            raise AdbError("El paquete dividido ocupa demasiado al descomprimirlo.")
        base = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name).name) or "paquete.apk"
        if not base.lower().endswith(".apk"):
            base += ".apk"
        target = destination / f"{index:02d}-{base}"
        written = 0
        try:
            with zf.open(info) as source, target.open("wb") as output:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > INSTALL_MAX_ENTRY:
                        raise AdbError(f"«{name}» supera el tamaño máximo permitido.")
                    output.write(chunk)
        except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
            raise AdbError(f"No pude extraer «{name}» del paquete: {exc}") from exc
        if not written:
            raise AdbError(f"«{name}» está vacío.")
        paths.append(target)
    return paths


def _filter_bundle_abis(parsed: list[dict], abis: list[str]) -> tuple[list[dict], list[dict]]:
    """Descarta splits de arquitecturas que el Fire TV no puede ejecutar."""
    if not abis:
        return parsed, []
    all_libs = set()
    for item in parsed:
        item["libs"] = apk_native_abis(item["path"])
        all_libs.update(item["libs"])
    if all_libs and not all_libs & set(abis):
        raise AdbError(abi_mismatch_message(sorted(all_libs), abis))

    primary = abis[0]
    for item in parsed:
        item["best_abi"] = next(
            (abi for abi in abis if abi in item["libs"]), ""
        ) or _declared_abi(item["path"].name)
    base = next((item for item in parsed if not item["info"]["split"]), None)
    primary_splits = any(
        item is not base and item["best_abi"] == primary for item in parsed
    )
    kept = []
    skipped = []
    for item in parsed:
        abi = item["best_abi"]
        if item is base:
            if abi and abi not in abis:
                raise AdbError(abi_mismatch_message(item["libs"] or [abi], abis))
            kept.append(item)
        elif not abi:
            kept.append(item)
        elif abi not in abis:
            skipped.append(item)
        elif primary_splits and abi != primary:
            skipped.append(item)
        else:
            kept.append(item)
    if base is None:
        raise AdbError("El paquete no tiene un APK base reconocible.")
    if base not in kept:
        raise AdbError(abi_mismatch_message(base["libs"], abis))
    return kept, skipped


def inspect_install_package(path: Path, filename: str, serial: str | None = None) -> dict:
    """Valida un APK o paquete dividido y devuelve el plan de instalación."""
    serial = serial or pick_serial()
    lower = filename.lower()
    if not lower.endswith(INSTALL_EXTENSIONS):
        raise AdbError(
            "Formato no admitido. Usa .apk, .apks, .xapk, .apkm o un .zip con APK."
        )
    if path.stat().st_size == 0:
        raise AdbError("El archivo está vacío.")
    if not zipfile.is_zipfile(path):
        raise AdbError("El archivo no es un APK ni un paquete ZIP válido.")

    abis = device_abis(serial)
    sdk = device_sdk(serial)
    single_apk = lower.endswith(".apk")
    if single_apk:
        entries = []
        obb = False
        total_size = path.stat().st_size
    else:
        with zipfile.ZipFile(path) as zf:
            entries = _select_bundle_entries(zf, filename, abis)
            if not entries:
                raise AdbError(
                    "El paquete no contiene ningún APK. Revisa que sea un .apks o XAPK completo."
                )
            obb = any(name.lower().endswith(".obb") for name in zf.namelist())
            total_size = sum(info.file_size for _, info in entries)

    tempdir = Path(tempfile.mkdtemp(prefix="firedeck_install_"))
    try:
        if single_apk:
            extracted = [path]
        else:
            with zipfile.ZipFile(path) as zf:
                extracted = _extract_bundle_apks(zf, entries, tempdir)
        parsed = []
        for apk in extracted:
            info = apk_manifest_info(apk)
            parsed.append({"path": apk, "info": info})
        packages = {item["info"]["package"] for item in parsed}
        if len(packages) != 1:
            raise AdbError(
                "El paquete mezcla APK de apps distintas ("
                + ", ".join(sorted(packages)) + "). Descarga una versión completa."
            )
        package = next(iter(packages))

        bases = [item for item in parsed if not item["info"]["split"]]
        if len(bases) > 1:
            names = ", ".join(item["path"].name for item in bases[:4])
            raise AdbError(
                "El paquete contiene varias variantes base (" + names + "). "
                "Suele ser un .apks con varias arquitecturas: usa el XAPK o el "
                "APK universal de una sola variante."
            )
        if not bases and len(parsed) > 1:
            raise AdbError("El paquete no tiene un APK base reconocible.")
        base = bases[0] if bases else parsed[0]
        base["info"]["split"] = ""

        min_sdk = base["info"].get("min_sdk")
        if sdk is not None and min_sdk and min_sdk > sdk:
            raise AdbError(
                f"La app exige Android API {min_sdk} o superior y tu Fire TV tiene "
                f"API {sdk}. Busca una versión compatible con Fire OS."
            )

        kept, skipped = _filter_bundle_abis(parsed, abis)
        kept.sort(key=lambda item: item is not base)

        signers = {}
        for item in kept:
            digest = apk_signer_sha256(item["path"])
            if not digest:
                raise AdbError(
                    f"No pude comprobar la firma de «{item['path'].name}». "
                    "Puede estar dañado o sin firmar."
                )
            item["signer"] = digest
            signers.setdefault(digest, []).append(item["path"].name)
        if len(signers) > 1:
            raise AdbError(
                "Los APK del paquete no tienen la misma firma. Android rechazaría "
                "la instalación; descarga de nuevo el paquete desde una sola fuente."
            )
        signer = next(iter(signers)) if signers else ""

        return {
            "kind": "apk" if len(kept) == 1 else "bundle",
            "package": package,
            "version_name": base["info"].get("version_name", ""),
            "version_code": base["info"].get("version_code"),
            "base": base["path"],
            "apks": [item["path"] for item in kept],
            "libs": sorted({lib for item in kept for lib in item.get("libs", [])}),
            "skipped": [
                {
                    "name": item["path"].name,
                    "abi": item["best_abi"],
                    "libs": item.get("libs", []),
                }
                for item in skipped
            ],
            "signer": signer,
            "obb": obb,
            "total_size": total_size,
            "tempdir": tempdir,
        }
    except Exception:
        shutil.rmtree(tempdir, ignore_errors=True)
        raise


def _install_summary(plan: dict) -> str:
    version = plan.get("version_name") or ""
    if plan.get("version_code"):
        version = (version + f" ({plan['version_code']})").strip()
    size = human_size((plan.get("total_size") or 0) / 1024)
    pieces = [f"{plan['package']}" + (f" {version}" if version else "")]
    if plan["kind"] == "bundle":
        pieces.append(f"{len(plan['apks'])} APK (base + {len(plan['apks']) - 1} divisiones)")
    else:
        pieces.append("1 APK")
    pieces.append(size)
    if plan.get("skipped"):
        pieces.append(f"{len(plan['skipped'])} split(s) omitido(s) por arquitectura")
    if plan.get("obb"):
        pieces.append("OBB no incluidos")
    pieces.append("firma " + plan["signer"][:12] + "…")
    return " · ".join(pieces)


def install_package(path: Path, filename: str, serial: str | None = None) -> str:
    """Instala un APK o un paquete dividido desde un archivo local."""
    serial = serial or pick_serial()
    plan = inspect_install_package(path, filename, serial)
    try:
        if plan["kind"] == "apk":
            install_file = plan["apks"][0]
            if install_file.suffix.lower() not in (".apk", ".apex"):
                install_file = plan["tempdir"] / "base.apk"
                shutil.copyfile(plan["apks"][0], install_file)
            rc, out, err = adb_run(
                ["-s", serial, "install", "-r", "-g", str(install_file)],
                timeout=900,
            )
        else:
            rc, out, err = adb_run(
                ["-s", serial, "install-multiple", "-r", "-g",
                 *(str(apk) for apk in plan["apks"])],
                timeout=1200,
            )
        text = (out + "\n" + err).strip()
        if rc != 0 or "Success" not in text:
            raise AdbError(install_failure_message(text, plan["libs"], device_abis(serial)))
        ABI_CACHE.pop(serial, None)
        return text + "\n\n" + _install_summary(plan)
    finally:
        shutil.rmtree(plan["tempdir"], ignore_errors=True)


def install_apk(data: bytes, filename: str) -> str:
    if not data:
        raise AdbError("El archivo APK está vacío.")
    if not filename.lower().endswith(INSTALL_EXTENSIONS):
        raise AdbError("Solo se aceptan archivos .apk, .apks, .xapk, .apkm o .zip.")
    fd, tmp_name = tempfile.mkstemp(prefix="firedeck_", suffix=".bin")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_bytes(data)
        return install_package(tmp, filename)
    finally:
        tmp.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# Descargar e instalar por código de Downloader o URL directa (3.3)
#
# La app Downloader de AFTVnews no descarga «por código»: cambia el número por
# una URL en aftv.news y baja ese archivo. Aquí se hace lo mismo en el PC y se
# reutiliza la instalación de la tarea 2.2. Las descargas son trabajos en
# segundo plano para poder mostrar progreso y cancelarlas a mitad.
# --------------------------------------------------------------------------

DOWNLOADER_FILE = ROOT / "downloader.json"
DOWNLOADER_LOCK = threading.RLock()
DOWNLOAD_CODES = re.compile(r"^[A-Za-z0-9]{3,32}$")
DOWNLOAD_HOST = "https://aftv.news/"
DOWNLOAD_MAX_BYTES = INSTALL_MAX_UPLOAD
DOWNLOAD_MAX_REDIRECTS = 6
DOWNLOAD_CONNECT_TIMEOUT = 20
DOWNLOAD_READ_TIMEOUT = 45
DOWNLOAD_CHUNK = 512 * 1024
DOWNLOAD_MAX_JOBS = 24
DOWNLOAD_JOB_TTL = 3600
DOWNLOAD_UA = (
    "Mozilla/5.0 (Linux; Android 9; AFTMM Build/PS7233) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Downloader/1.5.1"
)
DOWNLOAD_PAGE_LIMIT = 2 * 1024 * 1024
DOWNLOAD_JOBS: dict[str, dict] = {}
DOWNLOAD_JOBS_LOCK = threading.RLock()
# Solo para las pruebas internas: permite apuntar a un servidor local.
DOWNLOAD_ALLOW_PRIVATE = False


class DownloadCancelled(AdbError):
    """La descarga se detuvo a petición del usuario."""


def _is_public_address(value: str) -> bool:
    """True solo para IP públicas: evita que un enlace lea la red local."""
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return bool(ip.is_global) and not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def _public_endpoint(host: str, port: int) -> tuple[int, str]:
    """Resuelve el dominio y comprueba que todas sus direcciones sean públicas."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise AdbError(
            f"No pude resolver el dominio «{host}». Comprueba tu conexión y el enlace."
        ) from exc
    addresses: list[tuple[int, str]] = []
    for family, _, _, _, sockaddr in infos:
        address = str(sockaddr[0])
        if not DOWNLOAD_ALLOW_PRIVATE and not _is_public_address(address):
            raise AdbError(
                "Por seguridad, FireDeck no descarga de direcciones locales o privadas "
                "(localhost, 10.x, 192.168.x, 172.16–31.x). Usa un enlace público."
            )
        addresses.append((family, address))
    if not addresses:
        raise AdbError(f"El dominio «{host}» no tiene direcciones utilizables.")
    # IPv4 primero: algunos Fire OS/CDN fallan con IPv6 aunque el PC lo tenga.
    addresses.sort(key=lambda item: 0 if item[0] == socket.AF_INET else 1)
    return addresses[0]


def download_target(value) -> tuple[str, str, str]:
    """Convierte «código» o URL en (url, origen, código). Valida la forma."""
    text = str(value or "").strip()
    if not text:
        raise AdbError("Escribe un código de Downloader o una URL.")
    if len(text) > 2048:
        raise AdbError("El enlace es demasiado largo.")
    if re.match(r"^https?://", text, re.IGNORECASE):
        return text, "url", ""
    if "://" in text:
        raise AdbError("Solo se admiten enlaces http:// o https://.")
    code = text.lstrip("#").strip()
    if not DOWNLOAD_CODES.fullmatch(code):
        raise AdbError(
            "El código de Downloader debe tener entre 3 y 32 letras o números, "
            "sin espacios."
        )
    return DOWNLOAD_HOST + quote(code, safe=""), "code", code


def _connect_download(scheme: str, host: str, port: int, ip: str):
    """Abre la conexión contra la IP ya validada, conservando el SNI y el Host."""
    try:
        raw = socket.create_connection((ip, port), timeout=DOWNLOAD_CONNECT_TIMEOUT)
    except OSError as exc:
        raise AdbError(f"No pude conectar con «{host}»: {exc}") from exc
    try:
        if scheme == "https":
            sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
            conn = http.client.HTTPSConnection(host, port, timeout=DOWNLOAD_READ_TIMEOUT)
        else:
            sock = raw
            conn = http.client.HTTPConnection(host, port, timeout=DOWNLOAD_READ_TIMEOUT)
    except (ssl.SSLError, OSError) as exc:
        try:
            raw.close()
        except OSError:
            pass
        raise AdbError(
            f"El certificado de «{host}» no es válido o la conexión segura falló: {exc}"
        ) from exc
    conn.sock = sock
    return conn


def _is_mediafire_host(host: str) -> bool:
    host = (host or "").lower().rstrip(".")
    return host == "mediafire.com" or host.endswith(".mediafire.com")


def _is_mediafire_page_host(host: str) -> bool:
    """Páginas de MediaFire, no los hosts download*.mediafire.com (ya sirven el archivo)."""
    host = (host or "").lower().rstrip(".")
    return host in ("mediafire.com", "www.mediafire.com", "m.mediafire.com")


def _is_mediafire_folder(url: str) -> bool:
    parts = urlparse(url)
    return bool(
        parts.hostname
        and _is_mediafire_page_host(parts.hostname)
        and parts.path.lower().startswith("/folder")
    )


def _html_anchor_at(page: str, index: int) -> str:
    """Devuelve la etiqueta <a …> que contiene la posición indicada.

    No vale un simple `<a[^>]*>`: el atributo `onclick` de MediaFire contiene
    `=>`, y ese `>` cortaría la etiqueta antes de llegar a `id`.
    """
    start = page.rfind("<a", 0, index)
    if start == -1:
        return ""
    quote = ""
    position = start
    while position < len(page):
        char = page[position]
        if quote:
            if char == quote:
                quote = ""
        elif char in ('"', "'"):
            quote = char
        elif char == ">":
            return page[start : position + 1]
        position += 1
    return page[start:]


def _mediafire_direct_link(page: str, base: str) -> str:
    """Enlace real de descarga dentro de la página de un archivo de MediaFire."""
    # La página trae el botón como <a … href="https://download####.mediafire.com/…"
    # id="downloadButton">. Los atributos pueden venir en cualquier orden.
    marker = re.search(r"""\bid\s*=\s*["']downloadButton["']""", page, re.IGNORECASE)
    if marker:
        tag = _html_anchor_at(page, marker.start())
        match = re.search(r"""\bhref\s*=\s*["']([^"']+)["']""", tag, re.IGNORECASE)
        if match:
            target = html.unescape(match.group(1)).strip()
            if target and not target.lower().startswith(("#", "javascript:", "data:")):
                absolute = urljoin(base, target)
                if _is_mediafire_host(urlparse(absolute).hostname or ""):
                    return absolute

    # Algunas variantes no traen el botón pero dejan el enlace del CDN en el HTML.
    match = re.search(
        r"""https?://download\d*\.mediafire\.com/[^\s"'<>\\]+""", page, re.IGNORECASE
    )
    if match:
        return html.unescape(match.group(0))

    # Otras páginas guardan el destino en una variable de JavaScript.
    for pattern in (
        r"""\bdownload_link\b\s*[:=]\s*["']([^"']+)["']""",
        r"""\bdownloadUrl\b\s*[:=]\s*["']([^"']+)["']""",
    ):
        match = re.search(pattern, page, re.IGNORECASE)
        if not match:
            continue
        target = html.unescape(match.group(1)).strip().replace("\\/", "/")
        if target.startswith("//"):
            target = "https:" + target
        absolute = urljoin(base, target)
        if _is_mediafire_host(urlparse(absolute).hostname or ""):
            return absolute
    return ""


def _mediafire_page_problem(page: str) -> str:
    """Mensaje concreto cuando la página de MediaFire explica por qué no hay descarga."""
    low = page.lower()
    if "file is currently set to private" in low or "this file is private" in low:
        return (
            "Ese archivo de MediaFire es privado: quien lo subió no permite descargarlo. "
            "Pide permiso o usa otro enlace."
        )
    if "file removed" in low or "file deleted" in low or "file has been removed" in low:
        return "Ese archivo ya no existe en MediaFire: lo borraron o el enlace caducó."
    return ""


def _open_download(url: str):
    """GET validando cada salto: solo http/https y solo direcciones públicas."""
    current = url
    mediafire_pages = 0
    for _ in range(DOWNLOAD_MAX_REDIRECTS + 1):
        parts = urlparse(current)
        scheme = parts.scheme.lower()
        if scheme not in ("http", "https"):
            raise AdbError("Solo se admiten enlaces http:// o https://.")
        if parts.username or parts.password:
            raise AdbError("El enlace no puede llevar usuario y contraseña.")
        host = parts.hostname
        if not host:
            raise AdbError("La dirección no tiene un dominio válido.")
        try:
            port = parts.port or (443 if scheme == "https" else 80)
        except ValueError as exc:
            raise AdbError("El puerto del enlace no es válido.") from exc
        _family, ip = _public_endpoint(host, port)
        target = (parts.path or "/").replace(" ", "%20")
        if parts.query:
            target += "?" + parts.query.replace(" ", "%20")

        conn = _connect_download(scheme, host, port, ip)
        try:
            conn.request(
                "GET",
                target,
                headers={
                    "Host": host if port in (80, 443) else f"{host}:{port}",
                    "User-Agent": DOWNLOAD_UA,
                    "Accept": "*/*",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                },
            )
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            conn.close()
            raise AdbError(f"La descarga de «{host}» falló: {exc}") from exc

        if resp.status in (301, 302, 303, 307, 308):
            location = resp.getheader("Location")
            try:
                resp.read(16384)
            except (OSError, http.client.HTTPException):
                pass
            conn.close()
            if not location:
                raise AdbError(f"«{host}» redirigió sin indicar destino.")
            current = urljoin(current, location)
            continue
        if resp.status != 200:
            reason = f" {resp.reason}" if resp.reason else ""
            status = resp.status
            conn.close()
            if _is_mediafire_page_host(host) and status in (404, 410):
                raise AdbError(
                    "Ese archivo no existe en MediaFire o el enlace está mal "
                    f"({status}). Comprueba que lo sigues teniendo compartido."
                )
            raise AdbError(f"El servidor respondió {status}{reason}.")

        # aftv.news no responde con un 302: devuelve una página con una espera
        # de 6 s y `window.location = "<destino>"`. La app Downloader también
        # la interpreta, así que aquí se extrae el destino y se sigue sin esperar.
        if _is_shortener_host(host):
            content_type = (resp.getheader("Content-Type") or "").lower()
            if "html" in content_type or "text/" in content_type:
                try:
                    page = resp.read(512 * 1024).decode("utf-8", "replace")
                except (OSError, http.client.HTTPException) as exc:
                    conn.close()
                    raise AdbError(
                        f"No pude leer la página del código en «{host}»: {exc}"
                    ) from exc
                conn.close()
                destination = _shortener_destination(page)
                if not destination:
                    raise AdbError(
                        "Ese código no existe o ya no apunta a ninguna descarga. "
                        "Compruébalo en la app Downloader o usa una URL directa."
                    )
                current = urljoin(current, destination)
                continue

        # MediaFire no da un enlace directo: la URL del archivo devuelve una
        # página con un botón «Download» que apunta al CDN. Se lee esa página,
        # se extrae el enlace real y se sigue, igual que haría el navegador.
        if _is_mediafire_page_host(host):
            content_type = (resp.getheader("Content-Type") or "").lower()
            if "html" in content_type or "text/" in content_type:
                mediafire_pages += 1
                if mediafire_pages > 3:
                    conn.close()
                    raise AdbError(
                        "MediaFire encadenó varias páginas sin ofrecer el archivo. "
                        "Abre el enlace en el navegador para comprobar que se puede descargar."
                    )
                if _is_mediafire_folder(current):
                    conn.close()
                    raise AdbError(
                        "Ese enlace es una carpeta de MediaFire, no un archivo. Abre la "
                        "carpeta, entra al APK que quieras descargar y pega aquí el "
                        "enlace de ese archivo."
                    )
                try:
                    raw = resp.read(DOWNLOAD_PAGE_LIMIT)
                except (OSError, http.client.HTTPException) as exc:
                    conn.close()
                    raise AdbError(
                        f"No pude leer la página de MediaFire: {exc}"
                    ) from exc
                conn.close()
                page = raw.decode("utf-8", "replace")
                destination = _mediafire_direct_link(page, current)
                if not destination:
                    problem = _mediafire_page_problem(page)
                    if problem:
                        raise AdbError(problem)
                    raise AdbError(
                        "No encontré el enlace de descarga en esa página de MediaFire. "
                        "Puede que el archivo sea privado o que MediaFire haya cambiado "
                        "la página; abre el enlace en el navegador, pulsa Descargar y "
                        "pega aquí la dirección del archivo."
                    )
                if destination == current:
                    raise AdbError(
                        "MediaFire devolvió la misma página y no ofreció el archivo. "
                        "Abre el enlace en el navegador para comprobar que se puede descargar."
                    )
                current = destination
                continue

        return conn, resp, current

    raise AdbError("El enlace da demasiadas vueltas (demasiadas redirecciones).")


def _is_shortener_host(host: str) -> bool:
    host = (host or "").lower()
    return host == "aftv.news" or host == "aftvnews.com" or host.endswith(".aftvnews.com")


def _shortener_destination(page: str) -> str:
    """Destino real de la página intermedia de AFTVnews (código de Downloader)."""
    patterns = (
        r'window\.location\s*=\s*["\']([^"\']+)["\']',
        r'window\.location\.href\s*=\s*["\']([^"\']+)["\']',
        r'location\.assign\(\s*["\']([^"\']+)["\']\s*\)',
        r'location\.replace\(\s*["\']([^"\']+)["\']\s*\)',
        r'<meta[^>]+http-equiv=["\']refresh["\'][^>]+content=["\'][^"\']*?url=([^"\']+)["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, page, re.IGNORECASE)
        if match:
            target = html.unescape(match.group(1)).strip()
            if target and not target.lower().startswith(("javascript:", "data:")):
                return target
    # Sin JavaScript, la página deja el mismo enlace en <noscript>.
    match = re.search(
        r"<noscript>.*?<a[^>]+href=[\"']([^\"']+)[\"']", page, re.IGNORECASE | re.DOTALL
    )
    if match:
        target = html.unescape(match.group(1)).strip()
        if target and not target.lower().startswith(("javascript:", "data:")):
            return target
    return ""


def _repair_filename(name: str) -> str:
    """Arregla nombres UTF-8 que llegaron como bytes Latin-1 (p. ej. MediaFire)."""
    try:
        fixed = name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return name
    return fixed


def _download_filename(resp, final_url: str) -> str:
    """Nombre del archivo según Content-Disposition, URL y tipo MIME."""
    disposition = resp.getheader("Content-Disposition") or ""
    name = ""
    match = re.search(r"filename\*\s*=\s*([^;]+)", disposition, re.IGNORECASE)
    if match:
        value = match.group(1).strip().strip('"')
        if "''" in value:
            value = value.split("''", 1)[1]
        name = unquote(value)
    if not name:
        match = re.search(r'filename\s*=\s*"([^"]*)"', disposition, re.IGNORECASE)
        if match:
            name = match.group(1)
        else:
            match = re.search(r"filename\s*=\s*([^;]+)", disposition, re.IGNORECASE)
            if match:
                name = match.group(1).strip().strip('"')
    if not name:
        name = unquote(Path(urlparse(final_url).path).name)

    name = _repair_filename(name)
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[\x00-\x1f\x7f]+", "", name).strip().strip(".")
    content_type = (resp.getheader("Content-Type") or "").split(";")[0].strip().lower()
    if "html" in content_type:
        page_host = urlparse(final_url).hostname or ""
        where = f" ({page_host})" if page_host else ""
        raise AdbError(
            f"El enlace lleva a una página web{where}, no a un archivo. Abre esa "
            "página, copia el enlace directo de descarga y pégalo aquí."
        )
    suffix = Path(name).suffix.lower()
    if suffix not in INSTALL_EXTENSIONS:
        inferred = {
            "application/vnd.android.package-archive": ".apk",
            "application/x-authorware-bin": ".apk",
            "application/zip": ".zip",
            "application/x-zip-compressed": ".zip",
            "multipart/x-zip": ".zip",
        }.get(content_type, "")
        if not inferred:
            raise AdbError(
                "El enlace no apunta a un APK o paquete (.apk, .apks, .xapk, "
                ".apkm o .zip). Si es una página de descarga, busca el enlace directo."
            )
        name = (Path(name).stem if suffix else name) + inferred
    if not name:
        name = "paquete" + (suffix or ".apk")
    if len(name) > 140:
        stem = Path(name).stem[: 140 - len(Path(name).suffix)]
        name = stem + Path(name).suffix
    return name


def _response_size(resp) -> int:
    raw = resp.getheader("Content-Length")
    try:
        total = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        total = 0
    if total > DOWNLOAD_MAX_BYTES:
        raise AdbError(
            "El archivo supera el límite de "
            + human_size(DOWNLOAD_MAX_BYTES / 1024)
            + " para descargas."
        )
    return max(0, total)


def resolve_download(value) -> dict:
    """Comprueba el enlace sin descargar el archivo y devuelve la vista previa."""
    url, source, code = download_target(value)
    conn, resp, final = _open_download(url)
    try:
        filename = _download_filename(resp, final)
        total = _response_size(resp)
        content_type = (resp.getheader("Content-Type") or "").split(";")[0].strip().lower()
    finally:
        try:
            resp.close()
        finally:
            conn.close()
    parts = urlparse(final)
    return {
        "input": str(value or "").strip(),
        "source": source,
        "code": code,
        "host": parts.hostname or "",
        "service": "MediaFire" if _is_mediafire_host(parts.hostname or "") else "",
        "filename": filename,
        "size": total,
        "content_type": content_type,
        "ext": Path(filename).suffix.lower().lstrip("."),
    }


def _update_job(job: dict, **fields):
    with DOWNLOAD_JOBS_LOCK:
        job.update(fields)
        job["updated"] = time.time()


def _job_view(job: dict) -> dict:
    total = int(job.get("total") or 0)
    loaded = int(job.get("loaded") or 0)
    percent = None
    if total > 0:
        percent = max(0, min(100, round(loaded * 100 / total)))
    elif job.get("status") == "installing":
        percent = 100
    return {
        "id": job["id"],
        "input": job["input"],
        "source": job["source"],
        "status": job["status"],
        "message": job.get("message", ""),
        "filename": job.get("filename", ""),
        "total": total,
        "loaded": loaded,
        "percent": percent,
        "output": job.get("output", ""),
        "error": job.get("error", ""),
        "started": round(job.get("started", 0), 3),
        "finished": round(job.get("finished") or 0, 3),
    }


def _prune_jobs_locked():
    cutoff = time.time() - DOWNLOAD_JOB_TTL
    for key in [
        key
        for key, job in DOWNLOAD_JOBS.items()
        if job.get("finished") and job["finished"] < cutoff
    ]:
        DOWNLOAD_JOBS.pop(key, None)
    while len(DOWNLOAD_JOBS) > DOWNLOAD_MAX_JOBS:
        oldest = min(DOWNLOAD_JOBS.values(), key=lambda item: item.get("started", 0))
        DOWNLOAD_JOBS.pop(oldest["id"], None)


def _package_from_install_output(text: str) -> str:
    match = re.search(r"\n\n([A-Za-z][A-Za-z0-9_.]+)(?:\s|$)", text or "")
    return match.group(1) if match else ""


def _run_download_job(job: dict, url: str):
    bind_tab(job.get("tab", ""))
    temp: Path | None = None
    conn = None
    resp = None
    try:
        _update_job(job, status="resolving", message="Resolviendo el enlace…")
        if job["cancel"].is_set():
            raise DownloadCancelled("Descarga cancelada.")
        conn, resp, final = _open_download(url)
        filename = _download_filename(resp, final)
        total = _response_size(resp)
        _update_job(
            job,
            status="downloading",
            filename=filename,
            total=total,
            loaded=0,
            message=f"Descargando «{filename}»…",
        )

        suffix = Path(filename).suffix[:12] or ".bin"
        fd, tmp_name = tempfile.mkstemp(prefix="firedeck_dl_", suffix=suffix)
        temp = Path(tmp_name)
        loaded = 0
        try:
            with os.fdopen(fd, "wb") as output:
                while True:
                    if job["cancel"].is_set():
                        raise DownloadCancelled("Descarga cancelada.")
                    chunk = resp.read(DOWNLOAD_CHUNK)
                    if not chunk:
                        break
                    loaded += len(chunk)
                    if loaded > DOWNLOAD_MAX_BYTES:
                        raise AdbError(
                            "La descarga supera el límite de "
                            + human_size(DOWNLOAD_MAX_BYTES / 1024)
                            + "."
                        )
                    output.write(chunk)
                    _update_job(job, loaded=loaded)
        except (OSError, http.client.HTTPException) as exc:
            raise AdbError(f"La descarga se cortó antes de terminar: {exc}") from exc
        if total > 0 and loaded < total:
            raise AdbError(
                "La descarga se cortó: llegaron "
                + human_size(loaded / 1024)
                + " de "
                + human_size(total / 1024)
                + "."
            )
        try:
            resp.close()
        finally:
            resp = None
            conn.close()
            conn = None

        if job["cancel"].is_set():
            raise DownloadCancelled("Descarga cancelada.")
        if loaded == 0:
            raise AdbError("El servidor devolvió un archivo vacío.")

        _update_job(
            job,
            status="installing",
            percent=100,
            message="Descargado. Instalando en el Fire TV…",
        )
        output = install_package(temp, filename, serial=job.get("serial"))
        package = _package_from_install_output(output)
        _update_job(
            job,
            status="done",
            output=output,
            message="Instalación completada.",
            finished=time.time(),
        )
        remember_download(
            job["input"], job["source"], filename, package=package, ok=True
        )
    except DownloadCancelled:
        _update_job(
            job,
            status="cancelled",
            message="Descarga cancelada.",
            finished=time.time(),
        )
    except AdbError as exc:
        _update_job(
            job,
            status="error",
            error=str(exc),
            message="No se pudo completar.",
            finished=time.time(),
        )
        if temp is not None:
            remember_download(
                job["input"], job["source"], job.get("filename", ""), ok=False
            )
    except Exception as exc:  # noqa: BLE001
        log_unexpected("Descarga inesperada", exc)
        _update_job(
            job,
            status="error",
            error=f"Error interno: {exc}",
            message="No se pudo completar.",
            finished=time.time(),
        )
    finally:
        if resp is not None:
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        if temp is not None:
            temp.unlink(missing_ok=True)
        with DOWNLOAD_JOBS_LOCK:
            _prune_jobs_locked()


def start_download_job(value) -> dict:
    """Crea el trabajo de descarga+instalación y lo lanza en un hilo."""
    url, source, _code = download_target(value)
    serial = pick_serial()
    job_id = os.urandom(8).hex()
    job = {
        "id": job_id,
        "input": str(value or "").strip(),
        "source": source,
        "serial": serial,
        "tab": current_tab_id(),
        "status": "queued",
        "message": "En cola…",
        "filename": "",
        "total": 0,
        "loaded": 0,
        "output": "",
        "error": "",
        "started": time.time(),
        "finished": 0.0,
        "cancel": threading.Event(),
    }
    with DOWNLOAD_JOBS_LOCK:
        _prune_jobs_locked()
        DOWNLOAD_JOBS[job_id] = job
    threading.Thread(
        target=_run_download_job,
        args=(job, url),
        name=f"firedeck-download-{job_id}",
        daemon=True,
    ).start()
    return _job_view(job)


def download_job(job_id: str) -> dict:
    job_id = str(job_id or "").strip()
    with DOWNLOAD_JOBS_LOCK:
        job = DOWNLOAD_JOBS.get(job_id)
    if not job:
        raise AdbError("Ese trabajo de descarga ya no existe. Vuelve a empezar.")
    return _job_view(job)


def cancel_download_job(job_id: str) -> dict:
    job_id = str(job_id or "").strip()
    with DOWNLOAD_JOBS_LOCK:
        job = DOWNLOAD_JOBS.get(job_id)
        if job:
            job["cancel"].set()
            if job["status"] in ("queued", "resolving", "downloading"):
                job["message"] = "Cancelando…"
            else:
                job["message"] = "Ya no se puede cancelar: está instalando."
            return _job_view(job)
    raise AdbError("Ese trabajo de descarga ya no existe.")


def _read_downloader_payload() -> dict:
    try:
        raw = DOWNLOADER_FILE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"version": 1, "history": []}
    except OSError as exc:
        print(f"[FireDeck] No pude leer {DOWNLOADER_FILE.name}: {exc}")
        return {"version": 1, "history": []}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        print(f"[FireDeck] {DOWNLOADER_FILE.name} no es JSON válido; se ignora.")
        return {"version": 1, "history": []}
    if not isinstance(data, dict):
        return {"version": 1, "history": []}
    if not isinstance(data.get("history"), list):
        data["history"] = []
    return data


def _write_downloader_payload(payload: dict):
    tmp = DOWNLOADER_FILE.with_name(DOWNLOADER_FILE.name + ".tmp")
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    try:
        tmp.write_text(text, encoding="utf-8")
        try:
            tmp.replace(DOWNLOADER_FILE)
        except PermissionError:
            # OneDrive puede rechazar el reemplazo atómico de un marcador en la nube.
            DOWNLOADER_FILE.write_text(text, encoding="utf-8")
            tmp.unlink(missing_ok=True)
    except OSError as exc:
        print(f"[FireDeck] No pude guardar {DOWNLOADER_FILE.name}: {exc}")


def load_download_history() -> list[dict]:
    items = _read_downloader_payload().get("history") or []
    history = []
    for item in items:
        if not isinstance(item, dict):
            continue
        text = str(item.get("input") or "").strip()
        if not text:
            continue
        try:
            at = int(item.get("at") or 0)
        except (TypeError, ValueError):
            at = 0
        history.append(
            {
                "input": text,
                "source": str(item.get("source") or "url"),
                "filename": str(item.get("filename") or ""),
                "package": str(item.get("package") or ""),
                "ok": bool(item.get("ok", True)),
                "at": at,
            }
        )
    return history[:12]


def remember_download(
    input_text: str, source: str, filename: str, package: str = "", ok: bool = True
):
    text = str(input_text or "").strip()
    if not text:
        return
    entry = {
        "input": text[:2048],
        "source": source if source in ("code", "url") else "url",
        "filename": str(filename or "")[:160],
        "package": str(package or "")[:120],
        "ok": bool(ok),
        "at": int(time.time()),
    }
    with DOWNLOADER_LOCK:
        payload = _read_downloader_payload()
        history = [
            item
            for item in (payload.get("history") or [])
            if isinstance(item, dict) and str(item.get("input")) != entry["input"]
        ]
        history.insert(0, entry)
        payload = {"version": 1, "history": history[:12]}
        _write_downloader_payload(payload)


def clear_download_history():
    with DOWNLOADER_LOCK:
        _write_downloader_payload({"version": 1, "history": []})


PNG_SIG = b"\x89PNG\r\n\x1a\n"


def mdns_services() -> list[dict]:
    """Servicios ADB que anuncia el Fire TV por mDNS (Fire OS 8 incluido)."""
    try:
        rc, out, err = adb_run(["mdns", "services"], timeout=12)
    except AdbError:
        return []
    if rc != 0:
        return []
    services = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        name, kind, address = parts[0], parts[1], parts[-1]
        if "_adb" not in kind:
            continue
        host, sep, port = address.rpartition(":")
        if not sep or not host:
            continue
        try:
            port_number = int(port)
        except ValueError:
            continue
        services.append(
            {
                "name": name,
                "type": kind.strip("."),
                "host": host,
                "port": port_number,
                "pairing": "pairing" in kind,
            }
        )
    return services


def tcp_probe(host: str, port: int, timeout: float = 1.4) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        return sock.connect_ex((host, port)) == 0
    except OSError:
        return False
    finally:
        sock.close()


def screen_is_awake() -> bool | None:
    """True si la pantalla está encendida, False si está dormida, None si no se sabe."""
    try:
        rc, out = device_shell_quiet("dumpsys power", timeout=4)
    except AdbError:
        return None
    match = re.search(r"mWakefulness=(\w+)", out)
    if not match:
        return None
    return match.group(1).lower() in ("awake", "dreaming")


def screen_stats(raw: bytes) -> dict | None:
    """Analiza el volcado crudo de `screencap` y mide cuánto de la imagen es negro.

    Se usa el formato crudo porque leer los píxeles es inmediato; analizar el
    PNG exigiría descomprimir y desfiltrar toda la imagen.
    """
    if len(raw) < 16:
        return None
    width = int.from_bytes(raw[0:4], "little")
    height = int.from_bytes(raw[4:8], "little")
    pixel_format = int.from_bytes(raw[8:12], "little")
    if width <= 0 or height <= 0 or width * height > 30_000_000:
        return None
    stride = {1: 4, 2: 4, 3: 3, 4: 2}.get(pixel_format)
    if not stride:
        return None

    header = None
    for candidate in (16, 12):
        if len(raw) >= candidate + width * height * stride:
            header = candidate
            break
    if header is None:
        return None
    pixels = raw[header:]

    step_y = max(1, height // 90)
    step_x = max(1, width // 120)
    dark = bright_total = total = 0
    for y in range(0, height, step_y):
        row = y * width * stride
        for x in range(0, width, step_x):
            index = row + x * stride
            if stride >= 3:
                red, green, blue = pixels[index], pixels[index + 1], pixels[index + 2]
            else:
                value = pixels[index] | (pixels[index + 1] << 8)
                red = ((value >> 11) & 0x1F) * 255 // 31
                green = ((value >> 5) & 0x3F) * 255 // 63
                blue = (value & 0x1F) * 255 // 31
            total += 1
            luminance = (red * 299 + green * 587 + blue * 114) // 1000
            bright_total += luminance
            if luminance < 10:
                dark += 1

    if not total:
        return None
    ratio = dark / total
    return {
        "width": width,
        "height": height,
        "dark_ratio": round(ratio, 4),
        "mean_brightness": round(bright_total / total, 1),
        "black": ratio >= 0.985,
    }


def screenshot_png_via_file(serial: str) -> bytes:
    """Alternativa para adb de Windows que corrompe `exec-out`."""
    remote = "/sdcard/firedeck-shot.png"
    adb_run(["-s", serial, "shell", "screencap", "-p", remote], timeout=30)
    fd, tmp_name = tempfile.mkstemp(prefix="firedeck_shot_", suffix=".png")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        rc, out, err = adb_run(["-s", serial, "pull", remote, str(tmp)], timeout=30)
        data = tmp.read_bytes() if tmp.is_file() else b""
        adb_run(["-s", serial, "shell", "rm", "-f", remote], timeout=10)
        if not data.startswith(PNG_SIG):
            raise AdbError((out + "\n" + err).strip() or "La captura de pantalla falló.")
        return data
    finally:
        tmp.unlink(missing_ok=True)


def screenshot() -> tuple[bytes, dict | None]:
    serial = pick_serial()
    stats = None
    try:
        raw = adb_exec_out(["-s", serial, "exec-out", "screencap"], timeout=30)
        stats = screen_stats(raw)
    except AdbError:
        stats = None
    data = adb_exec_out(["-s", serial, "exec-out", "screencap", "-p"], timeout=30)
    if not data.startswith(PNG_SIG):
        data = screenshot_png_via_file(serial)
    return data, stats


# --------------------------------------------------------------------------
# Grabación de pantalla (screenrecord del propio Fire TV)
# --------------------------------------------------------------------------

def _new_recorder(serial: str) -> dict:
    return {
        "serial": serial,
        "proc": None,
        "remote": None,
        "started": 0.0,
        "duration": 0,
        "done": False,
        "error": "",
        "size": 0,
        "last_stat": 0.0,
    }


# Una grabación por dispositivo: dos pestañas pueden grabar Sala y Cuadro a la
# vez sin pisarse.
RECORDERS: dict[str, dict] = {}
RECORD_LOCK = threading.RLock()


def _recorder(serial: str, create: bool = False) -> dict | None:
    with RECORD_LOCK:
        rec = RECORDERS.get(serial)
        if rec is None and create:
            rec = _new_recorder(serial)
            RECORDERS[serial] = rec
        return rec


def _remote_size(serial: str, remote: str) -> int:
    try:
        rc, out, err = adb_run(
            ["-s", serial, "shell", f"stat -c %s {sh_quote(remote)} 2>/dev/null"],
            timeout=10,
        )
    except AdbError:
        return 0
    text = (out or "").strip()
    return int(text) if text.isdigit() else 0


def _empty_recording(serial: str | None) -> dict:
    return {
        "recording": False,
        "done": False,
        "error": "",
        "elapsed": 0.0,
        "remaining": 0.0,
        "duration": 0,
        "size": 0,
        "has_file": False,
        "serial": serial,
    }


def _record_finalize(rec: dict) -> None:
    proc = rec.get("proc")
    if not proc or rec.get("done"):
        return
    if proc.poll() is None:
        return
    output = ""
    try:
        if proc.stdout:
            output = _decode(proc.stdout.read())
    except (OSError, ValueError):
        output = ""
    serial = rec.get("serial")
    remote = rec.get("remote")
    size = _remote_size(serial, remote) if serial and remote else 0
    error = ""
    if size <= 0:
        error = (
            output.strip()
            or "screenrecord no generó ningún archivo. Puede que el Fire TV "
            "no permita grabar la pantalla en este momento."
        )
    rec.update(done=True, size=size, error=error)


def recording_status(serial: str | None = None) -> dict:
    serial = serial or pick_serial()
    with RECORD_LOCK:
        rec = RECORDERS.get(serial)
        if rec is None:
            return _empty_recording(serial)
        proc = rec.get("proc")
        _record_finalize(rec)
        recording = bool(proc and proc.poll() is None)
        now = time.time()
        if recording and now - rec.get("last_stat", 0) >= 2:
            size = _remote_size(rec["serial"], rec["remote"])
            rec["size"] = size
            rec["last_stat"] = now
        elapsed = max(0.0, now - rec["started"]) if rec.get("started") else 0.0
        remaining = 0.0
        if recording and rec.get("duration"):
            remaining = max(0.0, rec["duration"] - elapsed)
        has_file = bool(
            rec.get("done") and rec.get("size", 0) > 0 and not rec.get("error")
        )
        return {
            "recording": recording,
            "done": bool(rec.get("done")),
            "error": rec.get("error", ""),
            "elapsed": round(elapsed, 1),
            "remaining": round(remaining, 1),
            "duration": int(rec.get("duration") or 0),
            "size": int(rec.get("size") or 0),
            "has_file": has_file,
            "serial": rec.get("serial"),
        }


def start_recording(duration=30, bitrate=8_000_000) -> dict:
    try:
        duration = int(duration)
    except (TypeError, ValueError) as exc:
        raise AdbError("La duración de la grabación debe ser un número.") from exc
    duration = max(5, min(180, duration))
    try:
        bitrate = int(bitrate)
    except (TypeError, ValueError):
        bitrate = 8_000_000
    bitrate = max(1_000_000, min(20_000_000, bitrate))

    with RECORD_LOCK:
        serial = pick_serial()
        rec = _recorder(serial, create=True)
        proc = rec.get("proc")
        if proc and proc.poll() is None:
            raise AdbError("Ya hay una grabación en curso en este Fire TV.")
        with MIRROR_LOCK:
            if (MIRRORS.get(serial) or {}).get("proc"):
                raise AdbError(
                    "Ese Fire TV está enviando el espejo en vivo. Detén el espejo "
                    "antes de grabar."
                )
        if screen_is_awake() is False:
            raise AdbError(
                "El Fire TV está en suspensión: la grabación saldría en negro. "
                "Enciende la pantalla (tecla Inicio) y vuelve a intentarlo."
            )
        try:
            adb_run(
                ["-s", serial, "shell", "rm -f /sdcard/firedeck-rec-*.mp4"],
                timeout=15,
            )
        except AdbError:
            pass
        remote = f"/sdcard/firedeck-rec-{int(time.time())}.mp4"
        command = [
            ADB, "-s", serial, "shell", "screenrecord",
            "--bit-rate", str(bitrate),
            "--time-limit", str(duration),
            remote,
        ]
        try:
            proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
            )
        except (FileNotFoundError, OSError) as exc:
            raise AdbError("No pude iniciar screenrecord en el Fire TV.") from exc
        rec.update(
            serial=serial,
            proc=proc,
            remote=remote,
            started=time.time(),
            duration=duration,
            done=False,
            error="",
            size=0,
            last_stat=0.0,
        )
        return recording_status(serial)


def stop_recording(serial: str | None = None) -> dict:
    serial = serial or pick_serial()
    with RECORD_LOCK:
        rec = RECORDERS.get(serial)
        if rec is None:
            return _empty_recording(serial)
        proc = rec.get("proc")
        if not proc or proc.poll() is not None:
            _record_finalize(rec)
            return recording_status(serial)
        try:
            adb_run(
                [
                    "-s", serial, "shell",
                    "pkill -INT screenrecord 2>/dev/null || "
                    "killall -INT screenrecord 2>/dev/null || true",
                ],
                timeout=15,
            )
        except AdbError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        _record_finalize(rec)
        return recording_status(serial)


def recording_file(serial: str | None = None) -> tuple[Path, str]:
    serial = serial or pick_serial()
    with RECORD_LOCK:
        status = recording_status(serial)
        if status["recording"]:
            raise AdbError("Detén la grabación antes de descargar el vídeo.")
        if not status["done"]:
            raise AdbError("Todavía no hay ninguna grabación en este Fire TV.")
        if status["error"]:
            raise AdbError(status["error"])
        if not status["has_file"]:
            raise AdbError("La grabación no contiene datos.")
        rec = RECORDERS.get(serial) or {}
        remote = rec.get("remote")
        fd, tmp_name = tempfile.mkstemp(prefix="firedeck_rec_", suffix=".mp4")
        os.close(fd)
        local = Path(tmp_name)
        rc, text = _adb_pull(serial, remote, local, timeout=600)
        if rc != 0 or not local.is_file() or local.stat().st_size == 0:
            local.unlink(missing_ok=True)
            raise AdbError(text or "No pude descargar el vídeo del Fire TV.")
        try:
            adb_run(["-s", serial, "shell", f"rm -f {sh_quote(remote)}"], timeout=15)
        except AdbError:
            pass
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(rec.get("started") or time.time()))
        RECORDERS.pop(serial, None)
        return local, f"firedeck-{stamp}.mp4"


# --------------------------------------------------------------------------
# Espejo en vivo (H.264 del Fire TV, decodificado por el navegador)
# --------------------------------------------------------------------------

MIRROR_LOCK = threading.RLock()
# Un espejo por dispositivo: Sala y Cuadro pueden emitir a la vez desde dos
# pestañas distintas.
MIRRORS: dict[str, dict] = {}


def _stop_process(proc) -> None:
    if not proc:
        return
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
    except OSError:
        pass


# `screenrecord` con salida a tubería puede quedarse colgado tras SIGINT
# (esperando al codificador). Si eso pasa, el proceso sigue ocupando la
# pantalla: el siguiente espejo no recibe fotogramas y `screencap` falla.
# Se le da un margen para cerrar y, si no lo hace, se fuerza la salida.
SCREENRECORD_CLEANUP = (
    "pkill -INT screenrecord 2>/dev/null || true; "
    "n=0; while [ $n -lt 2 ] && pidof screenrecord >/dev/null 2>&1; do "
    "sleep 0.5; n=$((n+1)); done; "
    "pkill -9 screenrecord 2>/dev/null || true"
)


def stop_remote_screenrecord(serial: str) -> None:
    try:
        adb_run(["-s", serial, "shell", SCREENRECORD_CLEANUP], timeout=15)
    except AdbError:
        pass


def stop_mirror(serial: str, token: str | None = None, kill_remote: bool = True) -> None:
    """Detiene el espejo de `serial` (solo si sigue siendo el actual)."""
    with MIRROR_LOCK:
        state = MIRRORS.get(serial) or {}
        if token and state.get("token") != token:
            return
        proc = state.get("proc")
        MIRRORS.pop(serial, None)
    _stop_process(proc)
    if kill_remote:
        stop_remote_screenrecord(serial)


def stop_all_mirrors() -> None:
    with MIRROR_LOCK:
        serials = list(MIRRORS.keys())
    for serial in serials:
        stop_mirror(serial)


def shutdown_media() -> None:
    """Cierra grabaciones y espejos antes de apagar el servidor."""
    with RECORD_LOCK:
        serials = list(RECORDERS.keys())
    for serial in serials:
        try:
            stop_recording(serial)
        except Exception as exc:  # noqa: BLE001 - no debe impedir el apagado
            log_unexpected(f"No pude cerrar la grabación de {serial}", exc)
    stop_all_mirrors()


def mirror_chunks(serial: str, token: str):
    """Genera el H.264 en crudo (Annex B) del Fire TV.

    `screenrecord` limita cada sesión a 180 s, así que se reinicia solo. El
    navegador decodifica el flujo con WebCodecs y lo pinta en un canvas: no
    hace falta ffmpeg ni ninguna dependencia nueva en el PC.
    """
    short_failures = 0
    while True:
        # Si otra petición (o el botón «En vivo») detuvo el espejo, este hilo
        # no debe volver a arrancar screenrecord: competiría con el nuevo.
        with MIRROR_LOCK:
            if (MIRRORS.get(serial) or {}).get("token") != token:
                return
        started = time.time()
        command = [
            ADB, "-s", serial, "exec-out", "screenrecord",
            "--output-format=h264",
            "--bit-rate", "6000000",
            "--time-limit", "180",
            "-",
        ]
        try:
            proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0,
            )
        except (FileNotFoundError, OSError) as exc:
            raise AdbError("No pude iniciar screenrecord para el espejo.") from exc
        with MIRROR_LOCK:
            MIRRORS[serial] = {"token": token, "proc": proc}
        try:
            while True:
                chunk = proc.stdout.read1(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            # El token se conserva hasta que el manejador de la petición
            # termine: así `stop_mirror` puede matar también el screenrecord
            # remoto cuando el navegador corta la conexión.
            _stop_process(proc)
        # screenrecord terminó. Si fue por el límite de 180 s, se reanuda; si
        # murió nada más arrancar (pantalla dormida, otro screenrecord…), se
        # corta para no entrar en un bucle.
        if time.time() - started < 3:
            short_failures += 1
            if short_failures >= 2:
                raise AdbError(
                    "El Fire TV dejó de emitir vídeo. Comprueba que la pantalla "
                    "está encendida y que no hay otra grabación en curso."
                )
            time.sleep(0.5)
        else:
            short_failures = 0


def discover(port: int = 5555) -> list[str]:
    """Escanea la /24 local buscando el puerto ADB."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        local_ip = sock.getsockname()[0]
        sock.close()
    except OSError:
        try:
            local_ip = socket.gethostbyname(socket.gethostname())
        except OSError:
            local_ip = "127.0.0.1"
    base = ".".join(local_ip.split(".")[:3])

    def probe(host: str) -> str | None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.18)
        try:
            if s.connect_ex((host, port)) == 0:
                return host
        finally:
            s.close()
        return None

    hosts = [f"{base}.{i}" for i in range(1, 255)]
    with ThreadPoolExecutor(max_workers=80) as pool:
        found = [h for h in pool.map(probe, hosts) if h]
    return found


# --------------------------------------------------------------------------
# Herramientas de red (2.6)
# --------------------------------------------------------------------------

NETWORK_SCRIPT = r"""
echo @@ip@@; ip addr show wlan0 2>/dev/null
echo @@route@@; ip route show table all 2>/dev/null
echo @@hostname@@; getprop net.hostname
echo @@hardware@@; getprop ro.serialno
echo @@wifi@@; dumpsys wifi 2>/dev/null
echo @@connectivity@@; dumpsys connectivity 2>/dev/null | grep -m 1 'NetworkAgentInfo'
echo @@pings@@; gw=$(ip route show table all 2>/dev/null | awk '/^default via/ {print $3; exit}'); echo "GATEWAY=$gw"; ping -c 2 -W 2 "$gw" 2>&1 | tail -n 4; echo "INTERNET=8.8.8.8"; ping -c 2 -W 2 8.8.8.8 2>&1 | tail -n 4
echo @@end@@
"""


def _netmask_from_prefix(prefix: int) -> str:
    if not 0 <= prefix <= 32:
        return ""
    value = (0xFFFFFFFF << (32 - prefix)) & 0xFFFFFFFF if prefix else 0
    return ".".join(str((value >> shift) & 0xFF) for shift in (24, 16, 8, 0))


def _ping_summary(text: str) -> dict | None:
    """Resume la salida de ping de Android (pérdida y tiempos en ms)."""
    if not (text or "").strip():
        return None
    data: dict = {}
    loss = re.search(r"(\d+)% packet loss", text)
    sent = re.search(r"(\d+) packets transmitted, (\d+) received", text)
    times = re.search(
        r"rtt min/avg/max/[^=]+=\s*([\d.]+)/([\d.]+)/([\d.]+)", text
    )
    if loss:
        data["loss"] = int(loss.group(1))
    if sent:
        data["sent"] = int(sent.group(1))
        data["received"] = int(sent.group(2))
    if times:
        data["min"] = float(times.group(1))
        data["avg"] = float(times.group(2))
        data["max"] = float(times.group(3))
    return data or None


def _wifi_quality(rssi) -> int | None:
    try:
        value = int(rssi)
    except (TypeError, ValueError):
        return None
    return max(0, min(100, 2 * (value + 100)))


def _wifi_security(key_mgmt: str, protocols: str) -> str:
    name = (key_mgmt or "").strip().upper()
    if not name:
        return ""
    rsn = "RSN" in (protocols or "").upper()
    if name == "NONE":
        return "Abierta"
    if "SAE" in name:
        return "WPA3-SAE"
    if "WPA_PSK" in name:
        return "WPA2-PSK" if rsn else "WPA-PSK"
    if "WPA_EAP" in name or "IEEE8021X" in name:
        return "WPA2-Enterprise" if rsn else "WPA-Enterprise"
    return name.replace("_", "-")


def _parse_wifi_dump(text: str) -> dict:
    """Lee SSID, señal y enlace del volcado de `dumpsys wifi`."""
    wifi = {
        "connected": False,
        "ssid": "",
        "rssi": None,
        "quality": None,
        "link_mbps": None,
        "frequency_mhz": None,
        "band": "",
        "security": "",
        "dhcp": "",
    }
    info = re.search(r"mWifiInfo (.*)", text or "")
    net_id = None
    if info:
        line = info.group(1)
        wifi["connected"] = "Supplicant state: COMPLETED" in line

        def field(name: str) -> str:
            found = re.search(rf"{name}: ([^,]+)", line)
            return found.group(1).strip() if found else ""

        wifi["ssid"] = field("SSID")
        rssi = field("RSSI")
        if re.fullmatch(r"-?\d+", rssi):
            wifi["rssi"] = int(rssi)
            wifi["quality"] = _wifi_quality(wifi["rssi"])
        link = re.search(r"(\d+)", field("Link speed"))
        if link:
            wifi["link_mbps"] = int(link.group(1))
        freq = re.search(r"(\d+)", field("Frequency"))
        if freq:
            wifi["frequency_mhz"] = int(freq.group(1))
        net = field("Net ID")
        if re.fullmatch(r"\d+", net):
            net_id = int(net)

    config_block = ""
    if net_id is not None:
        config = re.search(rf'(?m)^\s*ID: {net_id} SSID: "(.*?)"', text or "")
        if config:
            config_block = text[config.end() : config.end() + 1500]
            if not wifi["ssid"] or wifi["ssid"].upper() == "XXX":
                wifi["ssid"] = config.group(1)
    if not config_block:
        # Sin Net ID utilizable: primer bloque de red guardada como aproximación.
        config = re.search(r'(?m)^\s*ID: \d+ SSID: "(.*?)"', text or "")
        if config:
            config_block = text[config.end() : config.end() + 1500]
            if not wifi["ssid"] or wifi["ssid"].upper() == "XXX":
                wifi["ssid"] = config.group(1)
    if config_block:
        key = re.search(r"KeyMgmt: ([^\n]+)", config_block)
        protocols = ""
        if key:
            protocols = key.group(1)
        wifi["security"] = _wifi_security(key.group(1) if key else "", protocols)
        dhcp = re.search(r"IP assignment: (\w+)", config_block)
        if dhcp:
            wifi["dhcp"] = dhcp.group(1)

    frequency = wifi["frequency_mhz"]
    if frequency:
        if frequency >= 5925:
            wifi["band"] = "6 GHz"
        elif frequency >= 4900:
            wifi["band"] = "5 GHz"
        else:
            wifi["band"] = "2.4 GHz"
    return wifi


def _tcp_latency(host: str, port: int, samples: int = 4, timeout: float = 0.6) -> dict | None:
    """Latencia PC→Fire TV medida con el saludo TCP del puerto ADB."""
    values: list[float] = []
    for _ in range(samples):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        start = time.perf_counter()
        try:
            result = sock.connect_ex((host, port))
        except OSError:
            result = -1
        finally:
            sock.close()
        if result != 0:
            break
        values.append(round((time.perf_counter() - start) * 1000, 1))
    if not values:
        return None
    return {
        "samples": values,
        "min": min(values),
        "avg": round(sum(values) / len(values), 1),
        "max": max(values),
        "loss": round((samples - len(values)) * 100 / samples),
    }


def network_target(devices: list[dict] | None = None) -> str:
    """Dispositivo que muestra la pestaña Red, aunque ahora esté desconectado."""
    serial = current_selection()
    if not valid_serial(serial):
        serial = saved_selection()
    if not serial:
        try:
            devices = devices if devices is not None else list_devices()
        except AdbError:
            devices = []
        online = [device for device in devices if device["state"] == "device"]
        serial = online[0]["serial"] if online else ""
    return serial if valid_serial(serial) else ""


def network_overview() -> dict:
    """Estado de red del Fire TV elegido: ADB, Wi-Fi, IP y latencias."""
    try:
        devices = list_devices()
    except AdbError:
        devices = []
    records = saved_device_records(devices)
    serial = network_target(devices)
    checked = int(time.time())
    if not serial:
        return {
            "device": None,
            "saved": records,
            "checked": checked,
            "notes": ["Conecta un Fire TV para ver su estado de red."],
        }

    host, port = serial_host_port(serial)
    live = next((device for device in devices if device["serial"] == serial), None)
    saved = next((item for item in load_saved_devices() if item["serial"] == serial), None)
    previous_hardware = str((saved or {}).get("hardware") or "")
    previous_ip = str((saved or {}).get("previous_ip") or "")
    reachable = tcp_probe(host, port, timeout=0.7)
    latency = _tcp_latency(host, port) if reachable else None

    sections: dict[str, str] = {}
    shell_error = ""
    if live and live["state"] == "device":
        try:
            rc, out, err = adb_run(["-s", serial, "shell", NETWORK_SCRIPT], timeout=50)
            sections = _profile_sections(out)
            if rc != 0 and not out:
                shell_error = err or "El Fire TV no devolvió datos de red."
        except AdbError as exc:
            shell_error = str(exc)

    hardware = (sections.get("hardware") or "").strip().splitlines()
    hardware = hardware[0].strip() if hardware else ""
    hardware_conflict = bool(
        previous_hardware and hardware and previous_hardware != hardware
    )
    if hardware and not hardware_conflict:
        remember_device_network(serial, hardware=hardware, ip=host)
        if saved:
            saved["hardware"] = hardware

    ip_info = {
        "interface": "wlan0",
        "address": "",
        "prefix": None,
        "netmask": "",
        "gateway": "",
        "mac": "",
        "hostname": (sections.get("hostname") or "").strip(),
    }
    net_text = sections.get("ip") or ""
    address = re.search(r"inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", net_text)
    if address:
        ip_info["address"] = address.group(1)
        ip_info["prefix"] = int(address.group(2))
        ip_info["netmask"] = _netmask_from_prefix(ip_info["prefix"])
    mac = re.search(r"link/ether\s+([0-9a-fA-F:]{17})", net_text)
    if mac:
        ip_info["mac"] = mac.group(1).upper()
    gateway = re.search(
        r"(?m)^default via (\d+\.\d+\.\d+\.\d+)", sections.get("route") or ""
    )
    if gateway:
        ip_info["gateway"] = gateway.group(1)

    wifi = _parse_wifi_dump(sections.get("wifi") or "")
    connectivity = sections.get("connectivity") or ""
    internet = {
        "validated": (bool(re.search(r"\bVALIDATED\b", connectivity)) if connectivity else None),
        "metered": ("NOT_METERED" not in connectivity) if connectivity else None,
        "signal": None,
    }
    signal = re.search(r"SignalStrength: (-?\d+)", connectivity)
    if signal:
        internet["signal"] = int(signal.group(1))

    pings_text = sections.get("pings") or ""
    gateway_text, _, internet_text = pings_text.partition("INTERNET=")
    pings = {
        "gateway": _ping_summary(gateway_text),
        "internet": _ping_summary(internet_text),
    }
    gateway_match = re.search(r"GATEWAY=(\S*)", gateway_text)
    if pings["gateway"]:
        pings["gateway"]["target"] = (
            gateway_match.group(1) if gateway_match else ip_info["gateway"]
        )
    if pings["internet"]:
        pings["internet"]["target"] = "8.8.8.8"

    notes: list[str] = []
    adb_state = (live or {}).get("state") or "offline"
    if adb_state == "unauthorized":
        notes.append("El Fire TV está esperando que aceptes el aviso de depuración ADB.")
    elif adb_state != "device":
        notes.append("ADB no ve este Fire TV; puede haber cambiado de IP o estar apagado.")
    if not reachable:
        notes.append(f"{host}:{port} no responde desde este PC.")
    if shell_error:
        notes.append(shell_error)
    if hardware_conflict:
        notes.append(
            "Esta IP responde con otro Fire TV distinto del guardado; el DHCP pudo "
            "reasignarla. Usa «Buscar de nuevo» para localizar el tuyo."
        )
    if not saved:
        notes.append("Este dispositivo aún no está en la lista guardada.")

    return {
        "device": {
            "serial": serial,
            "host": host,
            "port": port,
            "name": (saved or {}).get("name") or (live or {}).get("model") or serial,
            "model": (live or {}).get("model") or (saved or {}).get("model") or "",
            "alias": (saved or {}).get("name") or "",
            "saved": bool(saved),
            "state": adb_state,
            "online": adb_state == "device",
            "reachable": reachable,
            "hardware": hardware or previous_hardware,
            "latency": latency,
        },
        "wifi": wifi,
        "ip": ip_info,
        "internet": internet,
        "pings": pings,
        "ip_change": {
            "changed": bool(previous_ip and previous_ip != host),
            "previous": previous_ip if previous_ip and previous_ip != host else "",
            "current": host,
            "reused": hardware_conflict,
            "previous_hardware": previous_hardware if hardware_conflict else "",
        },
        "saved": records,
        "checked": checked,
        "notes": notes,
    }


def network_reconnect(serial: str | None = None) -> dict:
    """Corta y rehace la conexión ADB con el Fire TV elegido."""
    serial = str(serial or network_target()).strip()
    if not serial:
        raise AdbError("No hay ningún Fire TV seleccionado.")
    if not valid_serial(serial):
        raise AdbError("La dirección del Fire TV no es válida.")
    host, port = serial_host_port(serial)
    if ":" not in serial:
        raise AdbError("Solo se pueden reconectar dispositivos por Wi-Fi (IP:puerto).")
    adb_run(["disconnect", serial], timeout=15)
    rc, out, err = adb_run(["connect", serial], timeout=25)
    text = (out + "\n" + err).strip()
    if "connected to" not in text.lower():
        raise AdbError(
            f"No pude reconectar con {host}:{port}. Comprueba que el Fire TV esté "
            "encendido y que la depuración ADB siga activa."
            + (f" adb dijo: {text}" if text else "")
        )
    rc, out, err = adb_run(["-s", serial, "get-state"], timeout=15)
    state = (out or err).strip()
    if state != "device":
        raise AdbError(
            f"El Fire TV respondió, pero ADB lo ve como «{state or 'desconocido'}». "
            "Acepta el aviso de depuración en el televisor si aparece."
        )
    set_selected(serial)
    remember_device(serial, model=device_model(serial), selected=True)
    return {
        "serial": serial,
        "host": host,
        "port": port,
        "message": f"Reconectado con {host}:{port}.",
    }


def network_scan() -> dict:
    """Busca Fire TV en la red y reengancha los que hayan cambiado de IP."""
    try:
        devices = list_devices()
    except AdbError:
        devices = []
    live_by_serial = {device["serial"]: device for device in devices}

    hosts = set(discover(5555))
    for device in devices:
        if device["state"] == "device":
            hosts.add(serial_host_port(device["serial"])[0])
    for service in mdns_services():
        if not service.get("pairing"):
            hosts.add(str(service.get("host") or ""))

    host_list = sorted(host for host in hosts if host)
    found: list[dict] = []
    moved: list[dict] = []
    notes: list[str] = []

    for host in host_list:
        serial = f"{host}:5555"
        live = live_by_serial.get(serial)
        state = (live or {}).get("state") or "offline"
        if state != "device":
            rc, out, err = adb_run(["connect", serial], timeout=10)
            text = (out + "\n" + err).strip()
            low = text.lower()
            if "connected to" not in low and "already connected" not in low:
                if state == "unauthorized" or "unauthorized" in low or "authenticate" in low:
                    state = "unauthorized"
                else:
                    continue
            rc, out, err = adb_run(["-s", serial, "get-state"], timeout=10)
            state = (out or err).strip() or state
        if state == "unauthorized":
            found.append(
                {
                    "serial": serial,
                    "host": host,
                    "port": 5555,
                    "state": "unauthorized",
                    "model": (live or {}).get("model") or "",
                    "hardware": "",
                    "name": "",
                    "saved": False,
                }
            )
            continue
        if state != "device":
            continue

        rc, out, err = adb_run(
            ["-s", serial, "shell", "getprop ro.serialno; getprop ro.product.model"],
            timeout=12,
        )
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        hardware = lines[0] if lines else ""
        model = lines[1] if len(lines) > 1 else (live or {}).get("model", "")

        saved_items = load_saved_devices()
        exact = next((item for item in saved_items if item["serial"] == serial), None)
        match = None
        if hardware:
            match = next(
                (
                    item
                    for item in saved_items
                    if item.get("hardware") == hardware and item["serial"] != serial
                ),
                None,
            )
        entry_name = ""
        if exact is not None:
            entry_name = exact.get("name") or ""
            if hardware:
                remember_device_network(serial, hardware=hardware, ip=host)
        if match is not None:
            moved_entry = move_saved_device(
                match["serial"], serial, hardware=hardware, model=model
            )
            moved.append(
                {
                    "name": moved_entry.get("name")
                    or moved_entry.get("model")
                    or match["serial"],
                    "from": match["serial"],
                    "to": serial,
                }
            )
            entry_name = moved_entry.get("name") or entry_name
            with TABS_LOCK:
                for tab_entry in TABS.values():
                    if tab_entry.get("serial") == match["serial"]:
                        tab_entry["serial"] = serial
            if STATE.get("serial") == match["serial"]:
                STATE["serial"] = serial
        found.append(
            {
                "serial": serial,
                "host": host,
                "port": 5555,
                "state": "device",
                "model": model,
                "hardware": hardware,
                "name": entry_name,
                "saved": bool(exact or match),
            }
        )

    if moved:
        resumen = "; ".join(
            f"{item['name']} pasó de {item['from']} a {item['to']}" for item in moved
        )
        notes.append("Cambio de IP detectado: " + resumen + ".")
        selected = current_selection()
        if selected and any(item["to"] == selected for item in moved):
            remember_device(selected, selected=True)
    else:
        notes.append(
            "No se detectaron cambios de IP entre los dispositivos guardados."
            if found
            else "No se encontró ningún Fire TV con el puerto ADB abierto."
        )
    return {
        "found": found,
        "moved": moved,
        "scanned": len(host_list),
        "selected": current_selection(),
        "notes": notes,
    }


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".webp": "image/webp",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "FireDeck/1.1"
    protocol_version = "HTTP/1.1"

    # -- helpers -------------------------------------------------------
    def log_message(self, fmt, *args):
        if args and "/api/screenshot" in str(args[0]):
            return
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        return self.rfile.read(length) if length else b""

    def read_body_to_file(self, limit: int = INSTALL_MAX_UPLOAD, suffix: str = ".bin") -> Path:
        """Guarda el cuerpo de la petición en un temporal sin cargarlo en RAM."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            raise AdbError("La subida llegó vacía o sin tamaño.")
        if length > limit:
            self.close_connection = True
            raise AdbError(
                "El paquete supera el límite de "
                + human_size(limit / 1024)
                + " por subida."
            )
        fd, name = tempfile.mkstemp(prefix="firedeck_upload_", suffix=suffix)
        target = Path(name)
        remaining = length
        try:
            with os.fdopen(fd, "wb") as output:
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise AdbError("La subida se interrumpió antes de terminar.")
                    output.write(chunk)
                    remaining -= len(chunk)
            return target
        except Exception:
            target.unlink(missing_ok=True)
            self.close_connection = True
            raise

    def read_json(self) -> dict:
        raw = self.read_body()
        if not raw:
            return {}
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise AdbError("La petición debe enviarse como application/json.")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AdbError("JSON inválido en la petición.") from exc

    def guard(self, fn):
        try:
            return fn()
        except AdbError as exc:
            payload = {"ok": False, "error": str(exc)}
            if getattr(exc, "hint", None):
                payload["hint"] = exc.hint
            self.send_json(payload, 400)
        except ConnectionError:
            # El navegador cerró la conexión (pestaña recargada o cerrada) a
            # mitad de la respuesta. No es un fallo ni hay a quién responder.
            self.close_connection = True
        except Exception as exc:  # noqa: BLE001
            log_unexpected("Error inesperado", exc)
            self.send_json({"ok": False, "error": f"Error interno: {exc}"}, 500)

    def serve_file(self, path: Path):
        if not path.is_file():
            self.send_json({"ok": False, "error": "Archivo no encontrado."}, 404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(path.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def send_download(self, path: Path, filename: str, mime: str):
        """Envía un archivo en streaming (APK o vídeo) como descarga."""
        if not path.is_file():
            self.send_json({"ok": False, "error": "El archivo ya no está disponible."}, 404)
            return
        size = path.stat().st_size
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", filename) or "firedeck.bin"
        disposition = f'attachment; filename="{safe}"'
        encoded = quote(filename, safe="")
        if encoded != safe:
            disposition += f"; filename*=UTF-8''{encoded}"
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", disposition)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            with path.open("rb") as fh:
                while True:
                    chunk = fh.read(256 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (ConnectionError, BrokenPipeError, ConnectionAbortedError):
            return

    # -- protección contra webs externas (CSRF / DNS rebinding) ---------
    def request_allowed(self) -> bool:
        """Solo acepta peticiones hechas por la propia interfaz de FireDeck.

        Un servidor en localhost sin esta comprobación puede ser manejado por
        cualquier web abierta en el navegador (POST «simple» entre orígenes) o
        mediante DNS rebinding. Se exige que:
          - Host sea localhost o una IP literal (un nombre de dominio ajeno
            apuntando a 127.0.0.1 es justo el ataque de rebinding);
          - Origin, si viene, coincida con Host (mismo origen);
          - Sec-Fetch-Site, si viene, no sea cross-site/same-site.
        """
        host_header = (self.headers.get("Host") or "").strip()
        try:
            hostname = urlparse("//" + host_header).hostname or ""
        except ValueError:
            return False
        if hostname != "localhost" and not hostname.endswith(".localhost"):
            try:
                ipaddress.ip_address(hostname)
            except ValueError:
                return False
        origin = self.headers.get("Origin")
        if origin is not None and origin != "null":
            if urlparse(origin).netloc.lower() != host_header.lower():
                return False
        elif origin == "null":
            return False
        site = (self.headers.get("Sec-Fetch-Site") or "").lower()
        if site and site not in ("same-origin", "none"):
            return False
        return True

    def reject_foreign(self) -> bool:
        if self.request_allowed():
            return False
        self.close_connection = True  # el cuerpo sin leer no debe reutilizarse
        self.send_json(
            {"ok": False, "error": "Petición rechazada: no viene de la interfaz de FireDeck."},
            403,
        )
        return True

    # -- GET -----------------------------------------------------------
    def begin_request(self, query):
        """Resuelve la pestaña que hace la petición (cabecera o ?tab=)."""
        tab = self.headers.get(TAB_HEADER) or query.get("tab", [""])[0]
        bind_tab(tab)

    def do_GET(self):
        if self.reject_foreign():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        self.begin_request(query)

        if path in ("/", "/index.html"):
            self.serve_file(STATIC / "index.html")
            return
        if path.startswith("/static/"):
            rel = path[len("/static/"):]
            target = (STATIC / rel).resolve()
            if STATIC.resolve() not in target.parents:
                self.send_json({"ok": False, "error": "Ruta inválida."}, 403)
                return
            self.serve_file(target)
            return

        if path == "/api/status":
            self.guard(self.api_status)
            return
        if path == "/api/apps":
            self.guard(lambda: self.api_apps(query))
            return
        if path == "/api/debloat":
            self.guard(lambda: self.send_json({"ok": True, **list_system_apps()}))
            return
        if path == "/api/storage":
            self.guard(lambda: self.send_json({"ok": True, **storage_info()}))
            return
        if path == "/api/screenshot":
            self.guard(self.api_screenshot)
            return
        if path == "/api/discover":
            try:
                port = int(query.get("port", ["5555"])[0])
            except ValueError:
                port = 5555
            self.guard(
                lambda: self.send_json(
                    {"ok": True, "hosts": discover(port), "mdns": mdns_services()}
                )
            )
            return
        if path == "/api/diagnose":
            self.guard(lambda: self.api_diagnose(query))
            return
        if path == "/api/devices":
            self.guard(
                lambda: self.send_json({"ok": True, "devices": saved_device_records()})
            )
            return
        if path == "/api/device/info":
            self.guard(
                lambda: self.send_json({"ok": True, "device": device_profile()})
            )
            return
        if path == "/api/network":
            self.guard(lambda: self.send_json({"ok": True, **network_overview()}))
            return
        if path == "/api/apps/icon":
            self.guard(lambda: self.api_app_icon(query))
            return
        if path == "/api/apps/apk":
            self.guard(lambda: self.api_app_apk(query))
            return
        if path == "/api/files":
            self.guard(
                lambda: self.send_json(
                    {"ok": True, **list_files(query.get("path", [FILE_ROOT])[0])}
                )
            )
            return
        if path == "/api/files/download":
            self.guard(lambda: self.api_file_download(query))
            return
        if path == "/api/screenrecord/status":
            self.guard(lambda: self.send_json({"ok": True, **recording_status()}))
            return
        if path == "/api/screenrecord/download":
            self.guard(self.api_record_download)
            return
        if path == "/api/mirror/raw":
            self.guard(self.api_mirror_raw)
            return
        if path == "/api/downloader/history":
            self.guard(
                lambda: self.send_json({"ok": True, "history": load_download_history()})
            )
            return
        if path == "/api/downloader/job":
            self.guard(
                lambda: self.send_json(
                    {"ok": True, "job": download_job(query.get("id", [""])[0])}
                )
            )
            return

        self.send_json({"ok": False, "error": "Ruta no encontrada."}, 404)

    def api_status(self):
        try:
            version_raw = adb_run(["version"])[1]
        except AdbError:
            version_raw = ""
        version = version_raw.splitlines()[0] if version_raw else "adb no disponible"
        try:
            devices = list_devices()
        except AdbError as exc:
            devices = []
            print(f"[FireDeck] {exc}")
        info = current_device_info()
        saved = saved_device_records(devices)
        aliases = {item["serial"]: item.get("alias") or "" for item in saved}
        for device in devices:
            device["alias"] = aliases.get(device["serial"], "")
        selected = current_selection()
        if info:
            info["alias"] = aliases.get(info["serial"], "")
        self.send_json(
            {
                "ok": True,
                "adb": ADB,
                "adb_version": version,
                "devices": devices,
                "saved": saved,
                "selected": selected,
                "selected_name": aliases.get(selected, ""),
                "device": info,
                "tab": current_tab_id(),
                "tabs": tab_summary(aliases),
                "keys": adb_key_info(),
                "uptime": int(time.time() - START_TIME),
            }
        )

    def api_apps(self, query):
        include_system = query.get("system", ["0"])[0] in ("1", "true")
        apps = list_apps(include_system)
        self.send_json(
            {
                "ok": True,
                "apps": apps,
                "packages": [app["package"] for app in apps],
                "count": len(apps),
            }
        )

    def api_app_info(self):
        package = str(self.read_json().get("package", "")).strip()
        if not package:
            raise AdbError("Falta el paquete de la app.")
        self.send_json({"ok": True, "app": app_details(package)})

    def api_debloat_toggle(self, enabled: bool):
        payload = self.read_json()
        package = str(payload.get("package", "")).strip()
        if not package:
            raise AdbError("Falta el paquete de la app.")
        message = set_system_app_state(package, enabled, bool(payload.get("confirm")))
        self.send_json({"ok": True, "message": message})

    def api_debloat_restore(self):
        self.send_json({"ok": True, **restore_system_apps()})

    def api_network_reconnect(self):
        payload = self.read_json()
        serial = str(payload.get("serial", "")).strip() or None
        self.send_json({"ok": True, **network_reconnect(serial)})

    def api_network_scan(self):
        self.send_json({"ok": True, **network_scan()})

    def api_mirror_raw(self):
        """Flujo H.264 continuo del Fire TV para el espejo del navegador."""
        serial = pick_serial()
        with RECORD_LOCK:
            rec = RECORDERS.get(serial) or {}
            proc = rec.get("proc")
            if proc and proc.poll() is None:
                raise AdbError(
                    "Detén la grabación de pantalla de este Fire TV antes de "
                    "abrir el espejo."
                )
        if screen_is_awake() is False:
            raise AdbError(
                "El Fire TV está en suspensión: el espejo saldría en negro. "
                "Enciende la pantalla (tecla Inicio) y vuelve a activarlo."
            )
        stop_mirror(serial)
        # Un reinicio del servidor deja el screenrecord del Fire TV huérfano
        # hasta 180 s y bloquearía el espejo nuevo: se limpia siempre.
        stop_remote_screenrecord(serial)
        token = f"{os.getpid()}-{time.time_ns()}"
        # Se registra antes de abrir el flujo: el generador comprueba este
        # token para no resucitar screenrecord después de un «Detener».
        with MIRROR_LOCK:
            MIRRORS[serial] = {"token": token, "proc": None}
        self.send_response(200)
        self.send_header("Content-Type", "video/h264")
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("X-FireDeck-Mirror", "h264")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            for chunk in mirror_chunks(serial, token):
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        except AdbError as exc:
            # Las cabeceras ya se enviaron: no se puede responder con JSON.
            print(f"[FireDeck] Espejo: {exc}")
        finally:
            stop_mirror(serial, token)

    def api_mirror_stop(self):
        payload = self.read_json()
        serial = str(payload.get("serial", "")).strip()
        if not valid_serial(serial):
            serial = pick_serial()
        stop_mirror(serial)
        self.send_json({"ok": True, "serial": serial})

    def api_screenshot(self):
        data, stats = screenshot()
        if stats and stats["black"]:
            awake = screen_is_awake()
            if awake is False:
                message = (
                    "El Fire TV está en suspensión: la captura sale en negro porque la "
                    "pantalla está apagada. Envía una tecla (Inicio) para despertarlo y "
                    "vuelve a capturar."
                )
            else:
                message = (
                    "La captura sale completamente en negro. Fire OS no permite capturar "
                    "la capa de vídeo ni el contenido protegido (Netflix, Prime Video, "
                    "Disney+…). Prueba desde el menú de inicio del Fire TV: si ahí tampoco "
                    "se ve, la app que está en pantalla está usando una superficie segura."
                )
            self.send_json(
                {
                    "ok": False,
                    "black": True,
                    "asleep": awake is False,
                    "stats": stats,
                    "error": message,
                },
                409,
            )
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("X-FireDeck-Shot", "ok")
        self.end_headers()
        self.wfile.write(data)

    # -- POST ----------------------------------------------------------
    def do_POST(self):
        if self.reject_foreign():
            return
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        self.begin_request(query)

        if path == "/api/connect":
            self.guard(self.api_connect)
        elif path == "/api/pair":
            self.guard(self.api_pair)
        elif path == "/api/disconnect":
            self.guard(self.api_disconnect)
        elif path == "/api/adb/repair":
            self.guard(self.api_repair)
        elif path == "/api/select":
            self.guard(self.api_select)
        elif path == "/api/devices/name":
            self.guard(self.api_name_device)
        elif path == "/api/devices/forget":
            self.guard(self.api_forget_device)
        elif path == "/api/key":
            self.guard(lambda: self._result(send_key(str(self.read_json().get("key", "")))))
        elif path == "/api/text":
            self.guard(lambda: self._result(send_text(str(self.read_json().get("text", "")))))
        elif path == "/api/shell":
            self.guard(self.api_shell)
        elif path == "/api/apps/action":
            self.guard(self.api_app_action)
        elif path == "/api/apps/info":
            self.guard(self.api_app_info)
        elif path == "/api/debloat/disable":
            self.guard(lambda: self.api_debloat_toggle(False))
        elif path == "/api/debloat/enable":
            self.guard(lambda: self.api_debloat_toggle(True))
        elif path == "/api/debloat/restore":
            self.guard(self.api_debloat_restore)
        elif path == "/api/network/reconnect":
            self.guard(self.api_network_reconnect)
        elif path == "/api/network/scan":
            self.guard(self.api_network_scan)
        elif path == "/api/storage/trim":
            self.guard(self.api_trim)
        elif path == "/api/install":
            self.guard(self.api_install)
        elif path == "/api/downloader/resolve":
            self.guard(self.api_downloader_resolve)
        elif path == "/api/downloader/install":
            self.guard(self.api_downloader_install)
        elif path == "/api/downloader/cancel":
            self.guard(self.api_downloader_cancel)
        elif path == "/api/downloader/history/clear":
            self.guard(self.api_downloader_history_clear)
        elif path == "/api/reboot":
            self.guard(self.api_reboot)
        elif path == "/api/input":
            self.guard(self.api_input)
        elif path == "/api/screenrecord/start":
            self.guard(self.api_record_start)
        elif path == "/api/screenrecord/stop":
            self.guard(self.api_record_stop)
        elif path == "/api/mirror/stop":
            self.guard(self.api_mirror_stop)
        elif path == "/api/files/upload":
            self.guard(lambda: self.api_file_upload(query))
        elif path == "/api/files/mkdir":
            self.guard(self.api_file_mkdir)
        elif path == "/api/files/rename":
            self.guard(self.api_file_rename)
        elif path == "/api/files/delete":
            self.guard(self.api_file_delete)
        else:
            self.send_json({"ok": False, "error": "Ruta no encontrada."}, 404)

    def _result(self, output: str):
        self.send_json({"ok": True, "output": output})

    def _auth_message(self, host: str, port: int) -> str:
        detail = ""
        try:
            states = [d["state"] for d in list_devices() if d["serial"].startswith(host)]
            if states:
                detail = f" adb lo ve como «{', '.join(sorted(set(states)))}»."
        except AdbError:
            pass
        return (
            f"El Fire TV ({host}:{port}) rechazó la clave de este PC.{detail} "
            "Las causas habituales son: (1) el aviso «¿Permitir depuración USB?» no se "
            "aceptó; la TV debe estar encendida y despierta para mostrarlo, (2) hay una "
            "clave vieja guardada en este PC, o (3) es Fire OS 8 y hay que emparejar con "
            "el código y el puerto que muestra «Emparejar dispositivo», no con el 5555. "
            "Usa «Diagnóstico» en este mismo diálogo y, si aparece el aviso en la TV, "
            "marca «Permitir siempre»."
        )

    def _auto_connect(self, host: str) -> tuple[bool, str]:
        """Tras emparejar, intenta conectar probando 5555 y lo que anuncie mDNS."""
        notes = []
        candidates = [(host, 5555)]
        for service in mdns_services():
            if service["host"] == host and not service["pairing"]:
                candidates.append((host, service["port"]))
        seen = set()
        for candidate_host, candidate_port in candidates:
            if candidate_port in seen:
                continue
            seen.add(candidate_port)
            serial = f"{candidate_host}:{candidate_port}"
            rc, out, err = adb_run(["connect", serial], timeout=20)
            text = (out + "\n" + err).strip()
            notes.append(text)
            if "connected to" in text.lower():
                set_selected(serial)
                remember_device(serial, model=device_model(serial), selected=True)
                return True, "\n".join(note for note in notes if note)
        notes.append(
            "El emparejamiento funcionó, pero la conexión no. Comprueba que "
            "«Depuración ADB» siga activada y vuelve a pulsar «Conectar»."
        )
        return False, "\n".join(note for note in notes if note)

    def api_connect(self):
        data = self.read_json()
        host = str(data.get("host", "")).strip()
        try:
            port = int(data.get("port") or 5555)
        except (TypeError, ValueError) as exc:
            raise AdbError("El puerto debe ser un número (por defecto 5555).") from exc
        if not host:
            raise AdbError("Escribe la IP del Fire TV.")
        serial = f"{host}:{port}"
        rc, out, err = adb_run(["connect", serial], timeout=25)
        text = (out + "\n" + err).strip()
        low = text.lower()

        if "failed to authenticate" in low or "unauthorized" in low:
            # Un servidor ADB con claves viejas en memoria provoca este error aunque
            # la TV ya haya autorizado el PC. Reiniciarlo una vez es barato, pero
            # antes anotamos los dispositivos inalámbricos buenos para reconectarlos.
            previous = []
            try:
                previous = [d["serial"] for d in list_devices() if d["state"] == "device"]
            except AdbError:
                previous = []
            adb_run(["disconnect", serial], timeout=10)
            adb_run(["kill-server"], timeout=15)
            adb_run(["start-server"], timeout=30)
            for wireless in previous:
                if ":" in wireless:
                    adb_run(["connect", wireless], timeout=15)
            rc, out, err = adb_run(["connect", serial], timeout=25)
            text = (out + "\n" + err).strip()
            low = text.lower()
            if "failed to authenticate" in low or "unauthorized" in low:
                raise AuthError(self._auth_message(host, port))

        if "connected to" not in low and "already connected" not in low:
            if "cannot connect" in low or "failed to connect" in low or "refused" in low:
                raise AdbError(
                    f"No pude abrir {serial}. Revisa que el Fire TV esté encendido, en la "
                    "misma Wi-Fi y que «Depuración ADB» siga activa (se desactiva sola "
                    "tras algunos reinicios). También puede ser que la IP haya cambiado."
                )
            raise AdbError(
                text or "No se pudo conectar. Verifica que la depuración ADB esté activa."
            )
        set_selected(serial)
        remember_device(serial, model=device_model(serial), selected=True)
        self.send_json({"ok": True, "output": text, "selected": serial})

    def api_diagnose(self, query):
        host = str(query.get("host", [""])[0]).strip()
        try:
            port = int(query.get("port", ["5555"])[0] or 5555)
        except ValueError:
            port = 5555
        result: dict = {"ok": True, "host": host, "port": port, "checks": []}

        try:
            rc, out, err = adb_run(["version"], timeout=10)
            result["adb"] = ADB
            result["adb_version"] = (out or err).splitlines()[0] if (out or err) else ""
            result["checks"].append({"name": "adb instalado", "ok": rc == 0, "detail": result["adb_version"]})
        except AdbError as exc:
            result["adb"] = ADB
            result["adb_version"] = ""
            result["checks"].append({"name": "adb instalado", "ok": False, "detail": str(exc)})

        keydir = adb_key_dir()
        pub = keydir / "adbkey.pub"
        fingerprint = ""
        if pub.is_file():
            try:
                fingerprint = hashlib.sha256(pub.read_bytes()).hexdigest()[:16]
            except OSError:
                fingerprint = ""
        result["key_dir"] = str(keydir)
        result["key_fingerprint"] = fingerprint
        result["checks"].append(
            {
                "name": "clave ADB local",
                "ok": pub.is_file() and (keydir / "adbkey").is_file(),
                "detail": fingerprint or "sin clave (se generará al iniciar adb)",
            }
        )

        if host:
            reachable = tcp_probe(host, port)
            result["checks"].append(
                {
                    "name": f"TCP {host}:{port}",
                    "ok": reachable,
                    "detail": "puerto abierto" if reachable else "sin respuesta: IP incorrecta, TV apagada o depuración desactivada",
                }
            )
            for pair_port in [s["port"] for s in mdns_services() if s["host"] == host and s["pairing"]]:
                result["checks"].append(
                    {
                        "name": "puerto de emparejamiento anunciado",
                        "ok": True,
                        "detail": str(pair_port),
                    }
                )

        try:
            devices = list_devices()
        except AdbError as exc:
            devices = []
            result["checks"].append({"name": "adb devices", "ok": False, "detail": str(exc)})
        host_devices = [
            device for device in devices if not host or device["serial"].split(":")[0] == host
        ]
        result["devices"] = host_devices
        states = sorted({device["state"] for device in host_devices})
        result["checks"].append(
            {
                "name": "estado del dispositivo",
                "ok": "device" in states,
                "detail": ", ".join(states)
                or ("no aparece en adb devices" if host else "no hay dispositivos"),
            }
        )
        result["mdns"] = mdns_services()
        self.send_json(result)

    def api_pair(self):
        data = self.read_json()
        host = str(data.get("host", "")).strip()
        try:
            port = int(data.get("pair_port") or 5555)
        except (TypeError, ValueError) as exc:
            raise AdbError("El puerto de emparejamiento debe ser un número.") from exc
        code = str(data.get("code", "")).strip()
        if not host or not code:
            raise AdbError("La IP y el código de emparejamiento son obligatorios.")
        if not re.fullmatch(r"\d{6}", code):
            raise AdbError("El código de emparejamiento son exactamente 6 dígitos.")

        rc, out, err = adb_run(["pair", f"{host}:{port}", code], timeout=45)
        text = (out + "\n" + err).strip()
        low = text.lower()
        if "successfully paired" not in low:
            if "failed to connect" in low or "protocol fault" in low or "connection refused" in low:
                raise AdbError(
                    "No pude abrir el puerto de emparejamiento. En el Fire TV entra en "
                    "«Emparejar dispositivo» y copia el puerto que aparece ahí (es un "
                    "número alto, casi nunca 5555). Ese diálogo debe seguir abierto "
                    "mientras FireDeck se empareja."
                )
            if "wrong" in low or "denied" in low or "failed to authenticate" in low:
                raise AdbError(
                    "Código incorrecto o emparejamiento rechazado. Genera un código nuevo "
                    "en el Fire TV y escríbelo antes de que caduque."
                )
            raise AdbError(text or "El emparejamiento falló.")

        connected, note = self._auto_connect(host)
        self.send_json(
            {"ok": True, "output": text + ("\n" + note if note else ""), "connected": connected}
        )

    def api_disconnect(self):
        data = self.read_json()
        serial = data.get("serial") or current_selection()
        if serial:
            adb_run(["disconnect", serial], timeout=15)
        tab = current_tab_id()
        if tab:
            with TABS_LOCK:
                entry = TABS.get(tab)
                if entry and entry.get("serial") == serial:
                    entry["serial"] = ""
                    entry["explicit"] = False
        if STATE.get("serial") == serial:
            STATE["serial"] = None
        self.send_json({"ok": True})

    def api_repair(self):
        """Reinicia el servidor ADB y, opcionalmente, rota la clave RSA local."""
        data = self.read_json()
        host = str(data.get("host", "")).strip()
        try:
            port = int(data.get("port") or 5555)
        except (TypeError, ValueError) as exc:
            raise AdbError("El puerto debe ser un número (por defecto 5555).") from exc
        reset = bool(data.get("reset_key"))
        notes = []

        adb_run(["kill-server"], timeout=15)
        notes.append("Servidor ADB detenido.")

        if reset:
            keydir = adb_key_dir()
            keydir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S")
            moved = []
            for name in ("adbkey", "adbkey.pub"):
                path = keydir / name
                if path.is_file():
                    backup = path.with_name(f"{name}.old-{stamp}")
                    path.replace(backup)
                    moved.append(backup.name)
            notes.append(
                "Claves antiguas apartadas: " + ", ".join(moved) + "."
                if moved
                else "No había claves guardadas que apartar."
            )
            notes.append("adb generará una clave nueva y el Fire TV pedirá permiso otra vez.")

        adb_run(["start-server"], timeout=30)
        notes.append("Servidor ADB iniciado.")
        notes.append(f"Clave de este PC: {adb_key_info()['dir']}")

        serial = None
        connected = False
        if host:
            serial = f"{host}:{port}"
            rc, out, err = adb_run(["connect", serial], timeout=25)
            text = (out + "\n" + err).strip()
            notes.append(text)
            connected = "connected to" in text.lower()
            if connected:
                set_selected(serial)
                remember_device(serial, model=device_model(serial), selected=True)

        self.send_json(
            {
                "ok": True,
                "output": "\n".join(n for n in notes if n),
                "connected": connected,
                "serial": serial,
                "keys": adb_key_info(),
            }
        )

    def api_select(self):
        serial = str(self.read_json().get("serial", ""))
        devices = list_devices()
        if not any(d["serial"] == serial for d in devices):
            raise AdbError("Ese dispositivo ya no está disponible.")
        set_selected(serial)
        remember_device(serial, model=device_model(serial, devices), selected=True)
        self.send_json({"ok": True, "selected": serial})

    def api_name_device(self):
        data = self.read_json()
        serial = str(data.get("serial", "")).strip()
        if not valid_serial(serial):
            raise AdbError("El identificador del dispositivo no es válido.")
        if not any(item["serial"] == serial for item in load_saved_devices()):
            if not any(device["serial"] == serial for device in list_devices()):
                raise AdbError("Ese dispositivo no está guardado ni conectado.")
            remember_device(serial, model=device_model(serial))
        entry = rename_saved_device(serial, data.get("name", ""))
        self.send_json({"ok": True, "device": entry})

    def api_forget_device(self):
        serial = str(self.read_json().get("serial", "")).strip()
        if not valid_serial(serial):
            raise AdbError("El identificador del dispositivo no es válido.")
        removed = forget_saved_device(serial)
        self.send_json({"ok": True, "removed": removed})

    def api_shell(self):
        command = str(self.read_json().get("command", "")).strip()
        if not command:
            raise AdbError("Escribe un comando.")
        rc, out = device_shell_quiet(command, timeout=90)
        self.send_json({"ok": True, "output": out, "code": rc})

    def api_app_action(self):
        data = self.read_json()
        package = str(data.get("package", ""))
        action = str(data.get("action", ""))
        if action == "launch":
            output = launch_package(package)
        else:
            output = app_action(package, action, bool(data.get("confirm")))
        self.send_json({"ok": True, "output": output})

    def api_trim(self):
        size = str(self.read_json().get("size") or "512G")
        self.send_json({"ok": True, "output": trim_caches(size)})

    def api_install(self):
        filename = unquote(self.headers.get("X-Filename", "app.apk"))
        suffix = ".apk" if filename.lower().endswith(".apk") else ".bin"
        upload = self.read_body_to_file(suffix=suffix)
        try:
            self.send_json({"ok": True, "output": install_package(upload, filename)})
        finally:
            upload.unlink(missing_ok=True)

    def api_downloader_resolve(self):
        data = self.read_json()
        self.send_json({"ok": True, "info": resolve_download(data.get("input", ""))})

    def api_downloader_install(self):
        data = self.read_json()
        self.send_json({"ok": True, "job": start_download_job(data.get("input", ""))})

    def api_downloader_cancel(self):
        data = self.read_json()
        self.send_json(
            {"ok": True, "job": cancel_download_job(str(data.get("id", "")))}
        )

    def api_downloader_history_clear(self):
        clear_download_history()
        self.send_json({"ok": True, "history": []})

    def api_app_icon(self, query):
        package = query.get("package", [""])[0]
        icon = app_icon(package)
        if not icon:
            self.send_json(
                {"ok": False, "error": "Esta app no tiene un icono extraíble.", "icon": None},
                404,
            )
            return
        data, ext = icon
        mime = {
            ".png": "image/png",
            ".webp": "image/webp",
            ".jpg": "image/jpeg",
            ".svg": "image/svg+xml",
        }.get(ext, "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=604800")
        self.end_headers()
        self.wfile.write(data)

    def api_app_apk(self, query):
        package = normalize_package(query.get("package", [""])[0])
        if not valid_package(package):
            raise AdbError("Nombre de paquete inválido.")
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", package) or "app"
        tmpdir = Path(tempfile.mkdtemp(prefix="firedeck_apk_"))
        try:
            files = pull_package_apks(package, tmpdir)
            if len(files) == 1:
                payload = files[0]
                filename = f"{safe}.apk"
                mime = "application/vnd.android.package-archive"
            else:
                payload = tmpdir / f"{safe}-apks.zip"
                with zipfile.ZipFile(payload, "w", zipfile.ZIP_STORED) as zf:
                    for item in files:
                        zf.write(item, item.name)
                filename = f"{safe}-apks.zip"
                mime = "application/zip"
            self.send_download(payload, filename, mime)
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def api_input(self):
        output = device_input(self.read_json())
        self.send_json({"ok": True, "output": output})

    def api_file_download(self, query):
        local, filename = download_remote_file(query.get("path", [""])[0])
        mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        try:
            self.send_download(local, filename, mime)
        finally:
            local.unlink(missing_ok=True)

    def api_file_upload(self, query):
        directory = query.get("path", [FILE_ROOT])[0]
        overwrite = query.get("overwrite", ["0"])[0] in ("1", "true", "yes")
        filename = unquote(self.headers.get("X-Filename", ""))
        data = self.read_body()
        result = upload_remote_file(directory, filename, data, overwrite)
        self.send_json(
            {
                "ok": True,
                "output": f"«{result['name']}» subido a {directory}.",
                **result,
            }
        )

    def api_file_mkdir(self):
        path = make_remote_directory(self.read_json().get("path", ""))
        self.send_json(
            {"ok": True, "path": path, "output": f"Carpeta creada: «{path.rsplit('/', 1)[-1]}»."}
        )

    def api_file_rename(self):
        data = self.read_json()
        path = rename_remote_path(data.get("path", ""), data.get("name", ""))
        self.send_json(
            {"ok": True, "path": path, "output": f"Ahora se llama «{path.rsplit('/', 1)[-1]}»."}
        )

    def api_file_delete(self):
        data = self.read_json()
        path = delete_remote_path(data.get("path", ""), bool(data.get("confirm")))
        self.send_json(
            {"ok": True, "path": path, "output": f"Borrado: «{path.rsplit('/', 1)[-1]}»."}
        )

    def api_record_start(self):
        data = self.read_json()
        status = start_recording(data.get("duration", 30), data.get("bitrate", 8_000_000))
        self.send_json({"ok": True, **status})

    def api_record_stop(self):
        self.read_json()
        self.send_json({"ok": True, **stop_recording()})

    def api_record_download(self):
        path, filename = recording_file()
        try:
            self.send_download(path, filename, "video/mp4")
        finally:
            path.unlink(missing_ok=True)

    def api_reboot(self):
        if not self.read_json().get("confirm"):
            raise AdbError("Reiniciar el Fire TV requiere confirmación.")
        serial = pick_serial()
        adb_run(["-s", serial, "reboot"], timeout=15)
        self.send_json({"ok": True, "output": "Reiniciando..."})


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, BrokenPipeError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


def main():
    global ADB
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
    parser = argparse.ArgumentParser(description="FireDeck: control web para Fire TV por ADB")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Interfaz de escucha (127.0.0.1 por seguridad)")
    parser.add_argument("--port", type=int, default=8080,
                        help="Puerto HTTP (por defecto 8080)")
    parser.add_argument("--download-adb", action="store_true",
                        help="Descargar platform-tools si falta adb")
    args = parser.parse_args()

    if not shutil.which(ADB) and args.download_adb:
        try:
            ADB = download_platform_tools()
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            # OSError cubre red (URLError), disco y permisos.
            print(f"[FireDeck] No se pudo descargar adb: {exc}")
    if not shutil.which(ADB):
        print("[FireDeck] Aviso: adb no está en PATH. La app lo indicará al conectar.")

    server = QuietServer((args.host, args.port), Handler)
    shown = "localhost" if args.host in ("127.0.0.1", "0.0.0.0") else args.host
    print()
    print("  FireDeck  🔥  control de Fire TV por ADB")
    print(f"  Abre:  http://{shown}:{args.port}")
    print(f"  adb:   {ADB}")
    print("  Ctrl+C para detener")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[FireDeck] Hasta luego.")
    finally:
        shutdown_media()
        server.server_close()


if __name__ == "__main__":
    main()
