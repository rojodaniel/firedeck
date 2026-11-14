/* FireDeck · frontend sin dependencias */
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  status: null,
  apps: [],
  liveTimer: null,
  mirror: { active: false, controller: null, serial: "", handle: null },
  file: null,
  installing: false,
  download: { info: null, jobId: "", timer: null, busy: false },
  deviceInfo: null,
  rec: { recording: false, done: false, error: "", has_file: false },
  recTimer: null,
  files: { path: "/sdcard", parent: null, entries: [], loading: false },
  system: { apps: [], counts: null, serial: "" },
  network: null,
  networkLoading: false,
};

/* --------------------------- pestaña del navegador ------------------------ */

const TAB_STORAGE_KEY = "firedeck.tab";
const TAB_HEADER = "X-FireDeck-Tab";
const TAB_CLAIM_PREFIX = "firedeck.tab.claim.";
const TAB_CLAIM_TTL = 70_000;

function newTabId() {
  if (window.crypto?.randomUUID) return `tab-${window.crypto.randomUUID()}`;
  const random = Math.random().toString(36).slice(2, 10);
  return `tab-${Date.now().toString(36)}-${random}`;
}

function claimTab(id) {
  try {
    localStorage.setItem(`${TAB_CLAIM_PREFIX}${id}`, JSON.stringify({ ts: Date.now() }));
  } catch {
    /* localStorage bloqueado o lleno: la pestaña funciona igual */
  }
}

function releaseTab(id) {
  try {
    localStorage.removeItem(`${TAB_CLAIM_PREFIX}${id}`);
  } catch {
    /* nada que hacer */
  }
}

function tabClaimLive(id) {
  try {
    const raw = localStorage.getItem(`${TAB_CLAIM_PREFIX}${id}`);
    if (!raw) return false;
    const seen = Number(JSON.parse(raw).ts) || 0;
    return Date.now() - seen < TAB_CLAIM_TTL;
  } catch {
    return false;
  }
}

let tabId = sessionStorage.getItem(TAB_STORAGE_KEY) || "";
// Duplicar una pestaña (Ctrl + arrastrar, menú del navegador) copia
// sessionStorage, así que ambas compartirían identificador y dispositivo. Si
// otra pestaña está viva con el mismo id, esta se queda con uno propio.
if (tabId && tabClaimLive(tabId)) {
  tabId = "";
}
if (!tabId) {
  tabId = newTabId();
  sessionStorage.setItem(TAB_STORAGE_KEY, tabId);
}
claimTab(tabId);
setInterval(() => claimTab(tabId), 5000);
window.addEventListener("pagehide", () => releaseTab(tabId));
window.addEventListener("pageshow", () => claimTab(tabId));

function tabParam() {
  return `tab=${encodeURIComponent(tabId)}`;
}

function tabHeaders() {
  return { [TAB_HEADER]: tabId };
}

/* ------------------------------- utilidades ------------------------------ */

async function api(path, options = {}) {
  const { method = "GET", body, headers = {} } = options;
  const init = { method, headers: { [TAB_HEADER]: tabId, ...headers } };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch {
    throw new Error("No se pudo contactar con el servidor de FireDeck.");
  }
  let data;
  try {
    data = await res.json();
  } catch {
    throw new Error(`Respuesta inválida del servidor (${res.status}).`);
  }
  if (!res.ok || data.ok === false) {
    const error = new Error(data.error || `Error ${res.status}`);
    if (data.hint) error.hint = data.hint;
    throw error;
  }
  return data;
}

function toast(message, type = "") {
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  el.textContent = message;
  $("#toasts").appendChild(el);
  setTimeout(() => {
    el.classList.add("out");
    setTimeout(() => el.remove(), 280);
  }, 4200);
}

function friendlyError(err) {
  return err && err.message ? err.message : String(err);
}

// Bloque «vacío / error» construido con textContent: el texto (que puede traer
// nombres de archivos o salida de adb del Fire TV) nunca se interpreta como HTML.
function showEmptyState(container, icon, text) {
  const wrap = document.createElement("div");
  wrap.className = "empty-state";
  const symbol = document.createElement("span");
  symbol.textContent = icon;
  const message = document.createElement("p");
  message.textContent = text;
  wrap.append(symbol, message);
  container.replaceChildren(wrap);
}

const fmtBytes = (bytes) => {
  if (!Number.isFinite(bytes)) return "—";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (bytes >= 1024 && i < units.length - 1) {
    bytes /= 1024;
    i += 1;
  }
  return `${bytes.toFixed(bytes >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
};

/* -------------------------------- estado -------------------------------- */

function resetDeviceCache() {
  state.apps = [];
  appInfoCache.clear();
  state.files = { path: "/sdcard", parent: null, entries: [], loading: false };
  state.deviceInfo = null;
  state.network = null;
  state.system = { apps: [], counts: null, serial: "" };
  stopRecordingPoll();
  state.rec = { recording: false, done: false, error: "", has_file: false };
  renderRecording({ recording: false, done: false, error: "", has_file: false });
}

function reloadActiveView() {
  const active = document.querySelector(".view.active")?.id || "";
  if (active === "view-apps" && state.status?.device) loadApps();
  if (active === "view-system" && state.status?.device) loadSystemApps();
  if (active === "view-files" && state.status?.device) loadFiles("/sdcard");
  if (active === "view-device" && state.status?.device) loadDeviceInfo();
  if (active === "view-network") loadNetwork();
}

function renderTabs(tabs) {
  const box = $("#tabInfo");
  if (!box) return;
  tabs = tabs || [];
  const me = tabs.find((item) => item.self);
  const others = tabs.filter((item) => !item.self);
  box.textContent = "";

  const line = document.createElement("p");
  line.className = "tab-line";
  line.append(document.createTextNode("Esta pestaña controla "));
  const strong = document.createElement("strong");
  strong.textContent = (me && me.name) || "ningún Fire TV";
  line.append(strong, document.createTextNode("."));
  box.appendChild(line);

  if (others.length) {
    const extra = document.createElement("p");
    extra.className = "tab-line tab-line-muted";
    extra.textContent = `Otras pestañas: ${others
      .map((item) => `${item.label} → ${item.name || "sin dispositivo"}`)
      .join(" · ")}`;
    box.appendChild(extra);
  } else {
    const tip = document.createElement("p");
    tip.className = "tab-line tab-line-muted";
    tip.textContent =
      "Abre FireDeck en otra pestaña para controlar un segundo Fire TV a la vez.";
    box.appendChild(tip);
  }
}

function renderStatus(data) {
  const previous = state.status;
  const previousSerial = previous?.selected || previous?.device?.serial || "";
  const serial = data.selected || data.device?.serial || "";
  state.status = data;
  if (previousSerial && serial && previousSerial !== serial) {
    resetDeviceCache();
    // La lista, la ficha y la red son de otro Fire TV: se recargan al vuelo.
    setTimeout(reloadActiveView, 0);
  }
  const dev = data.device;
  if (state.mirror.active) {
    const serial = data.selected || dev?.serial || "";
    if (!dev || (state.mirror.serial && serial !== state.mirror.serial)) {
      stopMirror();
    }
  }
  const dot = $("#statusDot");
  const name = $("#deviceName");
  const meta = $("#deviceMeta");
  dot.className = "dot";
  $("#connectBtn").textContent = dev ? "Gestionar" : "Conectar";

  if (dev) {
    dot.classList.add("online");
    name.textContent = dev.alias || dev.model || "Fire TV";
    const os = dev.fireos || (dev.android ? `Android ${dev.android}` : "");
    const label = dev.alias && dev.model ? dev.model : "";
    meta.textContent = [label, dev.ip || dev.serial, os].filter(Boolean).join(" · ");
    const abiNode = $("#deviceAbi");
    if (abiNode) {
      const abis = dev.abis || [];
      abiNode.textContent = abis.length ? abis.join(", ") : "desconocida";
    }
    if (state.deviceInfo && state.deviceInfo.serial !== dev.serial) {
      state.deviceInfo = null;
    }
    if (state.system.serial && state.system.serial !== dev.serial) {
      state.system = { apps: [], counts: null, serial: "" };
      if ($("#view-system").classList.contains("active")) loadSystemApps();
    }
  } else {
    state.deviceInfo = null;
    if (state.system.apps.length) {
      state.system = { apps: [], counts: null, serial: "" };
    }
    const unauthorized = (data.devices || []).some((d) => d.state === "unauthorized");
    if (data.selected) {
      dot.classList.add("warn");
      name.textContent = data.selected_name || data.selected;
      meta.textContent = "No disponible en esta pestaña · Gestionar";
    } else if (unauthorized) {
      dot.classList.add("warn");
      name.textContent = "No autorizado";
      meta.textContent = "Acepta el aviso en la TV";
    } else {
      name.textContent = "Sin conectar";
      meta.textContent = "Pulsa para configurar";
    }
  }
  renderTabs(data.tabs);
  if (connectModal.open) renderSavedDevices();
}

async function refreshStatus(quiet = true) {
  try {
    const data = await api("/api/status");
    renderStatus(data);
    if (!quiet && data.device) toast(`Conectado a ${data.device.model}`, "ok");
    return data;
  } catch (err) {
    if (!quiet) toast(friendlyError(err), "err");
    return null;
  }
}

/* ------------------------------- conexión -------------------------------- */

const connectModal = $("#connectModal");
const modalError = $("#modalError");

function showModalError(message) {
  modalError.textContent = message;
  modalError.classList.toggle("hidden", !message);
}

const authHelp = $("#authHelp");

function showAuthHelp() {
  authHelp.classList.remove("hidden");
}

function hideAuthHelp() {
  authHelp.classList.add("hidden");
  $("#authOutput").classList.add("hidden");
  $("#authOutput").textContent = "";
}

async function repairAdb({ resetKey = false } = {}) {
  const host = $("#hostInput").value.trim();
  const port = Number($("#portInput").value) || 5555;
  const out = $("#authOutput");
  const buttons = [$("#repairAdb"), $("#resetKey")];
  buttons.forEach((b) => (b.disabled = true));
  out.classList.remove("hidden");
  out.textContent = resetKey
    ? "Reiniciando ADB y apartando la clave actual…"
    : "Reiniciando el servidor ADB…";
  try {
    const data = await api("/api/adb/repair", {
      method: "POST",
      body: { host, port, reset_key: resetKey },
    });
    out.textContent = data.output || "Listo.";
    if (data.connected) {
      await refreshStatus();
      connectModal.close();
      toast("Fire TV conectado", "ok");
    } else {
      out.textContent +=
        "\n\nAhora pulsa «Conectar» y acepta el aviso que aparezca en el televisor.";
    }
  } catch (err) {
    out.textContent = friendlyError(err);
  } finally {
    buttons.forEach((b) => (b.disabled = false));
  }
}

$("#repairAdb").addEventListener("click", () => repairAdb());
$("#resetKey").addEventListener("click", () => {
  const sure = window.confirm(
    "Se apartará la clave ADB de este PC y el Fire TV pedirá permiso otra vez.\n" +
      "Otros dispositivos Android tendrán que volver a autorizar este PC.\n\n¿Continuar?"
  );
  if (sure) repairAdb({ resetKey: true });
});

$("#diagBtn").addEventListener("click", async () => {
  const host = $("#hostInput").value.trim();
  const port = Number($("#portInput").value) || 5555;
  const out = $("#diagOutput");
  const btn = $("#diagBtn");
  btn.disabled = true;
  out.classList.remove("hidden");
  out.textContent = "Comprobando conexión, claves y mDNS…";
  try {
    const data = await api(
      `/api/diagnose?host=${encodeURIComponent(host)}&port=${port}`
    );
    const lines = [];
    lines.push(`adb:     ${data.adb_version || "no disponible"}`);
    lines.push(`archivo: ${data.adb || "—"}`);
    lines.push(
      `clave:   ${data.key_fingerprint || "sin generar"}  ${data.key_dir ? `(${data.key_dir})` : ""}`
    );
    lines.push("");
    (data.checks || []).forEach((check) => {
      lines.push(`${check.ok ? "✔" : "✘"} ${check.name}: ${check.detail}`);
    });
    if (data.mdns && data.mdns.length) {
      lines.push("", "Servicios mDNS del Fire TV:");
      data.mdns.forEach((svc) =>
        lines.push(`  · ${svc.type} → ${svc.host}:${svc.port}`)
      );
      const pairing = data.mdns.find((svc) => svc.pairing && svc.host === host);
      if (pairing && pairPortIsBlank()) {
        $("#pairPort").value = String(pairing.port);
        lines.push("", `Puerto de emparejamiento puesto en el campo: ${pairing.port}`);
      }
    }
    if (data.devices && data.devices.length) {
      lines.push("", "adb devices:");
      data.devices.forEach((dev) =>
        lines.push(`  · ${dev.serial}  ${dev.state}  ${dev.model || ""}`)
      );
    }
    out.textContent = lines.join("\n");
  } catch (err) {
    out.textContent = friendlyError(err);
  } finally {
    btn.disabled = false;
  }
});

function pairPortIsBlank() {
  const field = $("#pairPort");
  return !field.value || field.value === "5555";
}

function savedLabel(device) {
  return device.alias || device.display_name || device.model || device.serial;
}

function savedStatusText(device) {
  if (device.online) return "Conectado";
  if (device.state === "unauthorized") return "Pide autorización en la TV";
  if (device.state && device.state !== "offline") return device.state;
  return "Sin conexión";
}

async function prepareDeviceSwitch(targetSerial) {
  const current = state.status?.selected;
  if (!current || current === targetSerial) return;
  // La grabación y el espejo pertenecen al dispositivo anterior: se cierran
  // antes de mover esta pestaña para no dejarlos huérfanos.
  if (state.mirror.active) stopMirror();
  if (state.rec.recording) await stopScreenRecording();
}

async function useSavedDevice(device) {
  showModalError("");
  hideAuthHelp();
  const label = savedLabel(device);
  if (device.online) {
    try {
      await prepareDeviceSwitch(device.serial);
      await api("/api/select", { method: "POST", body: { serial: device.serial } });
      await refreshStatus();
      connectModal.close();
      toast(`Usando ${label}`, "ok");
    } catch (err) {
      showModalError(friendlyError(err));
    }
    return;
  }
  if (!device.serial.includes(":")) {
    showModalError(
      "Este dispositivo se conectó por USB. Vuelve a enchufarlo y elígelo en la lista de detectados."
    );
    return;
  }
  try {
    await prepareDeviceSwitch(device.serial);
    await api("/api/connect", {
      method: "POST",
      body: { host: device.host, port: device.port || 5555 },
    });
    localStorage.setItem("firedeck.host", device.host);
    localStorage.setItem("firedeck.port", String(device.port || 5555));
    await refreshStatus();
    connectModal.close();
    toast(`${label} conectado`, "ok");
  } catch (err) {
    showModalError(friendlyError(err));
    if (err.hint === "auth") showAuthHelp();
  }
}

async function renameSavedDevice(device) {
  const current = device.alias || "";
  const value = window.prompt(
    `Nombre para ${savedLabel(device)} (vacío para quitar el nombre personalizado):`,
    current || savedLabel(device)
  );
  if (value === null) return;
  try {
    await api("/api/devices/name", {
      method: "POST",
      body: { serial: device.serial, name: value.trim() },
    });
    await refreshStatus();
    toast(
      value.trim() ? `Guardado como «${value.trim()}»` : "Nombre personalizado eliminado",
      "ok"
    );
  } catch (err) {
    showModalError(friendlyError(err));
  }
}

async function forgetSavedDevice(device) {
  const label = savedLabel(device);
  const sure = window.confirm(
    `¿Olvidar «${label}» (${device.serial})?\n\nDejará de aparecer en la lista guardada. La conexión actual no se corta.`
  );
  if (!sure) return;
  try {
    await api("/api/devices/forget", {
      method: "POST",
      body: { serial: device.serial },
    });
    await refreshStatus();
    toast("Dispositivo olvidado", "ok");
  } catch (err) {
    showModalError(friendlyError(err));
  }
}

function renderSavedDevices() {
  const section = $("#savedDevices");
  const list = $("#savedList");
  const devices = state.status?.saved || [];
  const otherTabs = (state.status?.tabs || []).filter((tab) => !tab.self);
  list.textContent = "";
  section.classList.toggle("hidden", devices.length === 0);
  if (!devices.length) return;

  const current = state.status?.selected || state.status?.device?.serial;
  devices.forEach((device) => {
    const row = document.createElement("div");
    row.className = "saved-row";

    const pick = document.createElement("button");
    pick.type = "button";
    pick.className = "saved-pick";
    if (device.serial === current) pick.classList.add("current");
    pick.title = device.online
      ? `Usar ${savedLabel(device)} en esta pestaña`
      : `Conectar con ${device.serial}`;

    const name = document.createElement("span");
    name.className = "saved-name";
    name.textContent = savedLabel(device);
    const meta = document.createElement("small");
    meta.className = "saved-meta";
    const inTabs = otherTabs.filter((tab) => tab.serial === device.serial);
    const extra = inTabs.length
      ? ` · en ${inTabs.map((tab) => tab.label).join(" y ")}`
      : "";
    meta.textContent = `${device.serial} · ${savedStatusText(device)}${extra}`;
    pick.append(name, meta);
    pick.addEventListener("click", () => useSavedDevice(device));

    const rename = document.createElement("button");
    rename.type = "button";
    rename.className = "saved-action";
    rename.textContent = "✎";
    rename.title = `Poner nombre a ${savedLabel(device)}`;
    rename.addEventListener("click", () => renameSavedDevice(device));

    const forget = document.createElement("button");
    forget.type = "button";
    forget.className = "saved-action danger";
    forget.textContent = "✕";
    forget.title = `Olvidar ${savedLabel(device)}`;
    forget.addEventListener("click", () => forgetSavedDevice(device));

    row.append(pick, rename, forget);
    list.appendChild(row);
  });
}

function renderDeviceList() {
  const box = $("#deviceList");
  const devices = (state.status?.devices || []).filter((dev) => dev.state === "device");
  box.innerHTML = "";
  if (devices.length < 2) {
    box.classList.add("hidden");
    return;
  }
  const title = document.createElement("h3");
  title.textContent = "Fire TV detectados: elige cuál controlar";
  box.appendChild(title);
  const current = state.status?.selected;
  devices.forEach((dev) => {
    const btn = document.createElement("button");
    btn.type = "button";
    if (dev.serial === current) btn.classList.add("current");
    const name = document.createElement("span");
    name.textContent = dev.alias || dev.model || dev.serial;
    const serial = document.createElement("small");
    serial.textContent = dev.serial;
    btn.append(name, serial);
    btn.addEventListener("click", async () => {
      try {
        await prepareDeviceSwitch(dev.serial);
        await api("/api/select", { method: "POST", body: { serial: dev.serial } });
        await refreshStatus();
        connectModal.close();
        toast(`Usando ${dev.alias || dev.model || dev.serial}`, "ok");
      } catch (err) {
        showModalError(friendlyError(err));
      }
    });
    box.appendChild(btn);
  });
  box.classList.remove("hidden");
}

function openConnect() {
  showModalError("");
  hideAuthHelp();
  $("#diagOutput").classList.add("hidden");
  $("#diagOutput").textContent = "";
  $("#deviceList").classList.add("hidden");
  $("#deviceList").innerHTML = "";
  $("#savedList").textContent = "";
  $("#savedDevices").classList.add("hidden");
  const savedHost = localStorage.getItem("firedeck.host") || "";
  const savedPort = localStorage.getItem("firedeck.port") || "5555";
  $("#hostInput").value = savedHost;
  $("#portInput").value = savedPort;
  $("#pairPort").value = localStorage.getItem("firedeck.pairPort") || "5555";
  $("#pairToggle").checked = false;
  $("#pairFields").classList.add("hidden");
  $("#discoverResults").classList.add("hidden");
  $("#disconnectBtn").classList.toggle("hidden", !state.status?.device);
  renderDeviceList();
  renderSavedDevices();
  renderTabs(state.status?.tabs);
  refreshStatus()
    .then(() => {
      renderDeviceList();
      renderSavedDevices();
      renderTabs(state.status?.tabs);
    })
    .catch(() => {});
  connectModal.showModal();
  setTimeout(() => $("#hostInput").focus(), 50);
}

$("#connectBtn").addEventListener("click", openConnect);
$("#deviceChip").addEventListener("click", openConnect);

$("#pairToggle").addEventListener("change", (e) => {
  $("#pairFields").classList.toggle("hidden", !e.target.checked);
});

$("#cancelConnect").addEventListener("click", () => connectModal.close());
$("#closeConnect").addEventListener("click", () => connectModal.close());

$("#disconnectBtn").addEventListener("click", async () => {
  const serial = state.status?.selected || state.status?.device?.serial;
  try {
    await api("/api/disconnect", { method: "POST", body: { serial } });
    connectModal.close();
    $("#screenBox").classList.remove("has-image");
    await refreshStatus();
    toast("Fire TV desconectado", "ok");
  } catch (err) {
    showModalError(friendlyError(err));
  }
});

$("#discoverBtn").addEventListener("click", async () => {
  const btn = $("#discoverBtn");
  const box = $("#discoverResults");
  btn.disabled = true;
  btn.textContent = "Buscando…";
  box.classList.remove("hidden");
  box.innerHTML = '<span class="muted">Escaneando la red local y mDNS…</span>';
  try {
    const data = await api("/api/discover?port=5555");
    const hosts = data.hosts || [];
    const services = data.mdns || [];
    box.innerHTML = "";

    if (!hosts.length && !services.length) {
      box.innerHTML =
        '<span class="muted">No encontré dispositivos. Revisa que la TV esté encendida y en la misma Wi-Fi, o escribe su IP.</span>';
    } else {
      const addChip = (label, onClick) => {
        const chip = document.createElement("button");
        chip.type = "button";
        chip.className = "chip";
        chip.textContent = label;
        chip.addEventListener("click", onClick);
        box.appendChild(chip);
      };

      hosts.forEach((host) => {
        addChip(host, () => {
          $("#hostInput").value = host;
          $("#portInput").value = "5555";
        });
      });

      services.forEach((svc) => {
        const label = `${svc.pairing ? "Emparejar" : "Conectar"} · ${svc.host}:${svc.port}`;
        addChip(label, () => {
          $("#hostInput").value = svc.host;
          if (svc.pairing) {
            $("#pairToggle").checked = true;
            $("#pairFields").classList.remove("hidden");
            $("#pairPort").value = String(svc.port);
          } else {
            $("#portInput").value = String(svc.port);
          }
        });
      });

      if (hosts.length === 1 && !$("#hostInput").value) {
        $("#hostInput").value = hosts[0];
        toast(`Encontrado ${hosts[0]}`, "ok");
      }
      const pairing = services.find((svc) => svc.pairing && svc.host === $("#hostInput").value);
      if (pairing) $("#pairPort").value = String(pairing.port);
    }
  } catch (err) {
    const note = document.createElement("span");
    note.className = "muted";
    note.textContent = friendlyError(err);
    box.replaceChildren(note);
  } finally {
    btn.disabled = false;
    btn.textContent = "Detectar";
  }
});

$("#connectForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  showModalError("");
  const host = $("#hostInput").value.trim();
  const port = Number($("#portInput").value) || 5555;
  const wantPair = $("#pairToggle").checked;
  const pairPort = Number($("#pairPort").value) || 5555;
  const code = $("#pairCode").value.trim();

  if (!host) {
    showModalError("Escribe la IP del Fire TV.");
    return;
  }
  if (wantPair && !code) {
    showModalError("Escribe el código de 6 dígitos que muestra el Fire TV.");
    return;
  }

  const btn = $("#doConnect");
  btn.disabled = true;
  btn.textContent = wantPair ? "Emparejando…" : "Conectando…";
  try {
    await prepareDeviceSwitch(`${host}:${port}`);
    if (wantPair) {
      const paired = await api("/api/pair", {
        method: "POST",
        body: { host, pair_port: pairPort, code },
      });
      localStorage.setItem("firedeck.pairPort", String(pairPort));
      if (paired.connected) {
        localStorage.setItem("firedeck.host", host);
        localStorage.setItem("firedeck.port", String(port));
        connectModal.close();
        hideAuthHelp();
        await refreshStatus();
        toast("Fire TV conectado", "ok");
        return;
      }
      btn.textContent = "Conectando…";
    }
    await api("/api/connect", { method: "POST", body: { host, port } });
    localStorage.setItem("firedeck.host", host);
    localStorage.setItem("firedeck.port", String(port));
    localStorage.setItem("firedeck.pairPort", String(pairPort));
    connectModal.close();
    hideAuthHelp();
    await refreshStatus();
    toast("Fire TV conectado", "ok");
    if ($("#view-control").classList.contains("active")) captureOnce();
  } catch (err) {
    showModalError(friendlyError(err));
    if (err.hint === "auth") {
      showAuthHelp();
    } else {
      hideAuthHelp();
    }
  } finally {
    btn.disabled = false;
    btn.textContent = "Conectar";
  }
});

/* --------------------------------- tabs --------------------------------- */

$$(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    $$(".tab").forEach((t) => t.classList.toggle("active", t === tab));
    const view = tab.dataset.view;
    history.replaceState(null, "", `#${view}`);
    $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));

    if (view === "apps" && state.status?.device && state.apps.length === 0) loadApps();
    if (view === "system" && state.status?.device && state.system.apps.length === 0) loadSystemApps();
    if (view === "files" && state.status?.device) loadFiles(state.files.path);
    if (view === "device" && state.status?.device && !state.deviceInfo) loadDeviceInfo();
    if (view === "network") loadNetwork();
    if (view === "install") loadDownloadHistory();
    if (view === "storage" && state.status?.device) loadStorage();
    if (view !== "control" && state.mirror.active) stopMirror();
    if (view === "control" && state.status?.device && !$("#screenBox").classList.contains("has-image")) {
      captureOnce();
    }
  });
});

/* -------------------------------- control -------------------------------- */

async function sendKey(key) {
  try {
    await api("/api/key", { method: "POST", body: { key } });
  } catch (err) {
    toast(friendlyError(err), "err");
  }
}

$$("[data-key]").forEach((btn) => {
  btn.addEventListener("click", () => sendKey(btn.dataset.key));
});

$("#textForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("#textInput");
  const text = input.value;
  if (!text.trim()) return;
  try {
    await api("/api/text", { method: "POST", body: { text } });
    input.value = "";
    toast("Texto enviado", "ok");
  } catch (err) {
    toast(friendlyError(err), "err");
  }
});

document.addEventListener("keydown", (e) => {
  const view = $("#view-control");
  if (!view.classList.contains("active")) return;
  if (connectModal.open || $("#helpModal").open) return;
  const tag = (e.target.tagName || "").toLowerCase();
  if (["input", "textarea", "select"].includes(tag) || e.target.isContentEditable) return;

  const map = {
    ArrowUp: "up",
    ArrowDown: "down",
    ArrowLeft: "left",
    ArrowRight: "right",
    Enter: "center",
    Backspace: "back",
  };
  const key = map[e.key];
  if (key) {
    e.preventDefault();
    sendKey(key);
  }
});

/* ------------------------------ screenshots ------------------------------ */

let shotInFlight = false;
let shotUrl = null;

function setScreenBusy(busy) {
  $("#screenLoading").classList.toggle("show", busy);
}

function showScreenNotice(message) {
  $("#screenBox").classList.add("notice");
  $("#screenNoticeText").textContent = message;
  $("#screenNotice").classList.add("show");
}

function clearScreenNotice() {
  $("#screenBox").classList.remove("notice");
  $("#screenNotice").classList.remove("show");
}

async function captureOnce(showErrors = false) {
  if (!state.status?.device) {
    $("#screenBox").classList.remove("has-image");
    clearScreenNotice();
    return;
  }
  if (shotInFlight) return;
  shotInFlight = true;
  setScreenBusy(true);
  try {
    const res = await fetch(`/api/screenshot?t=${Date.now()}&${tabParam()}`, {
      cache: "no-store",
      headers: tabHeaders(),
    });
    if (!res.ok) {
      let data = null;
      try {
        data = await res.json();
      } catch {
        /* la respuesta no era JSON */
      }
      const err = new Error(
        (data && data.error) || `No se pudo capturar la pantalla (${res.status}).`
      );
      err.black = Boolean(data && data.black);
      throw err;
    }
    const blob = await res.blob();
    if (shotUrl) URL.revokeObjectURL(shotUrl);
    shotUrl = URL.createObjectURL(blob);
    $("#screenImg").src = shotUrl;
    $("#screenBox").classList.add("has-image");
    clearScreenNotice();
  } catch (err) {
    $("#screenBox").classList.remove("has-image");
    if (err.black) {
      // No es un fallo de red: Fire OS no deja capturar esa capa. Mejor explicarlo
      // que dejar un rectángulo negro sin contexto.
      showScreenNotice(err.message);
    } else {
      clearScreenNotice();
      if (showErrors) toast(err.message, "err");
    }
  } finally {
    shotInFlight = false;
    setScreenBusy(false);
  }
}

/* ------------------------------ espejo en vivo --------------------------- */

function setMirrorBadge(text, bad = false) {
  const badge = $("#mirrorBadge");
  badge.classList.toggle("bad", bad);
  if (!text) {
    badge.classList.remove("show");
    badge.textContent = "";
    return;
  }
  badge.textContent = text;
  badge.classList.add("show");
}

function stopMirror() {
  const mirror = state.mirror;
  const controller = mirror.controller;
  const handle = mirror.handle;
  const wasActive = mirror.active;
  state.mirror = { active: false, controller: null, serial: "", handle: null };
  if (wasActive) {
    // Aunque el navegador corte la lectura, el servidor puede seguir esperando
    // al siguiente fotograma: se le pide que cierre screenrecord ya.
    api("/api/mirror/stop", {
      method: "POST",
      body: { serial: mirror.serial },
    }).catch(() => {});
  }
  if (controller) {
    try {
      controller.abort();
    } catch {
      /* ya estaba cerrado */
    }
  }
  if (handle && handle.stop) {
    try {
      handle.stop();
    } catch {
      /* ya estaba cerrado */
    }
  }
  $("#screenBox").classList.remove("mirroring");
  $("#liveToggle").checked = false;
  setMirrorBadge("");
}

function startSnapshotMode(message = "") {
  clearInterval(state.liveTimer);
  state.liveTimer = null;
  if (!state.status?.device) {
    setMirrorBadge("");
    return;
  }
  captureOnce(true);
  state.liveTimer = setInterval(() => captureOnce(false), 2000);
  setMirrorBadge("Modo capturas · 2 s", true);
  if (message) toast(message, "err");
}

async function startMirror() {
  if (!state.status?.device) {
    $("#liveToggle").checked = false;
    toast("Conecta un Fire TV para ver la pantalla.", "err");
    return;
  }
  if (state.rec.recording) {
    $("#liveToggle").checked = false;
    toast("Detén la grabación antes de abrir el espejo.", "err");
    return;
  }

  stopMirror();
  clearInterval(state.liveTimer);
  state.liveTimer = null;

  const controller = new AbortController();
  const serial = state.status.selected || state.status.device.serial || "";
  state.mirror = { active: true, controller, serial, handle: null };
  $("#liveToggle").checked = true;
  setMirrorBadge("Conectando…", true);
  // `screencap` y `screenrecord` no pueden usar la pantalla a la vez: si hay
  // una captura en curso, se espera a que termine antes de abrir el espejo.
  for (let waited = 0; waited < 50 && shotInFlight; waited += 1) {
    await new Promise((resolve) => setTimeout(resolve, 100));
  }

  if (!window.FireDeckMirror) {
    stopMirror();
    startSnapshotMode();
    return;
  }

  const alive = () => state.mirror.active && state.mirror.controller === controller;
  const giveUp = () => {
    if (!alive()) return;
    stopMirror();
    startSnapshotMode(
      "El espejo no recibió vídeo (TV en suspensión o pantalla sin cambios). Sigo con capturas."
    );
  };
  // Antes de las cabeceras se comprueba el estado de la TV y se limpia el
  // `screenrecord` anterior: eso puede tardar varios segundos, así que el
  // primer margen es amplio y se acorta en cuanto el flujo conecta.
  let watchdog = setTimeout(giveUp, 20000);
  const armWatchdog = (ms) => {
    clearTimeout(watchdog);
    watchdog = setTimeout(giveUp, ms);
  };

  try {
    const handle = await window.FireDeckMirror.start({
      url: `/api/mirror/raw?t=${Date.now()}&${tabParam()}`,
      canvas: $("#screenCanvas"),
      signal: controller.signal,
      onFrame() {
        clearTimeout(watchdog);
        const box = $("#screenBox");
        box.classList.add("mirroring");
        box.classList.add("has-image");
        box.classList.remove("notice");
        clearScreenNotice();
      },
      onStatus(status) {
        if (!alive()) return;
        if (status.state === "connecting") {
          armWatchdog(20000);
          setMirrorBadge("Conectando…", true);
        } else if (status.state === "live") {
          // Con la pantalla quieta `screenrecord` no manda fotogramas nuevos y
          // la tasa cae a 0: el espejo sigue vivo, así que no se muestra «0 fps».
          setMirrorBadge(status.fps > 0 ? `En vivo · ${status.fps} fps` : "En vivo");
        } else if (status.state === "ended") {
          stopMirror();
          startSnapshotMode(
            "El espejo se cortó (pantalla dormida o grabación en curso). Sigo con capturas."
          );
        }
      },
      onError(error) {
        if (!alive()) return;
        stopMirror();
        startSnapshotMode(`Espejo no disponible: ${error.message}`);
      },
    });
    if (!alive()) {
      handle.stop();
      return;
    }
    state.mirror.handle = handle;
  } catch (error) {
    clearTimeout(watchdog);
    if (error.name === "AbortError" || !alive()) return;
    stopMirror();
    startSnapshotMode(error.message || "No se pudo abrir el espejo.");
  }
}

$("#shotBtn").addEventListener("click", () => {
  if (state.mirror.active) {
    startMirror();
  } else {
    captureOnce(true);
  }
});

$("#liveToggle").addEventListener("change", (event) => {
  if (event.target.checked) {
    startMirror();
  } else {
    stopMirror();
  }
});

document.addEventListener("visibilitychange", () => {
  if (!document.hidden) return;
  if (state.mirror.active) stopMirror();
  if (state.liveTimer) {
    clearInterval(state.liveTimer);
    state.liveTimer = null;
    $("#liveToggle").checked = false;
    setMirrorBadge("");
  }
});

/* -------------------------- tocar sobre la captura ------------------------ */

function screenPoint(event) {
  const box = $("#screenBox");
  const canvas = $("#screenCanvas");
  const mirroring = box.classList.contains("mirroring") && canvas.width > 0;
  const sourceWidth = mirroring ? canvas.width : $("#screenImg").naturalWidth;
  const sourceHeight = mirroring ? canvas.height : $("#screenImg").naturalHeight;
  if (!sourceWidth || !sourceHeight) return null;
  const rect = box.getBoundingClientRect();
  const scale = Math.min(rect.width / sourceWidth, rect.height / sourceHeight);
  const width = sourceWidth * scale;
  const height = sourceHeight * scale;
  const left = rect.left + (rect.width - width) / 2;
  const top = rect.top + (rect.height - height) / 2;
  const x = (event.clientX - left) / width;
  const y = (event.clientY - top) / height;
  if (x < 0 || x > 1 || y < 0 || y > 1) return null;
  return { x, y, localX: event.clientX - rect.left, localY: event.clientY - rect.top };
}

function tapRipple(x, y, swipe = false) {
  const dot = document.createElement("span");
  dot.className = `tap-ripple${swipe ? " swipe" : ""}`;
  dot.style.left = `${x}px`;
  dot.style.top = `${y}px`;
  $("#screenBox").appendChild(dot);
  setTimeout(() => dot.remove(), 600);
}

let touchStart = null;

$("#screenBox").addEventListener("pointerdown", (event) => {
  const box = $("#screenBox");
  if (!box.classList.contains("has-image") || event.button !== 0) return;
  const point = screenPoint(event);
  if (!point) return;
  touchStart = {
    ...point,
    clientX: event.clientX,
    clientY: event.clientY,
    time: Date.now(),
  };
  event.preventDefault();
  try {
    box.setPointerCapture(event.pointerId);
  } catch {
    /* sin captura, el gesto sigue funcionando dentro de la tarjeta */
  }
});

$("#screenBox").addEventListener("pointerup", async (event) => {
  if (!touchStart) return;
  const start = touchStart;
  touchStart = null;
  const end = screenPoint(event);
  if (!end) return;
  const distance = Math.hypot(event.clientX - start.clientX, event.clientY - start.clientY);
  const swipe = distance >= 12;
  tapRipple(end.localX, end.localY, swipe);
  try {
    if (!swipe) {
      await api("/api/input", {
        method: "POST",
        body: { action: "tap", x: end.x, y: end.y },
      });
    } else {
      const duration = Math.max(120, Math.min(1500, Date.now() - start.time));
      await api("/api/input", {
        method: "POST",
        body: {
          action: "swipe",
          x: start.x,
          y: start.y,
          x2: end.x,
          y2: end.y,
          duration,
        },
      });
    }
  } catch (err) {
    toast(friendlyError(err), "err");
  }
});

$("#screenBox").addEventListener("pointercancel", () => {
  touchStart = null;
});

/* ---------------------------- grabar pantalla ---------------------------- */

function renderRecording(status) {
  state.rec = { ...state.rec, ...status };
  const dot = $("#recDot");
  const label = $("#recStatus");
  const meta = $("#recMeta");
  const button = $("#recBtn");
  const download = $("#recDownload");
  const duration = $("#recDuration");

  dot.className = "rec-dot";
  download.classList.add("hidden");
  duration.disabled = false;
  button.disabled = false;

  if (status.recording) {
    dot.classList.add("on");
    label.textContent = "Grabando…";
    meta.textContent = `Quedan ${Math.ceil(status.remaining || 0)} s · ${fmtBytes(
      status.size || 0
    )}`;
    button.textContent = "Detener";
    duration.disabled = true;
  } else if (status.done && status.error) {
    dot.classList.add("failed");
    label.textContent = "No se pudo grabar";
    meta.textContent = status.error;
    button.textContent = "Reintentar";
  } else if (status.done && status.has_file) {
    dot.classList.add("ready");
    label.textContent = "Vídeo listo";
    meta.textContent = `${Math.round(status.elapsed || status.duration || 0)} s · ${fmtBytes(
      status.size || 0
    )}`;
    button.textContent = "Grabar otra vez";
    download.classList.remove("hidden");
  } else {
    label.textContent = "Grabar pantalla";
    meta.textContent = "MP4 · elige la duración";
    button.textContent = "Grabar";
  }
}

function stopRecordingPoll() {
  if (state.recTimer) {
    clearInterval(state.recTimer);
    state.recTimer = null;
  }
}

function startRecordingPoll() {
  if (state.recTimer) return;
  state.recTimer = setInterval(async () => {
    const data = await refreshRecording();
    if (data && !data.recording) stopRecordingPoll();
  }, 1200);
}

async function refreshRecording() {
  try {
    const data = await api("/api/screenrecord/status");
    renderRecording(data);
    if (data.recording) startRecordingPoll();
    return data;
  } catch {
    return null;
  }
}

async function stopScreenRecording() {
  const button = $("#recBtn");
  button.disabled = true;
  button.textContent = "Deteniendo…";
  try {
    const data = await api("/api/screenrecord/stop", { method: "POST" });
    renderRecording(data);
    if (data.recording) startRecordingPoll();
    toast("Grabación detenida", "ok");
  } catch (err) {
    toast(friendlyError(err), "err");
    button.disabled = false;
    button.textContent = "Detener";
  }
}

$("#recBtn").addEventListener("click", async () => {
  if (state.rec.recording) {
    await stopScreenRecording();
    return;
  }
  if (state.mirror.active) stopMirror();
  const button = $("#recBtn");
  const duration = Number($("#recDuration").value) || 30;
  button.disabled = true;
  button.textContent = "Iniciando…";
  try {
    const data = await api("/api/screenrecord/start", {
      method: "POST",
      body: { duration },
    });
    renderRecording(data);
    startRecordingPoll();
    toast(`Grabando ${duration} s de pantalla`, "ok");
  } catch (err) {
    toast(friendlyError(err), "err");
    button.disabled = false;
    button.textContent = "Grabar";
  }
});

$("#recDownload").addEventListener("click", async () => {
  const button = $("#recDownload");
  button.disabled = true;
  button.textContent = "Descargando…";
  try {
    const res = await fetch(`/api/screenrecord/download?${tabParam()}`, {
      headers: tabHeaders(),
    });
    if (!res.ok) {
      let data = null;
      try {
        data = await res.json();
      } catch {
        /* respuesta no JSON */
      }
      throw new Error((data && data.error) || `No se pudo descargar (${res.status}).`);
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    const stamp = new Date().toISOString().slice(0, 19).replace(/[T:]/g, "-");
    link.href = url;
    link.download = `firedeck-${stamp}.mp4`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 60000);
    toast("Vídeo descargado", "ok");
    renderRecording({ recording: false, done: false, error: "", has_file: false });
  } catch (err) {
    toast(friendlyError(err), "err");
  } finally {
    button.disabled = false;
    button.textContent = "Descargar vídeo";
  }
});

/* --------------------------------- apps ---------------------------------- */

const appInfoCache = new Map();
const ALIAS_KEY = "firedeck.aliases";

function loadAliases() {
  try {
    const parsed = JSON.parse(localStorage.getItem(ALIAS_KEY) || "{}");
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function saveAliases(aliases) {
  localStorage.setItem(ALIAS_KEY, JSON.stringify(aliases));
}

const CATEGORY_ICONS = {
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
};

function appInitial(text) {
  const source = (text || "?").trim();
  const match = source.match(/[\p{L}\p{N}]/u);
  return match ? match[0].toUpperCase() : "?";
}

function appHue(text) {
  let hash = 0;
  for (let i = 0; i < text.length; i += 1) {
    hash = (hash * 31 + text.charCodeAt(i)) % 360;
  }
  return hash;
}

function categoryIcon(app) {
  return app.icon || CATEGORY_ICONS[app.category] || "📦";
}

function renderAppDetails(app, box, info) {
  box.textContent = "";
  const rows = [];
  if (info.version) rows.push(["Versión", info.version]);
  if (info.size) rows.push(["Tamaño", info.size]);
  if (info.first_install) rows.push(["Instalada", info.first_install]);
  if (info.last_update) rows.push(["Actualizada", info.last_update]);
  if (info.target_sdk) {
    const sdk = `API ${info.target_sdk}${info.min_sdk ? ` · mínimo ${info.min_sdk}` : ""}`;
    rows.push(["Android", sdk]);
  }
  if (info.activity) rows.push(["Actividad", info.activity]);
  if (info.apk) rows.push(["APK", info.apk]);
  if (info.split_count > 1) rows.push(["APK divididos", `${info.split_count} archivos`]);

  rows.forEach(([key, value]) => {
    const line = document.createElement("div");
    line.className = "kv";
    const k = document.createElement("span");
    k.className = "k";
    k.textContent = key;
    const v = document.createElement("span");
    v.className = "v";
    v.textContent = value;
    line.append(k, v);
    box.appendChild(line);
  });

  const notes = [];
  if (!info.known) {
    notes.push(
      "El nombre se dedujo del identificador del paquete; no es el nombre oficial de la app."
    );
  }
  if (!rows.length) {
    notes.push("El Fire TV no devolvió datos de esta app.");
  }
  notes.forEach((text) => {
    const note = document.createElement("p");
    note.className = "app-detail-note";
    note.textContent = text;
    box.appendChild(note);
  });

  const aliases = loadAliases();
  const current = aliases[app.package] || "";
  const rename = document.createElement("button");
  rename.type = "button";
  rename.className = "link-btn app-rename";
  rename.textContent = current
    ? "Cambiar o quitar el nombre personalizado"
    : "Poner un nombre a esta app";
  rename.addEventListener("click", () => {
    const value = window.prompt(
      "Nombre para esta app (vacío para quitar el nombre personalizado):",
      current || app.name
    );
    if (value === null) return;
    const map = loadAliases();
    const clean = value.trim();
    if (clean && clean !== app.name) {
      map[app.package] = clean;
    } else {
      delete map[app.package];
    }
    saveAliases(map);
    renderApps();
    toast(clean ? `Ahora se llama ${clean}` : "Nombre personalizado eliminado", "ok");
  });

  const actions = document.createElement("div");
  actions.className = "app-detail-actions";
  const download = document.createElement("button");
  download.type = "button";
  download.className = "btn-mini download";
  download.textContent =
    info.split_count > 1 ? "⬇ Descargar APKs (.zip)" : "⬇ Descargar APK";
  download.title = info.split_count > 1
    ? "Descarga un ZIP con el APK base y sus divisiones"
    : "Descarga el APK instalado en el Fire TV";
  download.addEventListener("click", () => {
    const frame = document.createElement("iframe");
    frame.style.display = "none";
    frame.src = `/api/apps/apk?package=${encodeURIComponent(app.package)}&${tabParam()}`;
    document.body.appendChild(frame);
    setTimeout(() => frame.remove(), 120000);
    toast("Preparando la descarga del APK…", "ok");
  });
  actions.append(download, rename);
  box.appendChild(actions);
}

async function toggleAppDetails(app, box, button) {
  if (!box.classList.contains("hidden")) {
    box.classList.add("hidden");
    button.textContent = "Info";
    return;
  }
  box.classList.remove("hidden");
  button.textContent = "Ocultar";

  const cached = appInfoCache.get(app.package);
  if (cached) {
    renderAppDetails(app, box, cached);
    return;
  }

  box.textContent = "Leyendo datos del Fire TV…";
  try {
    const data = await api("/api/apps/info", {
      method: "POST",
      body: { package: app.package },
    });
    appInfoCache.set(app.package, data.app);
    renderAppDetails(app, box, data.app);
  } catch (err) {
    box.textContent = friendlyError(err);
  }
}

function renderApps() {
  const list = $("#appList");
  const term = $("#appSearch").value.trim().toLowerCase();
  const aliases = loadAliases();
  const nameOf = (app) => aliases[app.package] || app.name;
  const apps = state.apps.filter((app) => {
    if (!term) return true;
    return (
      nameOf(app).toLowerCase().includes(term) ||
      app.package.toLowerCase().includes(term) ||
      (app.category || "").toLowerCase().includes(term)
    );
  });
  $("#appsCount").textContent = `${apps.length} de ${state.apps.length} apps`;

  if (!apps.length) {
    list.innerHTML = `<div class="empty-state"><span>🔍</span><p>${
      state.apps.length ? "Ninguna app coincide con el filtro." : "No hay apps para mostrar."
    }</p></div>`;
    return;
  }

  const frag = document.createDocumentFragment();
  apps.forEach((app) => {
    const displayName = nameOf(app);
    const item = document.createElement("div");
    item.className = "app-item";

    const row = document.createElement("div");
    row.className = "app-row";

    const main = document.createElement("div");
    main.className = "app-main";

    const badge = document.createElement("div");
    badge.className = "app-badge";
    badge.textContent = appInitial(displayName);
    badge.title = app.package;
    const hue = appHue(app.package);
    badge.style.background = `hsl(${hue} 82% 95%)`;
    badge.style.color = `hsl(${hue} 52% 32%)`;
    badge.style.borderColor = `hsl(${hue} 58% 85%)`;

    const icon = document.createElement("img");
    icon.className = "app-icon";
    icon.alt = "";
    icon.loading = "lazy";
    icon.decoding = "async";
    icon.src = `/api/apps/icon?package=${encodeURIComponent(app.package)}&v=3&${tabParam()}`;
    icon.addEventListener("load", () => badge.classList.add("has-icon"));
    icon.addEventListener("error", () => icon.remove());
    badge.appendChild(icon);

    const text = document.createElement("div");
    text.className = "app-text";

    const title = document.createElement("div");
    title.className = "app-title";
    const name = document.createElement("span");
    name.className = "app-name";
    name.textContent = displayName;
    name.title = `${displayName}\n${app.package}`;
    const tag = document.createElement("span");
    tag.className = "app-cat";
    tag.textContent = `${categoryIcon(app)} ${app.category}`;
    title.append(name, tag);

    const pkg = document.createElement("button");
    pkg.type = "button";
    pkg.className = "app-pkg";
    pkg.textContent = app.package;
    pkg.title = "Copiar identificador";
    pkg.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(app.package);
        toast("Paquete copiado", "ok");
      } catch {
        toast(app.package);
      }
    });

    text.append(title, pkg);
    main.append(badge, text);

    const details = document.createElement("div");
    details.className = "app-details hidden";

    const actions = document.createElement("div");
    actions.className = "app-actions";

    const infoBtn = document.createElement("button");
    infoBtn.className = "btn-mini";
    infoBtn.textContent = "Info";
    infoBtn.title = "Versión, tamaño y ruta del APK";
    infoBtn.addEventListener("click", () => toggleAppDetails(app, details, infoBtn));

    const open = document.createElement("button");
    open.className = "btn-mini open";
    open.textContent = "Abrir";
    open.addEventListener("click", async () => {
      open.disabled = true;
      try {
        await api("/api/apps/action", {
          method: "POST",
          body: { package: app.package, action: "launch" },
        });
        toast(`Abriendo ${displayName}`, "ok");
      } catch (err) {
        toast(friendlyError(err), "err");
      } finally {
        open.disabled = false;
      }
    });

    const stop = document.createElement("button");
    stop.className = "btn-mini";
    stop.textContent = "Detener";
    stop.addEventListener("click", () => appAction(app, "stop", false));

    const dataBtn = document.createElement("button");
    dataBtn.className = "btn-mini";
    dataBtn.textContent = "Borrar datos";
    dataBtn.title = "Borra configuración y sesiones de la app";
    dataBtn.addEventListener("click", () => {
      if (confirm(`¿Borrar TODOS los datos de ${displayName}?\n\nSe cerrará la sesión y se perderá su configuración. Esta acción no se puede deshacer.`)) {
        appAction(app, "clear-data", true, true);
      }
    });

    const del = document.createElement("button");
    del.className = "btn-mini danger";
    del.textContent = "✕";
    del.title = `Desinstalar ${displayName}`;
    del.addEventListener("click", () => {
      if (confirm(`¿Desinstalar ${displayName} (${app.package}) del Fire TV?`)) {
        appAction(app, "uninstall", false, true);
      }
    });

    actions.append(infoBtn, open, stop, dataBtn, del);
    row.append(main, actions);
    item.append(row, details);
    frag.appendChild(item);
  });
  list.innerHTML = "";
  list.appendChild(frag);
}

async function appAction(app, action, confirmFlag = false, reload = false) {
  try {
    await api("/api/apps/action", {
      method: "POST",
      body: { package: app.package, action, confirm: confirmFlag },
    });
    toast("Hecho", "ok");
    if (reload) loadApps();
  } catch (err) {
    toast(friendlyError(err), "err");
  }
}

async function loadApps() {
  const list = $("#appList");
  list.innerHTML = '<div class="empty-state"><span>⏳</span><p>Cargando apps del Fire TV…</p></div>';
  $("#appsCount").textContent = "Cargando…";
  try {
    const system = $("#systemToggle").checked ? 1 : 0;
    const data = await api(`/api/apps?system=${system}`);
    state.apps = data.apps || [];
    appInfoCache.clear();
    renderApps();
  } catch (err) {
    showEmptyState(list, "⚠️", friendlyError(err));
    $("#appsCount").textContent = "Sin datos";
  }
}

$("#appsRefresh").addEventListener("click", loadApps);
$("#systemToggle").addEventListener("change", loadApps);
$("#appSearch").addEventListener("input", renderApps);

/* ----------------------------- apps del sistema --------------------------- */

const SAFETY_LABELS = { safe: "Segura", caution: "Precaución", critical: "Crítica" };
const SAFETY_ICONS = { safe: "✅", caution: "⚠️", critical: "⛔" };

function renderSystemApps() {
  const list = $("#systemList");
  const term = $("#systemSearch").value.trim().toLowerCase();
  const safeOnly = $("#systemSafeOnly").checked;
  const aliases = loadAliases();
  const nameOf = (app) => aliases[app.package] || app.name;
  const counts = state.system.counts || {};

  const apps = state.system.apps.filter((app) => {
    // «Solo seguras» deja siempre visibles las desactivadas para poder revertirlas.
    if (safeOnly && app.safety !== "safe" && app.state !== "disabled") return false;
    if (!term) return true;
    return (
      nameOf(app).toLowerCase().includes(term) ||
      app.package.toLowerCase().includes(term) ||
      (app.category || "").toLowerCase().includes(term)
    );
  });

  const disabled = counts.disabled || 0;
  const total = counts.total || state.system.apps.length;
  $("#systemCount").textContent =
    `${apps.length} de ${total} apps del sistema · ${disabled} desactivada` +
    `${disabled === 1 ? "" : "s"} · ${counts.safe || 0} seguras · ` +
    `${counts.critical || 0} protegidas`;
  $("#systemRestore").disabled = !(counts.tracked > 0);

  if (!apps.length) {
    list.innerHTML = `<div class="empty-state"><span>🔍</span><p>${
      state.system.apps.length
        ? "Ninguna app del sistema coincide con el filtro."
        : "No hay apps del sistema para mostrar."
    }</p></div>`;
    return;
  }

  const frag = document.createDocumentFragment();
  apps.forEach((app) => {
    const displayName = nameOf(app);
    const item = document.createElement("div");
    item.className = `app-item${app.state === "disabled" ? " is-disabled" : ""}`;

    const row = document.createElement("div");
    row.className = "app-row";

    const main = document.createElement("div");
    main.className = "app-main";

    const badge = document.createElement("div");
    badge.className = "app-badge";
    badge.textContent = appInitial(displayName);
    badge.title = app.package;
    const hue = appHue(app.package);
    badge.style.background = `hsl(${hue} 82% 95%)`;
    badge.style.color = `hsl(${hue} 52% 32%)`;
    badge.style.borderColor = `hsl(${hue} 58% 85%)`;

    const icon = document.createElement("img");
    icon.className = "app-icon";
    icon.alt = "";
    icon.loading = "lazy";
    icon.decoding = "async";
    icon.src = `/api/apps/icon?package=${encodeURIComponent(app.package)}&v=3&${tabParam()}`;
    icon.addEventListener("load", () => badge.classList.add("has-icon"));
    icon.addEventListener("error", () => icon.remove());
    badge.appendChild(icon);

    const text = document.createElement("div");
    text.className = "app-text";

    const title = document.createElement("div");
    title.className = "app-title";
    const name = document.createElement("span");
    name.className = "app-name";
    name.textContent = displayName;
    name.title = `${displayName}\n${app.package}`;

    const safety = document.createElement("span");
    safety.className = `chip-safety ${app.safety}`;
    safety.textContent = `${SAFETY_ICONS[app.safety] || "•"} ${SAFETY_LABELS[app.safety] || app.safety}`;
    safety.title = app.safety_reason || "";

    const status = document.createElement("span");
    status.className = `chip-state ${app.state}`;
    status.textContent = app.state === "disabled" ? "Desactivada" : "Activa";
    if (app.tracked && app.state === "disabled") {
      status.title = "Desactivada desde FireDeck en este dispositivo";
    }

    title.append(name, safety, status);

    const pkg = document.createElement("button");
    pkg.type = "button";
    pkg.className = "app-pkg";
    pkg.textContent = app.package;
    pkg.title = "Copiar identificador";
    pkg.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(app.package);
        toast("Paquete copiado", "ok");
      } catch {
        toast(app.package);
      }
    });

    text.append(title, pkg);
    main.append(badge, text);

    const actions = document.createElement("div");
    actions.className = "app-actions";

    const toggle = document.createElement("button");
    toggle.className = "btn-mini";
    if (app.state === "disabled") {
      toggle.classList.add("enable");
      toggle.textContent = "Activar";
      toggle.title = app.safety_reason || "Volver a activar esta app";
      toggle.addEventListener("click", () => toggleSystemApp(app, true, toggle));
    } else if (app.safety === "critical") {
      toggle.classList.add("locked");
      toggle.textContent = "Protegida";
      toggle.disabled = true;
      toggle.title = app.safety_reason || "Paquete esencial del sistema";
    } else {
      toggle.classList.add("warn");
      toggle.textContent = "Desactivar";
      toggle.title = "Ocultar esta app sin borrar su APK";
      toggle.addEventListener("click", () => toggleSystemApp(app, false, toggle));
    }
    actions.append(toggle);

    row.append(main, actions);
    item.append(row);
    frag.appendChild(item);
  });
  list.innerHTML = "";
  list.appendChild(frag);
}

async function toggleSystemApp(app, enable, button) {
  const aliases = loadAliases();
  const displayName = aliases[app.package] || app.name;
  if (!enable) {
    const extra =
      app.safety === "caution"
        ? `\n\n⚠️ Precaución: ${app.safety_reason}`
        : `\n\n${app.safety_reason}`;
    const ok = confirm(
      `¿Desactivar ${displayName} (${app.package})?${extra}\n\n` +
      "El APK no se borra y podrás reactivarla desde esta pestaña."
    );
    if (!ok) return;
  }

  const previous = button.textContent;
  button.disabled = true;
  button.textContent = enable ? "Activando…" : "Desactivando…";
  try {
    const data = await api(`/api/debloat/${enable ? "enable" : "disable"}`, {
      method: "POST",
      body: { package: app.package, confirm: !enable },
    });
    toast(data.message || (enable ? "App activada" : "App desactivada"), "ok");
    await loadSystemApps();
  } catch (err) {
    toast(friendlyError(err), "err");
    button.disabled = false;
    button.textContent = previous;
  }
}

async function loadSystemApps() {
  const list = $("#systemList");
  list.innerHTML = '<div class="empty-state"><span>⏳</span><p>Leyendo apps del sistema…</p></div>';
  $("#systemCount").textContent = "Cargando…";
  try {
    const data = await api("/api/debloat");
    state.system.apps = data.apps || [];
    state.system.counts = data.counts || null;
    state.system.serial = data.serial || "";
    renderSystemApps();
  } catch (err) {
    showEmptyState(list, "⚠️", friendlyError(err));
    $("#systemCount").textContent = "Sin datos";
  }
}

async function restoreSystemApps() {
  const count = state.system.counts?.tracked || 0;
  if (!count) return;
  const ok = confirm(
    `¿Reactivar ${count} app(s) que desactivaste desde FireDeck en este Fire TV?\n\n` +
    "No se tocan las apps que ya estaban desactivadas antes. Los APK nunca se borraron."
  );
  if (!ok) return;

  const button = $("#systemRestore");
  button.disabled = true;
  button.textContent = "Restaurando…";
  try {
    const data = await api("/api/debloat/restore", { method: "POST" });
    toast(data.message || "Apps reactivadas", "ok");
    if (data.failed && data.failed.length) {
      toast(`${data.failed.length} app(s) no se pudieron reactivar`, "err");
    }
    await loadSystemApps();
  } catch (err) {
    toast(friendlyError(err), "err");
    button.textContent = "Restaurar desactivadas";
    button.disabled = false;
  }
}

$("#systemRefresh").addEventListener("click", loadSystemApps);
$("#systemSearch").addEventListener("input", renderSystemApps);
$("#systemSafeOnly").addEventListener("change", renderSystemApps);
$("#systemRestore").addEventListener("click", restoreSystemApps);

/* ------------------------------- archivos -------------------------------- */

const FILE_ROOT = "/sdcard";
const filesInput = $("#filesInput");
const fileList = $("#fileList");

function fileMetaText(entry) {
  const parts = [];
  if (entry.dir) {
    parts.push("Carpeta");
  } else if (entry.link) {
    parts.push(entry.target ? `Enlace → ${entry.target}` : "Enlace");
  } else {
    parts.push(fmtBytes(entry.size));
  }
  if (entry.date) parts.push(entry.date);
  return parts.join(" · ");
}

function renderFileBreadcrumb(path) {
  const nav = $("#filesBreadcrumb");
  nav.innerHTML = "";
  const root = document.createElement("button");
  root.type = "button";
  root.className = "crumb";
  root.textContent = "Almacenamiento";
  root.addEventListener("click", () => loadFiles(FILE_ROOT));
  nav.appendChild(root);

  const rest = path === FILE_ROOT ? [] : path.slice(FILE_ROOT.length).split("/").filter(Boolean);
  let accumulated = FILE_ROOT;
  rest.forEach((part) => {
    accumulated += `/${part}`;
    const sep = document.createElement("span");
    sep.className = "crumb-sep";
    sep.textContent = "/";
    const crumb = document.createElement("button");
    crumb.type = "button";
    crumb.className = "crumb";
    crumb.textContent = part;
    crumb.title = accumulated;
    crumb.addEventListener("click", () => loadFiles(accumulated));
    nav.append(sep, crumb);
  });
  const last = nav.lastElementChild;
  if (rest.length && last) last.classList.add("current");
  if (!rest.length) root.classList.add("current");
}

function openRemoteFolder(entry) {
  loadFiles(entry.path);
}

function downloadRemoteFile(entry) {
  toast(`Preparando «${entry.name}»…`, "ok");
  const link = document.createElement("a");
  link.href = `/api/files/download?path=${encodeURIComponent(entry.path)}&${tabParam()}`;
  link.download = entry.name;
  document.body.appendChild(link);
  link.click();
  link.remove();
}

async function renameRemoteFile(entry) {
  const name = prompt(`Nuevo nombre para «${entry.name}»:`, entry.name);
  if (name === null) return;
  const clean = name.trim();
  if (!clean || clean === entry.name) return;
  try {
    const data = await api("/api/files/rename", {
      method: "POST",
      body: { path: entry.path, name: clean },
    });
    toast(data.output || "Renombrado", "ok");
    loadFiles(state.files.path);
  } catch (err) {
    toast(friendlyError(err), "err");
  }
}

async function deleteRemoteFile(entry) {
  const what = entry.dir
    ? `la carpeta «${entry.name}» y TODO su contenido`
    : `«${entry.name}»`;
  if (!confirm(`¿Borrar ${what} del Fire TV?\n\nEsta acción no se puede deshacer.`)) return;
  try {
    const data = await api("/api/files/delete", {
      method: "POST",
      body: { path: entry.path, confirm: true },
    });
    toast(data.output || "Borrado", "ok");
    loadFiles(state.files.path);
  } catch (err) {
    toast(friendlyError(err), "err");
  }
}

function renderFiles() {
  const { path, parent, entries } = state.files;
  $("#filesMeta").textContent = `${entries.length} elemento${entries.length === 1 ? "" : "s"} · ${path}`;
  $("#filesUp").disabled = !parent;
  renderFileBreadcrumb(path);

  if (!entries.length) {
    fileList.innerHTML =
      '<div class="empty-state"><span>📂</span><p>Esta carpeta está vacía. Arrastra archivos aquí o pulsa «Subir archivos».</p></div>';
    return;
  }

  const frag = document.createDocumentFragment();
  entries.forEach((entry) => {
    const item = document.createElement("div");
    item.className = "file-item";
    const row = document.createElement("div");
    row.className = "file-row";

    const main = document.createElement("div");
    main.className = "file-main";

    const badge = document.createElement("div");
    badge.className = `file-badge${entry.dir ? " dir" : ""}`;
    badge.textContent = entry.dir ? "📁" : entry.link ? "🔗" : "📄";
    badge.title = entry.path;

    const text = document.createElement("div");
    text.className = "file-text";

    const name = document.createElement("button");
    name.type = "button";
    name.className = "file-name";
    name.textContent = entry.name;
    name.title = entry.dir ? `Abrir ${entry.path}` : `Descargar ${entry.name}`;
    name.addEventListener("click", () =>
      entry.dir ? openRemoteFolder(entry) : downloadRemoteFile(entry)
    );

    const meta = document.createElement("span");
    meta.className = "file-meta";
    meta.textContent = fileMetaText(entry);

    text.append(name, meta);
    main.append(badge, text);

    const actions = document.createElement("div");
    actions.className = "file-actions";

    const openBtn = document.createElement("button");
    openBtn.className = "btn-mini open";
    openBtn.textContent = entry.dir ? "Abrir" : "Descargar";
    openBtn.addEventListener("click", () =>
      entry.dir ? openRemoteFolder(entry) : downloadRemoteFile(entry)
    );

    const renameBtn = document.createElement("button");
    renameBtn.className = "btn-mini";
    renameBtn.textContent = "Renombrar";
    renameBtn.addEventListener("click", () => renameRemoteFile(entry));

    const deleteBtn = document.createElement("button");
    deleteBtn.className = "btn-mini danger";
    deleteBtn.textContent = "Borrar";
    deleteBtn.addEventListener("click", () => deleteRemoteFile(entry));

    actions.append(openBtn, renameBtn, deleteBtn);
    row.append(main, actions);
    item.append(row);
    frag.appendChild(item);
  });
  fileList.innerHTML = "";
  fileList.appendChild(frag);
}

async function loadFiles(path = FILE_ROOT) {
  if (!state.status?.device) {
    fileList.innerHTML =
      '<div class="empty-state"><span>📺</span><p>Conecta un Fire TV para explorar sus archivos.</p></div>';
    $("#filesMeta").textContent = "Sin dispositivo conectado";
    return;
  }
  state.files.loading = true;
  fileList.innerHTML = '<div class="empty-state"><span>⏳</span><p>Leyendo la carpeta…</p></div>';
  try {
    const data = await api(`/api/files?path=${encodeURIComponent(path)}`);
    state.files = {
      path: data.path,
      parent: data.parent,
      entries: data.entries || [],
      loading: false,
    };
    renderFiles();
  } catch (err) {
    state.files = { path, parent: path === FILE_ROOT ? null : FILE_ROOT, entries: [], loading: false };
    $("#filesMeta").textContent = "No se pudo leer la carpeta";
    showEmptyState(fileList, "⚠️", friendlyError(err));
    renderFileBreadcrumb(path);
    $("#filesUp").disabled = !state.files.parent;
  }
}

function uploadOneFile(file, overwrite, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const query = `path=${encodeURIComponent(state.files.path)}&overwrite=${overwrite ? 1 : 0}`;
    xhr.open("POST", `/api/files/upload?${query}&${tabParam()}`);
    xhr.setRequestHeader("X-Filename", encodeURIComponent(file.name));
    xhr.setRequestHeader(TAB_HEADER, tabId);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.loaded);
    };
    xhr.onload = () => {
      let data = null;
      try {
        data = JSON.parse(xhr.responseText);
      } catch {
        /* respuesta no JSON */
      }
      if (xhr.status >= 200 && xhr.status < 300 && data?.ok) resolve(data);
      else reject(new Error((data && data.error) || `Error ${xhr.status}`));
    };
    xhr.onerror = () => reject(new Error("No se pudo conectar con FireDeck."));
    xhr.send(file);
  });
}

async function uploadFiles(fileListObject) {
  const files = Array.from(fileListObject || []);
  if (!files.length) return;
  if (!state.status?.device) {
    toast("Conecta un Fire TV primero", "err");
    return;
  }

  const existing = new Set(state.files.entries.map((entry) => entry.name));
  const queue = [];
  for (const file of files) {
    let overwrite = false;
    if (existing.has(file.name)) {
      overwrite = confirm(
        `Ya existe «${file.name}» en ${state.files.path}.\n\n¿Quieres reemplazarlo?`
      );
      if (!overwrite) continue;
    }
    queue.push({ file, overwrite });
  }
  if (!queue.length) return;

  const total = queue.reduce((sum, item) => sum + Math.max(1, item.file.size), 0);
  const progress = $("#filesProgress");
  const bar = $("#filesBar");
  const status = $("#filesStatus");
  progress.classList.remove("hidden");
  status.classList.remove("hidden");
  bar.style.width = "0%";

  let completed = 0;
  try {
    for (let index = 0; index < queue.length; index += 1) {
      const { file, overwrite } = queue[index];
      status.textContent = `Subiendo «${file.name}» (${index + 1}/${queue.length})…`;
      await uploadOneFile(file, overwrite, (loaded) => {
        const pct = Math.min(100, ((completed + loaded) / total) * 100);
        bar.style.width = `${pct}%`;
      });
      completed += Math.max(1, file.size);
      bar.style.width = `${Math.min(100, (completed / total) * 100)}%`;
    }
    toast(
      queue.length === 1 ? "Archivo subido" : `${queue.length} archivos subidos`,
      "ok"
    );
  } catch (err) {
    toast(friendlyError(err), "err");
    status.textContent = friendlyError(err);
  } finally {
    loadFiles(state.files.path);
    setTimeout(() => {
      progress.classList.add("hidden");
      status.classList.add("hidden");
      bar.style.width = "0%";
    }, 2200);
  }
}

$("#filesRefresh").addEventListener("click", () => loadFiles(state.files.path));
$("#filesUp").addEventListener("click", () => {
  if (state.files.parent) loadFiles(state.files.parent);
});
$("#filesUpload").addEventListener("click", () => filesInput.click());
filesInput.addEventListener("change", () => {
  uploadFiles(filesInput.files);
  filesInput.value = "";
});

$("#filesMkdir").addEventListener("click", async () => {
  const name = prompt("Nombre de la nueva carpeta:");
  if (name === null) return;
  const clean = name.trim();
  if (!clean) return;
  try {
    const data = await api("/api/files/mkdir", {
      method: "POST",
      body: { path: `${state.files.path.replace(/\/+$/, "")}/${clean}` },
    });
    toast(data.output || "Carpeta creada", "ok");
    loadFiles(state.files.path);
  } catch (err) {
    toast(friendlyError(err), "err");
  }
});

["dragenter", "dragover"].forEach((eventName) =>
  fileList.addEventListener(eventName, (event) => {
    event.preventDefault();
    fileList.classList.add("drag");
  })
);
["dragleave", "drop"].forEach((eventName) =>
  fileList.addEventListener(eventName, (event) => {
    event.preventDefault();
    if (eventName === "dragleave" && fileList.contains(event.relatedTarget)) return;
    fileList.classList.remove("drag");
  })
);
fileList.addEventListener("drop", (event) => uploadFiles(event.dataTransfer.files));

/* -------------------------------- storage -------------------------------- */

async function loadStorage() {
  try {
    const data = await api("/api/storage");
    $("#statTotal").textContent = data.total || "—";
    $("#statUsed").textContent = data.used || "—";
    $("#statAvail").textContent = data.available || "—";
    $("#storageRaw").textContent = data.raw || "(sin salida)";
  } catch (err) {
    toast(friendlyError(err), "err");
  }
}

$("#storageRefresh").addEventListener("click", loadStorage);

$("#trimBtn").addEventListener("click", async () => {
  const btn = $("#trimBtn");
  btn.disabled = true;
  btn.textContent = "Liberando…";
  try {
    const data = await api("/api/storage/trim", {
      method: "POST",
      body: { size: "512G" },
    });
    toast(data.output || "Caché liberada", "ok");
    await loadStorage();
  } catch (err) {
    toast(friendlyError(err), "err");
  } finally {
    btn.disabled = false;
    btn.textContent = "Liberar caché ahora";
  }
});

/* ------------------------------ dispositivo ------------------------------ */

function fmtUptime(seconds) {
  const total = Math.max(0, Math.floor(Number(seconds) || 0));
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (days) return `${days} d ${hours} h`;
  if (hours) return `${hours} h ${minutes} min`;
  return `${minutes} min`;
}

function infoRow(label, value, mono = false) {
  const row = document.createElement("div");
  row.className = "info-row";
  const key = document.createElement("span");
  key.className = "k";
  key.textContent = label;
  const val = document.createElement("span");
  val.className = `v${mono ? " mono" : ""}`;
  val.textContent = value || "—";
  row.append(key, val);
  return row;
}

function infoMeter(used, total) {
  const wrap = document.createElement("div");
  wrap.className = "info-meter";
  const bar = document.createElement("span");
  const pct = total > 0 ? Math.max(0, Math.min(100, (used / total) * 100)) : 0;
  bar.style.width = `${pct}%`;
  if (pct >= 85) wrap.classList.add("hot");
  wrap.appendChild(bar);
  return wrap;
}

function infoCard(title, rows, { meter = null, note = "" } = {}) {
  const card = document.createElement("div");
  card.className = "info-card";
  const heading = document.createElement("h3");
  heading.textContent = title;
  card.appendChild(heading);
  const list = document.createElement("div");
  list.className = "info-rows";
  rows.forEach(([label, value, mono]) => list.appendChild(infoRow(label, value, mono)));
  card.appendChild(list);
  if (meter) card.appendChild(meter);
  if (note) {
    const hint = document.createElement("p");
    hint.className = "info-note";
    hint.textContent = note;
    card.appendChild(hint);
  }
  return card;
}

function renderDeviceInfo(device) {
  const heroName = $("#deviceHeroName");
  const heroMeta = $("#deviceHeroMeta");
  const grid = $("#deviceInfoGrid");
  grid.textContent = "";

  if (!device) {
    heroName.textContent = "Sin conectar";
    heroMeta.textContent = "Conecta un Fire TV para ver sus datos.";
    const placeholder = document.createElement("div");
    placeholder.className = "info-placeholder";
    placeholder.textContent = "No hay ningún dispositivo conectado.";
    grid.appendChild(placeholder);
    return;
  }

  heroName.textContent = device.alias || device.model || "Fire TV";
  heroMeta.textContent = [
    device.brand,
    device.model,
    device.fireos,
    device.android ? `Android ${device.android} (API ${device.sdk})` : "",
  ]
    .filter(Boolean)
    .join(" · ");

  const resolution = device.resolution?.width
    ? `${device.resolution.width} × ${device.resolution.height}`
    : "";
  const memory = device.memory || {};
  const storage = device.storage || {};
  const cpu = device.cpu || {};
  const memPct = memory.total
    ? Math.round((memory.used / memory.total) * 100)
    : 0;
  const storagePct = storage.total
    ? Math.round((storage.used / storage.total) * 100)
    : 0;
  const zones = device.temperature?.zones || [];

  grid.append(
    infoCard("Sistema", [
      ["Modelo", device.model],
      ["Marca", device.brand],
      ["Fire OS", device.fireos],
      ["Android", device.android ? `${device.android} (API ${device.sdk})` : ""],
      ["Número de serie", device.serial_number, true],
      ["Encendido", fmtUptime(device.uptime)],
    ]),
    infoCard("Red", [
      ["IP", device.ip],
      ["MAC", device.mac, true],
      ["Conexión ADB", device.serial, true],
    ]),
    infoCard("Pantalla", [
      ["Resolución", resolution],
      ["Densidad", device.density ? `${device.density} ppp` : ""],
    ]),
    infoCard("Procesador", [
      ["Modelo", cpu.model],
      ["Núcleos", cpu.cores ? String(cpu.cores) : ""],
      ["Uso ahora", cpu.usage != null ? `${cpu.usage} %` : ""],
    ]),
    infoCard(
      "Memoria RAM",
      [
        ["Total", fmtBytes(memory.total)],
        ["En uso", `${fmtBytes(memory.used)} (${memPct} %)`],
        ["Disponible", fmtBytes(memory.available)],
      ],
      { meter: infoMeter(memory.used, memory.total) }
    ),
    infoCard(
      "Almacenamiento",
      [
        ["Total", fmtBytes(storage.total)],
        ["Usado", `${fmtBytes(storage.used)} (${storagePct} %)`],
        ["Disponible", fmtBytes(storage.available)],
      ],
      { meter: infoMeter(storage.used, storage.total) }
    ),
    infoCard(
      "Temperatura",
      zones.length
        ? zones.map((zone) => [zone.name, `${zone.celsius.toFixed(1)} °C`])
        : [["Sensores", "No disponible"]],
      {
        note: zones.length
          ? `Máxima: ${device.temperature.max.toFixed(1)} °C`
          : "Este Fire TV no publica sensores térmicos por ADB.",
      }
    )
  );
}

async function loadDeviceInfo() {
  if (!state.status?.device) {
    renderDeviceInfo(null);
    toast("Conecta un Fire TV primero", "err");
    return;
  }
  const button = $("#deviceRefresh");
  button.disabled = true;
  button.textContent = "Leyendo…";
  const grid = $("#deviceInfoGrid");
  grid.innerHTML = '<div class="info-placeholder">Leyendo la ficha del Fire TV…</div>';
  try {
    const data = await api("/api/device/info");
    state.deviceInfo = data.device;
    renderDeviceInfo(data.device);
  } catch (err) {
    state.deviceInfo = null;
    grid.innerHTML = "";
    const placeholder = document.createElement("div");
    placeholder.className = "info-placeholder";
    placeholder.textContent = friendlyError(err);
    grid.appendChild(placeholder);
    toast(friendlyError(err), "err");
  } finally {
    button.disabled = false;
    button.textContent = "Actualizar";
  }
}

$("#deviceRefresh").addEventListener("click", loadDeviceInfo);

/* ---------------------------------- red ---------------------------------- */

function netStateLabel(state) {
  if (state === "device") return "Conectado";
  if (state === "unauthorized") return "Sin autorizar";
  if (state === "offline") return "Desconectado";
  return state || "Desconocido";
}

function signalBars(quality) {
  const wrap = document.createElement("div");
  wrap.className = "signal";
  wrap.title = quality == null ? "Sin datos de señal" : `Calidad ${quality} %`;
  const lit = quality == null ? 0 : Math.max(1, Math.ceil(quality / 20));
  for (let i = 1; i <= 5; i += 1) {
    const bar = document.createElement("i");
    if (i <= lit) bar.classList.add("on");
    wrap.appendChild(bar);
  }
  return wrap;
}

function renderNetSaved(records, currentSerial) {
  const list = $("#netSaved");
  list.textContent = "";
  if (!records.length) {
    const empty = document.createElement("p");
    empty.className = "hint";
    empty.textContent = "Todavía no hay dispositivos guardados.";
    list.appendChild(empty);
    return;
  }
  records.forEach((item) => {
    const row = document.createElement("div");
    row.className = `net-saved-row${item.serial === currentSerial ? " current" : ""}`;
    if (item.previous_ip && item.previous_ip !== item.host) {
      row.title = `Última IP conocida: ${item.previous_ip}`;
    }
    const dot = document.createElement("span");
    dot.className = `dot${
      item.online ? " online" : item.state === "unauthorized" ? " warn" : ""
    }`;
    const name = document.createElement("span");
    name.className = "net-saved-name";
    name.textContent = item.display_name || item.name || item.serial;
    const ip = document.createElement("span");
    ip.className = "net-saved-ip";
    ip.textContent = `${item.host}:${item.port}`;
    row.append(dot, name, ip);
    if (item.previous_ip && item.previous_ip !== item.host) {
      const previous = document.createElement("span");
      previous.className = "net-saved-prev";
      previous.textContent = `antes ${item.previous_ip}`;
      row.appendChild(previous);
    }
    const state = document.createElement("span");
    state.className = `net-saved-state${
      item.online ? " online" : item.state === "unauthorized" ? " warn" : ""
    }`;
    state.textContent = item.online
      ? "En línea"
      : item.state === "unauthorized"
        ? "Sin autorizar"
        : "Desconectado";
    row.appendChild(state);
    list.appendChild(row);
  });
}

function showNetBanner(icon, title, text) {
  $("#netBannerIcon").textContent = icon;
  $("#netBannerTitle").textContent = title;
  $("#netBannerText").textContent = text;
  $("#netBanner").classList.remove("hidden");
}

function renderNetNotes(notes) {
  const box = $("#netNotes");
  box.textContent = "";
  (notes || []).forEach((note) => {
    const line = document.createElement("p");
    line.textContent = note;
    box.appendChild(line);
  });
}

function renderNetwork(data) {
  const device = data.device;
  const grid = $("#netGrid");
  const latency = device?.latency || null;
  const wifi = data.wifi || {};
  const ip = data.ip || {};
  const pings = data.pings || {};
  const internet = data.internet || {};

  $("#netBanner").classList.add("hidden");
  $("#netMeta").textContent = device
    ? "Conexión ADB, Wi-Fi e IP del dispositivo seleccionado"
    : "Conecta un Fire TV para ver su red";

  if (!device) {
    $("#netName").textContent = "Sin conectar";
    $("#netAddress").textContent = "Conecta un Fire TV para ver su red.";
    $("#netDot").className = "dot";
    $("#netLatency").textContent = "—";
    $("#netLatency").className = "";
    $("#netSignal").textContent = "—";
    $("#netSignal").className = "";
    $("#netChecked").textContent = "—";
    $("#netChecked").className = "";
    grid.textContent = "";
    const placeholder = document.createElement("div");
    placeholder.className = "info-placeholder";
    placeholder.textContent = "No hay ningún Fire TV conectado.";
    grid.appendChild(placeholder);
    renderNetNotes(data.notes);
    renderNetSaved(data.saved || [], "");
    return;
  }

  $("#netName").textContent = device.name || device.model || device.serial;
  const model = device.model && device.model !== device.name ? device.model : "";
  $("#netAddress").textContent = [
    `${device.host}:${device.port}`,
    model,
    netStateLabel(device.state),
  ]
    .filter(Boolean)
    .join(" · ");
  $("#netDot").className = `dot${
    device.online ? " online" : device.state === "unauthorized" ? " warn" : ""
  }`;

  $("#netLatency").textContent = latency ? `${latency.avg.toFixed(1)} ms` : "—";
  $("#netLatency").className = latency ? "ok" : "warn";
  $("#netLatency").title = latency
    ? `Mínimo ${latency.min} ms · máximo ${latency.max} ms · ${latency.loss} % de pérdida`
    : "El puerto ADB no responde desde este PC";
  $("#netSignal").textContent =
    wifi.quality != null ? `${wifi.quality} %` : "—";
  $("#netSignal").className =
    wifi.quality == null ? "" : wifi.quality >= 60 ? "ok" : "warn";
  $("#netSignal").title =
    wifi.rssi != null ? `${wifi.rssi} dBm` : "Sin datos de señal";
  $("#netChecked").textContent = data.checked
    ? new Date(data.checked * 1000).toLocaleTimeString("es-MX", {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
      })
    : "—";
  $("#netChecked").className = "";

  const connectivity = data.internet?.validated;
  const wifiCard = infoCard(
    "Wi-Fi",
    [
      ["Red", wifi.ssid],
      [
        "Señal",
        wifi.rssi != null ? `${wifi.rssi} dBm (${wifi.quality} %)` : "",
      ],
      ["Banda", wifi.band],
      ["Frecuencia", wifi.frequency_mhz ? `${wifi.frequency_mhz} MHz` : ""],
      ["Velocidad", wifi.link_mbps ? `${wifi.link_mbps} Mbps` : ""],
      ["Seguridad", wifi.security],
      ["Asignación IP", wifi.dhcp],
    ],
    {
      note: wifi.connected
        ? "Datos leídos del propio Fire TV con dumpsys wifi."
        : "Wi-Fi no conectado o no disponible por ADB.",
    }
  );
  if (wifi.quality != null) {
    wifiCard.insertBefore(signalBars(wifi.quality), wifiCard.querySelector(".info-rows"));
  }

  grid.textContent = "";
  grid.append(
    infoCard(
      "Conexión ADB",
      [
        ["Estado", netStateLabel(device.state)],
        ["Dirección", `${device.host}:${device.port}`, true],
        ["Latencia media", latency ? `${latency.avg.toFixed(1)} ms` : "Sin respuesta"],
        ["Mín. / máx.", latency ? `${latency.min} / ${latency.max} ms` : ""],
        ["Pérdida", latency ? `${latency.loss} %` : ""],
        ["Número de serie", device.hardware, true],
      ],
      {
        note: latency
          ? "Medida con 4 conexiones TCP al puerto ADB."
          : "El puerto ADB no responde: revisa la IP o reconecta.",
      }
    ),
    wifiCard,
    infoCard(
      "Dirección IP",
      [
        ["IP", ip.address, true],
        [
          "Máscara",
          ip.netmask ? `${ip.netmask}${ip.prefix != null ? ` (/${ip.prefix})` : ""}` : "",
          true,
        ],
        ["Puerta de enlace", ip.gateway, true],
        ["MAC", ip.mac, true],
        ["Interfaz", ip.interface, true],
        ["Hostname", ip.hostname],
      ],
      { note: "La MAC es la del adaptador Wi-Fi del Fire TV." }
    ),
    infoCard(
      "Desde el Fire TV",
      [
        [
          "Router",
          pings.gateway?.avg != null ? `${pings.gateway.avg.toFixed(1)} ms` : "Sin respuesta",
        ],
        [
          "Pérdida al router",
          pings.gateway?.loss != null ? `${pings.gateway.loss} %` : "",
        ],
        [
          "Internet (8.8.8.8)",
          pings.internet?.avg != null ? `${pings.internet.avg.toFixed(1)} ms` : "Sin respuesta",
        ],
        [
          "Pérdida a internet",
          pings.internet?.loss != null ? `${pings.internet.loss} %` : "",
        ],
        [
          "Internet validado",
          connectivity === true ? "Sí" : connectivity === false ? "No" : "",
        ],
      ],
      { note: "Pings lanzados desde el propio Fire TV, no desde el PC." }
    )
  );

  if (data.ip_change?.reused) {
    showNetBanner(
      "⚠️",
      "Esta IP pertenece a otro Fire TV",
      `La dirección ${data.ip_change.current} respondió con un equipo distinto del guardado. ` +
        "El router pudo reasignarla; pulsa «Buscar de nuevo» para localizar el tuyo."
    );
  } else if (data.ip_change?.changed) {
    showNetBanner(
      "📡",
      `La IP de ${device.name} cambió`,
      `Antes ${data.ip_change.previous}, ahora ${data.ip_change.current}. ` +
        "La lista guardada ya apunta a la dirección nueva."
    );
  }

  renderNetNotes(data.notes);
  renderNetSaved(data.saved || [], device.serial);
}

async function loadNetwork() {
  if (state.networkLoading) return;
  state.networkLoading = true;
  const button = $("#netRefresh");
  button.disabled = true;
  button.textContent = "Comprobando…";
  $("#netMeta").textContent = "Leyendo el estado de red del Fire TV…";
  try {
    const data = await api("/api/network");
    state.network = data;
    renderNetwork(data);
  } catch (err) {
    state.network = null;
    $("#netMeta").textContent = "No se pudo leer el estado de red";
    const grid = $("#netGrid");
    grid.textContent = "";
    const placeholder = document.createElement("div");
    placeholder.className = "info-placeholder";
    placeholder.textContent = friendlyError(err);
    grid.appendChild(placeholder);
    toast(friendlyError(err), "err");
  } finally {
    state.networkLoading = false;
    button.disabled = false;
    button.textContent = "Comprobar ahora";
  }
}

$("#netRefresh").addEventListener("click", loadNetwork);

$("#netReconnect").addEventListener("click", async () => {
  const device = state.network?.device;
  if (!device) {
    toast("Conecta un Fire TV primero", "err");
    return;
  }
  const button = $("#netReconnect");
  button.disabled = true;
  button.textContent = "Reconectando…";
  try {
    const data = await api("/api/network/reconnect", {
      method: "POST",
      body: { serial: device.serial },
    });
    toast(data.message, "ok");
    await refreshStatus();
    await loadNetwork();
  } catch (err) {
    toast(friendlyError(err), "err");
  } finally {
    button.disabled = false;
    button.textContent = "Reconectar ADB";
  }
});

$("#netScan").addEventListener("click", async () => {
  const button = $("#netScan");
  button.disabled = true;
  button.textContent = "Buscando…";
  try {
    const data = await api("/api/network/scan", { method: "POST", body: {} });
    if (data.moved?.length) {
      data.moved.forEach((item) => {
        toast(`${item.name}: ${item.from} → ${item.to}`, "ok");
      });
    } else if (data.found?.length) {
      toast(
        `Encontrados ${data.found.length} Fire TV; no hubo cambios de IP`,
        "ok"
      );
    } else {
      toast("No se encontró ningún Fire TV en la red local", "err");
    }
    await refreshStatus();
    await loadNetwork();
  } catch (err) {
    toast(friendlyError(err), "err");
  } finally {
    button.disabled = false;
    button.textContent = "Buscar de nuevo";
  }
});

/* -------------------------------- consola -------------------------------- */

function consoleLine(text, cls = "") {
  const out = $("#consoleOut");
  const span = document.createElement("span");
  span.className = cls;
  span.textContent = text.endsWith("\n") ? text : `${text}\n`;
  out.appendChild(span);
  out.scrollTop = out.scrollHeight;
}

async function runCommand(command) {
  if (!command.trim()) return;
  consoleLine(`$ ${command}`, "cmd");
  try {
    const data = await api("/api/shell", { method: "POST", body: { command } });
    if (data.output) consoleLine(data.output);
    if (data.code) consoleLine(`[código de salida: ${data.code}]`, "err");
  } catch (err) {
    consoleLine(friendlyError(err), "err");
    toast(friendlyError(err), "err");
  }
}

$("#consoleForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("#consoleInput");
  const command = input.value;
  input.value = "";
  await runCommand(command);
});

$("#quickRow").addEventListener("click", (e) => {
  const chip = e.target.closest("[data-cmd]");
  if (chip) runCommand(chip.dataset.cmd);
});

$("#clearConsole").addEventListener("click", () => {
  $("#consoleOut").innerHTML = '<span class="muted">$ consola limpia</span>';
});

$("#rebootBtn").addEventListener("click", async () => {
  if (!confirm("¿Reiniciar el Fire TV?\n\nLa conexión ADB se perderá unos segundos.")) return;
  try {
    await api("/api/reboot", { method: "POST", body: { confirm: true } });
    toast("Reiniciando el Fire TV…", "ok");
  } catch (err) {
    toast(friendlyError(err), "err");
  }
});

/* ------------------------------- instalar -------------------------------- */

const dropzone = $("#dropzone");
const apkInput = $("#apkInput");

function setFile(file) {
  if (!file) return;
  const match = file.name.toLowerCase().match(/\.(apk|apks|xapk|apkm|zip)$/);
  if (!match) {
    toast("Selecciona un APK, APKS, XAPK, APKM o ZIP", "err");
    return;
  }
  const ext = match[1];
  const split = ext !== "apk";
  state.file = file;
  $("#fileIcon").textContent = ext.toUpperCase();
  $("#fileName").textContent = file.name;
  $("#fileSize").textContent = fmtBytes(file.size);
  $("#fileHint").textContent = split
    ? "Paquete dividido: FireDeck instalará la base y los splits compatibles."
    : "APK único: se instala con adb install.";
  $("#fileCard").classList.remove("hidden");
  $("#installBtn").disabled = false;
  $("#installLog").classList.add("hidden");
  $("#installLog").textContent = "";
}

dropzone.addEventListener("click", () => apkInput.click());
apkInput.addEventListener("change", () => setFile(apkInput.files[0]));

["dragenter", "dragover"].forEach((evt) =>
  dropzone.addEventListener(evt, (e) => {
    e.preventDefault();
    dropzone.classList.add("drag");
  })
);
["dragleave", "drop"].forEach((evt) =>
  dropzone.addEventListener(evt, (e) => {
    e.preventDefault();
    dropzone.classList.remove("drag");
  })
);
dropzone.addEventListener("drop", (e) => {
  const file = e.dataTransfer.files[0];
  if (file) setFile(file);
});

$("#clearFile").addEventListener("click", () => {
  state.file = null;
  apkInput.value = "";
  $("#fileCard").classList.add("hidden");
  $("#fileHint").textContent = "";
  $("#installBtn").disabled = true;
});

$("#installBtn").addEventListener("click", () => {
  if (!state.file || state.installing) return;
  const file = state.file;
  const btn = $("#installBtn");
  const bar = $("#installBar");
  const progress = $("#installProgress");
  const log = $("#installLog");

  state.installing = true;
  btn.disabled = true;
  btn.textContent = "Instalando…";
  progress.classList.remove("hidden");
  log.classList.remove("hidden");
  log.textContent = "Subiendo paquete…";
  bar.style.width = "0%";

  const xhr = new XMLHttpRequest();
  xhr.open("POST", `/api/install?${tabParam()}`);
  xhr.setRequestHeader("X-Filename", encodeURIComponent(file.name));
  xhr.setRequestHeader(TAB_HEADER, tabId);

  xhr.upload.onprogress = (e) => {
    if (e.lengthComputable) {
      const pct = Math.round((e.loaded / e.total) * 100);
      bar.style.width = `${pct}%`;
      log.textContent = `Subiendo paquete… ${pct}%`;
    }
  };

  xhr.upload.onload = () => {
    bar.style.width = "100%";
    log.textContent = "Paquete enviado. Analizando base y splits e instalando (puede tardar)…";
  };

  xhr.onload = () => {
    state.installing = false;
    btn.disabled = false;
    btn.textContent = "Instalar en el Fire TV";
    let data = null;
    try {
      data = JSON.parse(xhr.responseText);
    } catch {
      /* respuesta no JSON */
    }
    if (xhr.status >= 200 && xhr.status < 300 && data && data.ok) {
      log.textContent = data.output || "Instalación completada.";
      toast("Paquete instalado", "ok");
      loadApps();
    } else {
      log.textContent = (data && data.error) || `Error ${xhr.status}`;
      toast((data && data.error) || "La instalación falló", "err");
    }
    setTimeout(() => progress.classList.add("hidden"), 1200);
  };

  xhr.onerror = () => {
    state.installing = false;
    btn.disabled = false;
    btn.textContent = "Instalar en el Fire TV";
    log.textContent = "No se pudo conectar con FireDeck.";
    toast("No se pudo subir el paquete", "err");
  };

  xhr.send(file);
});

/* --------------------------- descargar e instalar ------------------------ */

const dlState = state.download;

function dlClearPreview() {
  dlState.info = null;
  $("#dlPreview").classList.add("hidden");
  $("#dlInstallBtn").disabled = true;
}

function dlShowPreview(info) {
  dlState.info = info;
  $("#dlIcon").textContent = (info.ext || "apk").toUpperCase();
  $("#dlName").textContent = info.filename || "paquete";
  const bits = [info.size ? fmtBytes(info.size) : "tamaño desconocido"];
  if (info.source === "code") bits.push(`código ${info.code}`);
  $("#dlMeta").textContent = bits.join(" · ");
  const server = [info.service, info.host].filter(Boolean).join(" · ");
  $("#dlHost").textContent = server ? `Servidor: ${server}` : "";
  $("#dlPreview").classList.remove("hidden");
  $("#dlInstallBtn").disabled = false;
  $("#dlLog").classList.add("hidden");
  $("#dlLog").textContent = "";
}

async function resolveDownload() {
  const input = $("#dlInput").value.trim();
  if (!input) {
    toast("Escribe un código o una URL", "err");
    $("#dlInput").focus();
    return;
  }
  const btn = $("#dlResolveBtn");
  btn.disabled = true;
  btn.textContent = "Resolviendo…";
  dlClearPreview();
  try {
    const data = await api("/api/downloader/resolve", {
      method: "POST",
      body: { input },
    });
    dlShowPreview(data.info);
    toast(
      data.info.source === "code" ? "Código resuelto" : "Enlace comprobado",
      "ok"
    );
  } catch (err) {
    toast(friendlyError(err), "err");
  } finally {
    btn.disabled = false;
    btn.textContent = "Resolver";
  }
}

$("#dlForm").addEventListener("submit", (e) => {
  e.preventDefault();
  resolveDownload();
});

function dlStopPolling() {
  if (dlState.timer) {
    clearInterval(dlState.timer);
    dlState.timer = null;
  }
}

function dlSetProgress(percent) {
  const value = Number.isFinite(percent) ? percent : 0;
  $("#dlBar").style.width = `${Math.max(0, Math.min(100, value))}%`;
}

function dlRenderJob(job) {
  dlSetProgress(job.percent);
  let text = job.message || "";
  if (job.status === "downloading") {
    const loaded = fmtBytes(job.loaded || 0);
    text = job.total
      ? `Descargando «${job.filename}»… ${loaded} de ${fmtBytes(job.total)} (${job.percent ?? 0}%)`
      : `Descargando «${job.filename}»… ${loaded}`;
  } else if (job.status === "installing") {
    text = "Descargado. Instalando en el Fire TV (puede tardar)…";
  } else if (job.status === "done") {
    text = job.output || "Instalación completada.";
  } else if (job.status === "error") {
    text = job.error || "La instalación falló.";
  } else if (job.status === "cancelled") {
    text = "Descarga cancelada.";
  }
  $("#dlLog").classList.remove("hidden");
  $("#dlLog").textContent = text;
  $("#dlCancelBtn").classList.toggle(
    "hidden",
    !["queued", "resolving", "downloading"].includes(job.status)
  );
}

function dlFinishJob() {
  dlStopPolling();
  dlState.jobId = "";
  dlState.busy = false;
  const btn = $("#dlInstallBtn");
  btn.disabled = !dlState.info;
  btn.textContent = "Descargar e instalar";
  $("#dlCancelBtn").classList.add("hidden");
}

async function dlPoll() {
  if (!dlState.jobId) return;
  try {
    const data = await api(
      `/api/downloader/job?id=${encodeURIComponent(dlState.jobId)}&${tabParam()}`
    );
    const job = data.job;
    dlRenderJob(job);
    if (["done", "error", "cancelled"].includes(job.status)) {
      dlFinishJob();
      if (job.status === "done") {
        toast("Paquete instalado", "ok");
        loadApps();
      } else if (job.status === "error") {
        toast(job.error || "La instalación falló", "err");
      }
      loadDownloadHistory();
    }
  } catch (err) {
    dlFinishJob();
    toast(friendlyError(err), "err");
  }
}

$("#dlInstallBtn").addEventListener("click", async () => {
  if (dlState.busy || !dlState.info) return;
  const input = $("#dlInput").value.trim() || dlState.info.input;
  if (!input) return;
  const btn = $("#dlInstallBtn");
  dlState.busy = true;
  btn.disabled = true;
  btn.textContent = "Descargando…";
  $("#dlLog").classList.remove("hidden");
  $("#dlLog").textContent = "Preparando la descarga…";
  $("#dlCancelBtn").classList.remove("hidden");
  $("#dlCancelBtn").disabled = false;
  dlSetProgress(0);
  try {
    const data = await api("/api/downloader/install", {
      method: "POST",
      body: { input },
    });
    dlState.jobId = data.job.id;
    dlRenderJob(data.job);
    dlStopPolling();
    dlState.timer = setInterval(dlPoll, 700);
  } catch (err) {
    dlState.busy = false;
    btn.disabled = false;
    btn.textContent = "Descargar e instalar";
    $("#dlCancelBtn").classList.add("hidden");
    $("#dlLog").textContent = friendlyError(err);
    toast(friendlyError(err), "err");
  }
});

$("#dlCancelBtn").addEventListener("click", async () => {
  if (!dlState.jobId) return;
  const btn = $("#dlCancelBtn");
  btn.disabled = true;
  btn.textContent = "Cancelando…";
  try {
    const data = await api("/api/downloader/cancel", {
      method: "POST",
      body: { id: dlState.jobId },
    });
    dlRenderJob(data.job);
  } catch (err) {
    toast(friendlyError(err), "err");
  } finally {
    btn.disabled = false;
    btn.textContent = "Cancelar";
  }
});

function renderDownloadHistory(items) {
  const box = $("#dlHistory");
  box.innerHTML = "";
  if (!items.length) return;
  const label = document.createElement("span");
  label.className = "muted";
  label.textContent = "Recientes:";
  box.appendChild(label);
  for (const item of items) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = `chip${item.ok === false ? " failed" : ""}`;
    chip.title = item.input;
    const name = item.source === "code" ? `Código ${item.input}` : item.filename || item.input;
    chip.textContent = item.package ? `${name} · ${item.package}` : name;
    chip.addEventListener("click", () => {
      $("#dlInput").value = item.input;
      resolveDownload();
    });
    box.appendChild(chip);
  }
  const clear = document.createElement("button");
  clear.type = "button";
  clear.className = "chip";
  clear.textContent = "Limpiar";
  clear.addEventListener("click", async () => {
    try {
      await api("/api/downloader/history/clear", { method: "POST", body: {} });
      renderDownloadHistory([]);
    } catch (err) {
      toast(friendlyError(err), "err");
    }
  });
  box.appendChild(clear);
}

async function loadDownloadHistory() {
  try {
    const data = await api(`/api/downloader/history?${tabParam()}`);
    renderDownloadHistory(data.history || []);
  } catch {
    /* el historial es opcional */
  }
}

/* --------------------------------- ayuda --------------------------------- */

$("#helpBtn").addEventListener("click", () => $("#helpModal").showModal());
$("#helpClose").addEventListener("click", () => $("#helpModal").close());
$("#helpModal").addEventListener("click", (e) => {
  if (e.target === $("#helpModal")) $("#helpModal").close();
});

/* --------------------------------- inicio -------------------------------- */

const initialView = location.hash.replace("#", "");
if (["control", "apps", "system", "files", "device", "network", "install", "storage", "console"].includes(initialView)) {
  const tab = $(`.tab[data-view="${initialView}"]`);
  if (tab) tab.click();
}

refreshStatus().then((data) => {
  if (!data?.device) return;
  if (initialView === "apps" && state.apps.length === 0) loadApps();
  if (initialView === "system" && state.system.apps.length === 0) loadSystemApps();
  if (initialView === "files") loadFiles(state.files.path);
  if (initialView === "device" && !state.deviceInfo) loadDeviceInfo();
  if (initialView === "storage") loadStorage();
});
refreshRecording();
loadDownloadHistory();
setInterval(() => refreshStatus(), 5000);
