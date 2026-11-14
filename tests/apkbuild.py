"""Constructores de datos sintéticos para probar los parseos de FireDeck.

Generan, sin herramientas externas, los formatos binarios que lee app.py:
XML binario de Android (AXML), resources.arsc, APK con firma v1 (PKCS#7) o v2
(APK Signing Block) y paquetes ZIP (.apks / .xapk).
"""
from __future__ import annotations

import struct
import zipfile
from pathlib import Path

SIG_MAGIC = b"APK Sig Block 42"
V2_ID = 0x7109871A

TYPE_REFERENCE = 0x01
TYPE_STRING = 0x03
TYPE_INT_DEC = 0x10


# ---------------------------------------------------------------- string pool
def _len8(value: int) -> bytes:
    return bytes([value]) if value < 0x80 else bytes([0x80 | (value >> 8), value & 0xFF])


def string_pool(strings: list[str], utf8: bool = False) -> bytes:
    blobs = []
    for text in strings:
        if utf8:
            raw = text.encode("utf-8")
            blobs.append(_len8(len(text)) + _len8(len(raw)) + raw + b"\x00")
        else:
            units = len(text.encode("utf-16-le")) // 2
            if units < 0x8000:
                head = struct.pack("<H", units)
            else:
                head = struct.pack("<HH", 0x8000 | (units >> 16), units & 0xFFFF)
            blobs.append(head + text.encode("utf-16-le") + b"\x00\x00")
    offsets, body = [], b""
    for blob in blobs:
        offsets.append(len(body))
        body += blob
    body += b"\x00" * (-len(body) % 4)
    header_size = 28
    strings_start = header_size + 4 * len(strings)
    size = strings_start + len(body)
    return (
        struct.pack(
            "<HHIIIIII", 0x0001, header_size, size, len(strings), 0,
            0x100 if utf8 else 0, strings_start, 0,
        )
        + b"".join(struct.pack("<I", o) for o in offsets)
        + body
    )


# ----------------------------------------------------------------------- AXML
def axml(events: list[tuple], utf8: bool = False) -> bytes:
    """events: ("start", nombre, [(attr, "str"|"int"|"ref", valor), ...]) o ("end", nombre)."""
    strings: list[str] = []

    def idx(text: str) -> int:
        if text not in strings:
            strings.append(text)
        return strings.index(text)

    for event in events:
        idx(event[1])
        if event[0] == "start":
            for name, kind, value in event[2]:
                idx(name)
                if kind == "str":
                    idx(value)

    chunks = b""
    for event in events:
        if event[0] == "start":
            attrs = b""
            for name, kind, value in event[2]:
                if kind == "str":
                    raw, dtype, data = idx(value), TYPE_STRING, idx(value)
                elif kind == "ref":
                    raw, dtype, data = -1, TYPE_REFERENCE, value
                else:
                    raw, dtype, data = -1, TYPE_INT_DEC, value
                attrs += struct.pack("<iiiHBBI", -1, idx(name), raw, 8, 0, dtype, data)
            size = 16 + 20 + len(attrs)
            chunks += struct.pack("<HHIii", 0x0102, 16, size, 1, -1)
            chunks += struct.pack("<iiHHHHHH", -1, idx(event[1]), 20, 20, len(event[2]), 0, 0, 0)
            chunks += attrs
        else:
            chunks += struct.pack("<HHIii", 0x0103, 16, 24, 1, -1)
            chunks += struct.pack("<ii", -1, idx(event[1]))
    pool = string_pool(strings, utf8)
    total = 8 + len(pool) + len(chunks)
    return struct.pack("<HHI", 0x0003, 8, total) + pool + chunks


def manifest_xml(
    package: str,
    split: str = "",
    version_code: int | None = 1,
    version_name: str = "1.0",
    min_sdk: int | None = None,
    icon_ref: int | None = None,
) -> bytes:
    """Manifiesto realista: minSdkVersion va en <uses-sdk>, hijo de <manifest>."""
    attrs = [("package", "str", package)]
    if split:
        attrs.append(("split", "str", split))
    if version_code is not None:
        attrs.append(("versionCode", "int", version_code))
    if version_name:
        attrs.append(("versionName", "str", version_name))
    events: list[tuple] = [("start", "manifest", attrs)]
    if min_sdk is not None:
        events += [("start", "uses-sdk", [("minSdkVersion", "int", min_sdk)]), ("end", "uses-sdk")]
    app_attrs = [("icon", "ref", icon_ref)] if icon_ref is not None else []
    events += [("start", "application", app_attrs), ("end", "application"), ("end", "manifest")]
    return axml(events)


# ------------------------------------------------------------- resources.arsc
def arsc(global_strings: list[str], package_id: int, types: list[dict]) -> bytes:
    """types: {"type_id", "density", "entries": {indice: (dtype, dato)}, "sparse": bool}."""
    pool = string_pool(global_strings)
    type_chunks = b""
    for spec in types:
        entries = spec["entries"]
        sparse = spec.get("sparse", False)
        order = sorted(entries)
        entry_blob = b""
        entry_offsets = {}
        for index in order:
            dtype, data = entries[index]
            entry_offsets[index] = len(entry_blob)
            entry_blob += struct.pack("<HHI", 8, 0, 0) + struct.pack("<HBBI", 8, 0, dtype, data)
        if sparse:
            count = len(order)
            offsets = b"".join(struct.pack("<HH", i, entry_offsets[i] // 4) for i in order)
        else:
            count = max(order) + 1
            offsets = b"".join(
                struct.pack("<I", entry_offsets[i]) if i in entry_offsets
                else struct.pack("<I", 0xFFFFFFFF)
                for i in range(count)
            )
        header_size = 52
        config = bytearray(32)
        struct.pack_into("<I", config, 0, 32)
        struct.pack_into("<H", config, 14, spec.get("density", 0))
        size = header_size + len(offsets) + len(entry_blob)
        type_chunks += struct.pack(
            "<HHIBBHII", 0x0201, header_size, size, spec["type_id"],
            0x01 if sparse else 0, 0, count, header_size + len(offsets),
        ) + bytes(config) + offsets + entry_blob
    package_header = struct.pack("<HHII", 0x0200, 288, 288 + len(type_chunks), package_id)
    package_header += b"\x00" * (288 - len(package_header))
    package = package_header + type_chunks
    total = 12 + len(pool) + len(package)
    return struct.pack("<HHII", 0x0002, 12, total, 1) + pool + package


# ----------------------------------------------------------------- firma / DER
def lp(data: bytes) -> bytes:
    return struct.pack("<I", len(data)) + data


def v2_value(certificate: bytes) -> bytes:
    certificates = lp(certificate)
    signed_data = lp(lp(b"digest")) + lp(certificates) + lp(b"")
    signer = lp(signed_data) + lp(b"signatures") + lp(b"pubkey")
    return lp(lp(signer))


def der(tag: int, content: bytes) -> bytes:
    n = len(content)
    if n < 0x80:
        length = bytes([n])
    else:
        raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
        length = bytes([0x80 | len(raw)]) + raw
    return bytes([tag]) + length + content


def pkcs7_with_cert(certificate: bytes) -> bytes:
    signed = der(
        0x30,
        der(0x02, b"\x01") + der(0x31, b"") + der(0x30, b"") + der(0xA0, certificate),
    )
    return der(0x30, der(0x06, b"\x2a\x86") + der(0xA0, signed))


def fake_certificate(label: str) -> bytes:
    return der(0x30, ("cert-" + label).encode())


def add_signing_block(zip_bytes: bytes, pairs: dict[int, bytes]) -> bytes:
    eocd = zip_bytes.rfind(b"PK\x05\x06")
    cd_offset = struct.unpack_from("<I", zip_bytes, eocd + 16)[0]
    body = b"".join(struct.pack("<QI", 4 + len(v), i) + v for i, v in pairs.items())
    size = len(body) + 24
    block = struct.pack("<Q", size) + body + struct.pack("<Q", size) + SIG_MAGIC
    patched = bytearray(zip_bytes[:cd_offset] + block + zip_bytes[cd_offset:])
    new_eocd = eocd + len(block)
    struct.pack_into("<I", patched, new_eocd + 16, cd_offset + len(block))
    return bytes(patched)


# ------------------------------------------------------------------------ PNG
def png_image(size: int = 96) -> bytes:
    """PNG RGBA 8 bits con colores variados (pasa los filtros de 'icono válido')."""
    import zlib

    rows = b""
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            row += bytes([(x * 255) // size, (y * 255) // size, (x * y) % 256, 255])
        rows += bytes(row)

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


# ------------------------------------------------------------------------ ZIP
def zip_bytes(files: dict[str, bytes]) -> bytes:
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buffer.getvalue()


def make_apk(
    path: Path,
    package: str,
    split: str = "",
    min_sdk: int | None = None,
    libs: tuple[str, ...] = (),
    cert: str | None = "A",
    scheme: str = "v2",
    version_code: int | None = 7,
    version_name: str = "1.2.3",
) -> Path:
    files = {"AndroidManifest.xml": manifest_xml(
        package, split=split, version_code=version_code,
        version_name=version_name, min_sdk=min_sdk)}
    for abi in libs:
        files[f"lib/{abi}/libnative.so"] = b"\x7fELF"
    certificate = fake_certificate(cert) if cert else b""
    if cert and scheme == "v1":
        files["META-INF/CERT.RSA"] = pkcs7_with_cert(certificate)
    data = zip_bytes(files)
    if cert and scheme == "v2":
        data = add_signing_block(data, {V2_ID: v2_value(certificate)})
    path.write_bytes(data)
    return path
