// gremote dashboard frontend (no build step).

let devices = [];

function $(sel) { return document.querySelector(sel); }

async function api(path, options) {
  const res = await fetch(path, options || {});
  if (res.status === 401) {
    showLogin();
    throw new Error("Not authenticated");
  }
  let data = {};
  try { data = await res.json(); } catch (e) { /* non-JSON */ }
  if (!res.ok) {
    throw new Error(data.detail || ("HTTP " + res.status));
  }
  return data;
}

// ---- auth -------------------------------------------------------------

async function checkSession() {
  try {
    await api("/api/auth/me");
    showDashboard();
    await loadDevices();
  } catch (e) {
    showLogin();
  }
}

async function login(e) {
  e.preventDefault();
  try {
    await api("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: $("#password").value })
    });
    showDashboard();
    await loadDevices();
  } catch (err) {
    $("#login-error").textContent = err.message;
  }
}

async function logout() {
  try { await api("/api/auth/logout", { method: "POST" }); } catch (e) {}
  showLogin();
}

function showLogin() {
  $("#login").style.display = "flex";
  $("#dashboard").style.display = "none";
}

function showDashboard() {
  $("#login").style.display = "none";
  $("#dashboard").style.display = "block";
  $("#login-error").textContent = "";
  $("#password").value = "";
}

// ---- devices ------------------------------------------------------------

async function loadTypes() {
  const data = await api("/api/devices/types");
  const sel = $("#device-type");
  sel.innerHTML = "";
  data.types.forEach(function (t) {
    const opt = document.createElement("option");
    opt.value = t.type;
    opt.textContent = t.label;
    sel.appendChild(opt);
  });
}

async function loadDevices() {
  const data = await api("/api/devices");
  devices = data.devices;
  renderDevices();
}

function deviceById(id) {
  for (let i = 0; i < devices.length; i++) {
    if (devices[i].id === id) return devices[i];
  }
  return null;
}

function renderDevices() {
  const list = $("#device-list");
  list.innerHTML = "";
  if (!devices.length) {
    list.innerHTML = "<p class='empty'>No devices yet. Add your first device above.</p>";
    return;
  }
  devices.forEach(function (d) {
    const card = buildCard(d);
    list.appendChild(card);
    refreshStatus(d, card);
    if (d.type === "sony_bravia") {
      loadApps(d, card.querySelector(".app-select"));
    }
  });
}

function buildCard(d) {
  const card = document.createElement("div");
  card.className = "card";
  card.id = "card-" + d.id;

  const head = document.createElement("div");
  head.className = "card-head";
  const name = document.createElement("h3");
  name.textContent = d.name;
  const sub = document.createElement("div");
  sub.className = "sub";
  sub.textContent = d.type + " · " + d.host;
  head.appendChild(name);
  head.appendChild(sub);

  const status = document.createElement("div");
  status.className = "status";
  status.textContent = "status: …";

  const controls = document.createElement("div");
  controls.className = "controls";
  controls.appendChild(btn("Wake", function () { run(d.id, "wake"); }));
  controls.appendChild(btn("Power", function () { run(d.id, "toggle_power"); }));

  if (d.type === "sony_bravia") {
    controls.appendChild(sonyControls(d));
  }

  const del = btn("Delete", function () { remove(d.id); }, "danger");
  del.style.marginLeft = "auto";
  controls.appendChild(del);

  card.appendChild(head);
  card.appendChild(status);
  card.appendChild(controls);
  return card;
}

function sonyControls(d) {
  const wrap = document.createElement("div");
  wrap.className = "sony-controls";

  wrap.appendChild(btn("Mute", function () { run(d.id, "toggle_mute"); }));

  // volume slider
  const vol = document.createElement("div");
  vol.className = "vol-wrap";
  const slider = document.createElement("input");
  slider.type = "range";
  slider.min = "0";
  slider.max = "100";
  slider.value = "0";
  slider.className = "vol-slider";
  const out = document.createElement("output");
  out.textContent = "—";
  slider.addEventListener("input", function () { out.textContent = slider.value; });
  slider.addEventListener("change", function () {
    run(d.id, "set_volume", { volume: Number(slider.value) });
  });
  vol.appendChild(slider);
  vol.appendChild(out);
  wrap.appendChild(vol);

  // input select
  const inputSel = document.createElement("select");
  inputSel.className = "input-select";
  const inputs = [["tv", "TV"], ["hdmi1", "HDMI 1"], ["hdmi2", "HDMI 2"],
                  ["hdmi3", "HDMI 3"], ["hdmi4", "HDMI 4"]];
  inputs.forEach(function (pair) {
    const o = document.createElement("option");
    o.value = pair[0];
    o.textContent = pair[1];
    inputSel.appendChild(o);
  });
  inputSel.addEventListener("change", function () {
    run(d.id, "set_input", { input: inputSel.value });
  });
  wrap.appendChild(inputSel);

  // apps select + launch
  const appSel = document.createElement("select");
  appSel.className = "app-select";
  const opt = document.createElement("option");
  opt.value = "";
  opt.textContent = "Apps…";
  appSel.appendChild(opt);
  wrap.appendChild(appSel);
  wrap.appendChild(btn("Launch", function () {
    if (appSel.value) run(d.id, "launch_app", { uri: appSel.value });
  }));

  return wrap;
}

function btn(label, onClick, cls) {
  const b = document.createElement("button");
  b.type = "button";
  b.textContent = label;
  b.className = cls || "";
  b.addEventListener("click", onClick);
  return b;
}

async function refreshStatus(d, card) {
  const statusEl = card.querySelector(".status");
  statusEl.textContent = "status: …";
  try {
    const data = await api("/api/devices/" + d.id + "/status");
    statusEl.textContent = formatStatus(data.status);
    const slider = card.querySelector(".vol-slider");
    const v = data.status && data.status.volume;
    if (slider && v !== null && v !== undefined) {
      slider.value = v;
      const out = slider.parentElement.querySelector("output");
      if (out) out.textContent = v;
    }
  } catch (err) {
    statusEl.textContent = "status: error (" + err.message + ")";
  }
}

function formatStatus(s) {
  if (!s) return "status: unknown";
  const parts = [];
  if (s.power) parts.push("power: " + s.power);
  if (s.volume !== undefined && s.volume !== null) parts.push("volume: " + s.volume);
  if (s.mute) parts.push("muted");
  return "status: " + (parts.join(" · ") || "unknown");
}

async function loadApps(d, selectEl) {
  if (!selectEl) return;
  try {
    const data = await api("/api/devices/" + d.id + "/action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "apps" })
    });
    const apps = (data.result && data.result.apps) || [];
    selectEl.innerHTML = "";
    const none = document.createElement("option");
    none.value = "";
    none.textContent = "Launch app…";
    selectEl.appendChild(none);
    apps.forEach(function (app) {
      if (!app.uri) return;
      const o = document.createElement("option");
      o.value = app.uri;
      o.textContent = app.title || app.uri;
      selectEl.appendChild(o);
    });
  } catch (err) {
    const first = selectEl.querySelector("option");
    if (first) first.textContent = "Apps unavailable";
  }
}

async function run(id, name, params) {
  try {
    await api("/api/devices/" + id + "/action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: name, params: params || {} })
    });
  } catch (err) {
    alert(err.message);
  }
  const d = deviceById(id);
  const card = $("#card-" + id);
  if (d && card) refreshStatus(d, card);
}

async function remove(id) {
  if (!confirm("Delete this device?")) return;
  try {
    await api("/api/devices/" + id, { method: "DELETE" });
    await loadDevices();
  } catch (err) {
    alert(err.message);
  }
}

async function addDevice(e) {
  e.preventDefault();
  const body = {
    name: $("#device-name").value,
    type: $("#device-type").value,
    host: $("#device-host").value,
    mac: $("#device-mac").value || null
  };
  const psk = $("#device-psk").value;
  if (psk) body.config = { psk: psk };
  try {
    await api("/api/devices", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
    $("#device-name").value = "";
    $("#device-host").value = "";
    $("#device-mac").value = "";
    $("#device-psk").value = "";
    await loadDevices();
  } catch (err) {
    alert(err.message);
  }
}

// ---- discovery -----------------------------------------------------------

async function scanNetwork() {
  const btn = $("#scan-network");
  const results = $("#discovery-results");
  btn.disabled = true;
  btn.textContent = "Scanning…";
  results.innerHTML = "<p class='muted'>Scanning the network for Sony TVs…</p>";
  try {
    const data = await api("/api/discovery/scan", { method: "POST" });
    renderDiscovery(data.devices || []);
  } catch (err) {
    results.innerHTML = "<p class='error'>Scan failed: " + err.message + "</p>";
  } finally {
    btn.disabled = false;
    btn.textContent = "Scan network";
  }
}

function renderDiscovery(devs) {
  const results = $("#discovery-results");
  results.innerHTML = "";
  if (!devs.length) {
    results.innerHTML = "<p class='muted'>No Sony TVs found. Check the TV is on the same network with IP control enabled, then scan again.</p>";
    return;
  }
  const list = document.createElement("div");
  list.className = "discovery-list";
  devs.forEach(function (dev) {
    const row = document.createElement("div");
    row.className = "discovery-row";
    const label = document.createElement("span");
    label.className = "discovery-label";
    label.textContent = (dev.friendly_name || dev.host) + " — " + dev.host;
    if (dev.model) label.textContent += " (" + dev.model + ")";
    row.appendChild(label);
    row.appendChild(btn("Use", function () { useDiscovery(dev); }));
    list.appendChild(row);
  });
  results.appendChild(list);
}

function useDiscovery(dev) {
  $("#device-name").value = dev.friendly_name || ("TV " + dev.host);
  $("#device-type").value = "sony_bravia";
  $("#device-host").value = dev.host;
  $("#device-mac").value = "";
  $("#device-psk").value = "";
  $("#discovery-results").innerHTML =
    "<p class='muted'>Filled in " + dev.host + ". Enter the PSK (and MAC if you know it), then click Add.</p>";
  $("#device-psk").focus();
}

// ---- wire up ------------------------------------------------------------

document.addEventListener("DOMContentLoaded", function () {
  $("#login-form").addEventListener("submit", login);
  $("#logout").addEventListener("click", logout);
  $("#add-form").addEventListener("submit", addDevice);
  $("#refresh-all").addEventListener("click", function () { loadDevices(); });
  $("#scan-network").addEventListener("click", scanNetwork);
  loadTypes();
  checkSession();
});
