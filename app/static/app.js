'use strict';
// gremote dashboard frontend (no build step).

const ALL = 'all';
const $ = (sel) => document.querySelector(sel);
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* ignore */ } },
};

let devices = [];
let selected = store.get('selectedDevice') || null;
const statusCache = {};   // id -> last status dict
const appsCache = {};     // id -> apps from the TV
const commandsCache = {}; // id -> IRCC command names the TV supports
const commandsRequested = new Set();
const inputsCache = {};   // id -> inputs the TV reports [{uri, title, connected}]
const inputsRequested = new Set();

// ------------------------------------------------------------------ api --
class AuthError extends Error {}

async function api(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401 && !url.startsWith('/api/auth/login')) {
    showLogin();
    throw new AuthError('Not signed in');
  }
  let data = {};
  try { data = await res.json(); } catch { /* empty */ }
  if (!res.ok) throw new Error(data.detail || `Request failed (${res.status})`);
  return data;
}

let toastTimer;
function toast(msg, isError = false, ms = 3200) {
  const el = $('#toast');
  el.textContent = msg;
  el.classList.toggle('error', isError);
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), ms);
}

function buzz() { if (navigator.vibrate) navigator.vibrate(8); }

// ----------------------------------------------------------------- auth --
function showLogin() {
  $('#login').hidden = false;
  $('#app').hidden = true;
  if ($('#settings').open) $('#settings').close();
}

async function showApp() {
  $('#login').hidden = true;
  $('#app').hidden = false;
  $('#login-error').textContent = '';
  $('#password').value = '';
  await loadTypes();
  await loadDevices();
}

async function login(e) {
  e.preventDefault();
  try {
    await api('POST', '/api/auth/login', { password: $('#password').value });
    await showApp();
  } catch (err) {
    $('#login-error').textContent = err.message;
  }
}

async function logout() {
  try { await api('POST', '/api/auth/logout'); } catch { /* ignore */ }
  showLogin();
}

// -------------------------------------------------------------- devices --
async function loadTypes() {
  try {
    const data = await api('GET', '/api/devices/types');
    const sel = $('#deviceType');
    sel.innerHTML = '';
    data.types.forEach((t) => {
      const o = document.createElement('option');
      o.value = t.type;
      o.textContent = t.label;
      sel.appendChild(o);
    });
  } catch { /* shown after login */ }
}

async function loadDevices() {
  const data = await api('GET', '/api/devices');
  devices = data.devices;
  if (selected !== ALL && !devices.some((d) => d.id === selected)) selected = devices[0] ? devices[0].id : null;
  renderTabs();
  renderDeviceList();
  renderRemote();
  refreshStatus();
  if (!devices.length) openSettings();
}

const currentDevice = () => devices.find((d) => d.id === selected);
const targets = () => (selected === ALL ? devices : devices.filter((d) => d.id === selected));

function select(id) {
  selected = id;
  store.set('selectedDevice', id || '');
  renderTabs();
  renderRemote();
  refreshStatus();
}

function dotClass(status) {
  if (!status) return 'dot';
  if (status.power === 'active') return 'dot on';
  if (status.power === 'standby') return 'dot standby';
  return 'dot off';
}

function renderTabs() {
  const tabs = $('#tabs');
  tabs.innerHTML = '';
  const add = (id, label, status) => {
    const b = document.createElement('button');
    b.className = 'tab';
    b.setAttribute('role', 'tab');
    b.setAttribute('aria-selected', String(id === selected));
    const dot = document.createElement('span');
    dot.className = status === null ? '' : dotClass(status);
    if (status !== null) b.appendChild(dot);
    b.appendChild(document.createTextNode(label));
    b.addEventListener('click', () => select(id));
    tabs.appendChild(b);
  };
  devices.forEach((d) => add(d.id, d.name, statusCache[d.id] || undefined));
  if (devices.length > 1) add(ALL, 'All', null);
}

function renderRemote() {
  $('#remote').classList.toggle('disabled', !selected);
  renderApps();
  renderCommands();
  renderInputs();
  applyPagesSupport();
  loadCommands();
  loadInputs();
  renderScreenReader();
  checkScreenReader();
  autoLoadApps();
  if ($('#tvSettingsPanel').open || $('#allCommandsPanel').open) autoScanPages();
}

// --------------------------------------------------------------- status --
async function refreshStatus() {
  await Promise.all(devices.map(async (d) => {
    try { statusCache[d.id] = (await api('GET', `/api/devices/${d.id}/status`)).status; }
    catch (err) { if (err instanceof AuthError) return; statusCache[d.id] = null; }
  }));
  renderTabs();
  renderStatusBar();
}

function renderStatusBar() {
  const bar = $('#statusBar');
  const d = currentDevice();
  if (selected === ALL) { bar.textContent = `Controlling all ${devices.length} devices`; return; }
  if (!d) { bar.textContent = 'No devices yet. Open settings to add one.'; return; }
  const s = statusCache[d.id];
  if (!s) { bar.textContent = `${d.host} · unreachable`; return; }
  const parts = [d.host, `power: ${s.power}`];
  if (s.volume !== null && s.volume !== undefined) {
    parts.push(`volume ${s.volume}`);
    $('#volSlider').value = s.volume;
    $('#volOut').textContent = s.volume;
  }
  if (s.mute) parts.push('muted');
  bar.textContent = parts.join(' · ');
}

// ------------------------------------------------------------- commands --
let lastErrorAt = 0;

/** Run an action on the selected device(s). */
async function run(action, params, { quiet = false } = {}) {
  const list = targets();
  if (!list.length) { toast('Add a device first', true); openSettings(); return null; }
  const results = await Promise.allSettled(list.map((d) =>
    api('POST', `/api/devices/${d.id}/action`, { action, params: params || {} })));
  const failed = results
    .map((r, i) => (r.status === 'rejected' && !(r.reason instanceof AuthError) ? `${list[i].name}: ${r.reason.message}` : null))
    .filter(Boolean);
  if (failed.length && (!quiet || Date.now() - lastErrorAt > 2500)) toast(failed.join('\n') + '\n(see Debug log)', true, 5000);
  if (failed.length) lastErrorAt = Date.now();
  if (['toggle_power', 'power_on', 'power_off', 'toggle_mute', 'set_volume'].includes(action)) {
    setTimeout(refreshStatus, 1500);
  }
  const ok = results.find((r) => r.status === 'fulfilled');
  return ok ? ok.value.result : null;
}

const ircc = (code, opts) => run('ircc', { code }, opts);

async function powerOn() {
  const r = await run('power_on', null, { quiet: true });
  if (r === null) run('wake');   // TV in deep standby: fall back to Wake-on-LAN
}

// ------------------------------------------------ tap / hold detection --
// Nothing is sent on touch-down; a tap fires on release. Moving the finger
// (scrolling) cancels the press. Holding still starts a repeat.
const MOVE_SLOP = 10;
const HOLD_DELAY = 450;
const REPEAT_EVERY = 160;

function onTap(el, fn, { repeat } = {}) {
  let g = null;
  const reset = () => {
    if (!g) return;
    clearTimeout(g.holdTimer);
    clearInterval(g.repeatTimer);
    el.classList.remove('pressed');
    g = null;
  };
  el.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    reset();
    g = { x: e.clientX, y: e.clientY, held: false };
    el.classList.add('pressed');
    if (repeat) {
      g.holdTimer = setTimeout(() => {
        if (!g) return;
        g.held = true;
        buzz();
        repeat();
        g.repeatTimer = setInterval(repeat, REPEAT_EVERY);
      }, HOLD_DELAY);
    }
  });
  el.addEventListener('pointermove', (e) => {
    if (g && !g.held && Math.hypot(e.clientX - g.x, e.clientY - g.y) > MOVE_SLOP) reset();
  });
  el.addEventListener('pointerup', () => {
    if (!g) return;
    const wasHeld = g.held;
    reset();
    if (!wasHeld) { buzz(); fn(); }
  });
  ['pointercancel', 'pointerleave'].forEach((ev) => el.addEventListener(ev, reset));
  el.addEventListener('contextmenu', (e) => e.preventDefault());
}

function wireRemote() {
  document.querySelectorAll('#remote [data-ircc]').forEach((el) => {
    const code = el.dataset.ircc;
    onTap(el, () => ircc(code), el.hasAttribute('data-repeat') ? { repeat: () => ircc(code, { quiet: true }) } : {});
  });
  document.querySelectorAll('#remote [data-ircc-match]').forEach((el) => {
    onTap(el, () => { if (el.dataset.resolved) ircc(el.dataset.resolved); });
  });
  document.querySelectorAll('#remote [data-action]').forEach((el) => {
    const action = el.dataset.action;
    onTap(el, () => (action === 'power_on' ? powerOn() : run(action)));
  });
  // Input buttons are rebuilt per TV (renderInputs), so listen on their container.
  const inputsRow = $('#inputButtons');
  defaultInputsHtml = inputsRow.innerHTML;
  inputsRow.addEventListener('click', (e) => {
    const el = e.target.closest('[data-uri], [data-input]');
    if (!el) return;
    buzz();
    run('set_input', el.dataset.uri ? { uri: el.dataset.uri } : { input: el.dataset.input });
  });

  let volTimer;
  $('#volSlider').addEventListener('input', (e) => {
    $('#volOut').textContent = e.target.value;
    clearTimeout(volTimer);
    volTimer = setTimeout(() => run('set_volume', { volume: Number(e.target.value) }), 250);
  });

  $('#keyForm').addEventListener('submit', (e) => {
    e.preventDefault();
    const v = $('#keyInput').value.trim();
    if (v) { ircc(v); $('#keyInput').value = ''; }
  });

  $('#loadApps').addEventListener('click', () => loadApps());
}

// ----------------------------------------------------------------- apps --
function renderApps() {
  const box = $('#apps');
  box.innerHTML = '';
  const d = currentDevice();
  const apps = (d && appsCache[d.id]) || [];
  if (!apps.length) {
    const p = document.createElement('p');
    p.className = 'empty';
    p.textContent = selected === ALL ? 'Select a single device to see its apps.'
      : d && appsLoading.has(d.id) ? 'Loading apps from the TV…'
        : 'No apps loaded yet (they load when the TV is on).';
    box.appendChild(p);
    return;
  }
  apps.forEach((app) => {
    if (!app.uri) return;
    const b = document.createElement('button');
    b.className = 'app';
    b.textContent = app.title || app.uri;
    onTap(b, () => run('launch_app', { uri: app.uri }));
    box.appendChild(b);
  });
}

// Apps load by themselves when a TV is selected or added (and again on the next status
// check if the TV was off), and are remembered in this browser so they show at once next time.
const appsLoading = new Set();
const appsFresh = new Set();     // loaded from the TV during this visit

async function loadApps({ quiet = false } = {}) {
  const d = currentDevice();
  if (!d || selected === ALL) { if (!quiet) toast('Select a single device first', true); return; }
  if (appsLoading.has(d.id)) return;
  appsLoading.add(d.id);
  renderApps();
  try {
    const data = await api('POST', `/api/devices/${d.id}/action`, { action: 'apps' });
    const apps = (data.result && data.result.apps) || [];
    if (apps.length || !quiet) appsCache[d.id] = apps;
    if (apps.length) {
      appsFresh.add(d.id);
      store.set(`apps:${d.id}`, JSON.stringify(apps.map(({ title, uri }) => ({ title, uri }))));
    }
    if (!apps.length && !quiet) toast('The TV returned no apps', true);
  } catch (err) {
    if (!quiet && !(err instanceof AuthError)) toast(err.message, true);
  } finally {
    appsLoading.delete(d.id);
    if (currentDevice() && currentDevice().id === d.id) renderApps();
  }
}

function autoLoadApps() {
  const d = currentDevice();
  if (!d || selected === ALL || d.type !== 'sony_bravia' || appsFresh.has(d.id)) return;
  if (!appsCache[d.id]) {
    try { appsCache[d.id] = JSON.parse(store.get(`apps:${d.id}`) || 'null') || undefined; } catch { /* ignore */ }
  }
  loadApps({ quiet: true });
}

// ------------------------------------------------------------- settings --
function openSettings() { if (!$('#settings').open) $('#settings').showModal(); }

function renderDeviceList() {
  const list = $('#deviceList');
  list.innerHTML = '';
  if (!devices.length) {
    list.innerHTML = '<p class="empty">No devices yet.</p>';
    return;
  }
  devices.forEach((d) => {
    const card = document.createElement('div');
    card.className = 'tv-card';
    const head = document.createElement('div');
    head.className = 'head';
    const name = document.createElement('strong');
    name.textContent = d.name;
    const badge = document.createElement('span');
    badge.className = 'badge';
    badge.textContent = d.type;
    const pencil = document.createElement('button');
    pencil.className = 'icon-btn small';
    pencil.type = 'button';
    pencil.title = `Edit ${d.name}`;
    pencil.setAttribute('aria-label', `Edit ${d.name}`);
    pencil.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>';
    pencil.addEventListener('click', () => card.replaceWith(buildEditForm(d)));
    head.append(name, badge, pencil);
    const meta = document.createElement('div');
    meta.className = 'meta';
    meta.textContent = d.host + (d.mac ? ` · ${d.mac}` : ' · no MAC (Wake-on-LAN off)') +
      (d.has_psk ? ' · PSK set' : ' · no PSK');
    const actions = document.createElement('div');
    actions.className = 'actions';
    const del = document.createElement('button');
    del.className = 'btn small danger';
    del.type = 'button';
    del.textContent = 'Delete';
    del.addEventListener('click', async () => {
      if (!confirm(`Delete ${d.name}?`)) return;
      try { await api('DELETE', `/api/devices/${d.id}`); await loadDevices(); }
      catch (err) { toast(err.message, true); }
    });
    const macBtn = document.createElement("button");
    macBtn.className = "btn small";
    macBtn.type = "button";
    macBtn.textContent = d.mac ? "Refresh MAC from TV" : "Get MAC from TV";
    macBtn.addEventListener("click", async () => {
      macBtn.disabled = true;
      try {
        const { device } = await api("POST", `/api/devices/${d.id}/fetch-mac`);
        toast(`MAC: ${device.mac}`);
        await loadDevices();
      } catch (err) { if (!(err instanceof AuthError)) toast(err.message + " (see Debug log)", true, 5000); }
      finally { macBtn.disabled = false; }
    });
    actions.append(macBtn, del);
    card.append(head, meta, actions);
    list.appendChild(card);
  });
}

function buildEditForm(d) {
  const form = document.createElement('form');
  form.className = 'tv-card edit-form';
  const field = (name, value, placeholder, type = 'text') => {
    const label = document.createElement('label');
    label.className = 'field';
    const span = document.createElement('span');
    span.textContent = placeholder;
    const input = document.createElement('input');
    input.name = name;
    input.type = type;
    input.value = value || '';
    input.autocomplete = type === 'password' ? 'new-password' : 'off';
    label.append(span, input);
    return { label, input };
  };
  const nameF = field('name', d.name, 'Name');
  const hostF = field('host', d.host, 'IP address');
  const macF = field('mac', d.mac, 'MAC (Wake-on-LAN)');
  const pskF = field('psk', '', d.has_psk ? 'Pre-Shared Key (leave blank to keep the current one)' : 'Pre-Shared Key', 'password');
  nameF.input.required = true;
  hostF.input.required = true;

  const actions = document.createElement('div');
  actions.className = 'actions';
  const save = document.createElement('button');
  save.className = 'btn small primary';
  save.type = 'submit';
  save.textContent = 'Save';
  const cancel = document.createElement('button');
  cancel.className = 'btn small';
  cancel.type = 'button';
  cancel.textContent = 'Cancel';
  cancel.addEventListener('click', renderDeviceList);
  actions.append(save, cancel);

  form.append(nameF.label, hostF.label, macF.label, pskF.label, actions);
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    save.disabled = true;
    const body = {
      name: nameF.input.value,
      type: d.type,
      host: hostF.input.value,
      mac: macF.input.value || null,
      config: pskF.input.value ? { psk: pskF.input.value } : {},
    };
    try {
      await api('PUT', `/api/devices/${d.id}`, body);
      delete commandsCache[d.id];
      commandsRequested.delete(d.id);
      delete inputsCache[d.id];
      inputsRequested.delete(d.id);
      toast(`Saved ${body.name}`);
      await loadDevices();
    } catch (err) {
      if (!(err instanceof AuthError)) toast(err.message, true);
      save.disabled = false;
    }
  });
  return form;
}

async function addDevice(e) {
  e.preventDefault();
  const f = new FormData(e.target);
  const body = {
    name: f.get('name'),
    type: f.get('type'),
    host: f.get('host'),
    mac: f.get('mac') || null,
  };
  if (f.get('psk')) body.config = { psk: f.get('psk') };
  try {
    const { device } = await api('POST', '/api/devices', body);
    e.target.reset();
    $('#discovered').innerHTML = '';
    selected = device.id;
    store.set('selectedDevice', device.id);
    await loadDevices();
    toast(device.mac ? `Added ${device.name} (MAC ${device.mac})` : `Added ${device.name}. MAC not detected; see Debug log`);
    if (device.type === 'sony_bravia') scanPages(currentDevice() || device, { quiet: true });
  } catch (err) {
    toast(err.message, true);
  }
}

async function scanNetwork() {
  const btn = $('#scanBtn');
  const out = $('#discovered');
  btn.disabled = true;
  btn.textContent = 'Scanning…';
  out.innerHTML = '';
  try {
    const { devices: found } = await api('POST', '/api/discovery/scan');
    if (!found.length) {
      out.innerHTML = '<p class="empty">No Sony TVs found. Check IP control is enabled and the server is on the same network, then try again.</p>';
    }
    found.forEach((dev) => {
      const card = document.createElement('div');
      card.className = 'tv-card';
      const head = document.createElement('div');
      head.className = 'head';
      const name = document.createElement('strong');
      name.textContent = dev.friendly_name || dev.host;
      const use = document.createElement('button');
      use.className = 'btn small primary';
      use.type = 'button';
      use.textContent = 'Use';
      use.addEventListener('click', () => {
        const f = $('#addForm');
        f.elements.name.value = dev.friendly_name || `TV ${dev.host}`;
        f.elements.type.value = 'sony_bravia';
        f.elements.host.value = dev.host;
        f.elements.mac.value = '';
        f.elements.psk.value = '';
        f.elements.psk.focus();
        toast('Filled in. Enter the PSK (and MAC if known), then Add.');
      });
      head.append(name, use);
      const meta = document.createElement('div');
      meta.className = 'meta';
      meta.textContent = dev.host + (dev.model ? ` · ${dev.model}` : '') + (dev.note ? ` · ${dev.note}` : '');
      card.append(head, meta);
      out.appendChild(card);
    });
  } catch (err) {
    if (!(err instanceof AuthError)) toast(`Scan failed: ${err.message}`, true);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Scan network';
  }
}

// ------------------------------------------------------------- keyboard --
const KEYMAP = {
  ArrowUp: 'up', ArrowDown: 'down', ArrowLeft: 'left', ArrowRight: 'right',
  Enter: 'confirm', Backspace: 'back', Escape: 'back', h: 'home', H: 'home',
  '+': 'volume_up', '=': 'volume_up', '-': 'volume_down', _: 'volume_down',
  PageUp: 'channel_up', PageDown: 'channel_down', s: 'input', S: 'input', ' ': 'play',
};

function wireKeyboard() {
  let playing = false;
  document.addEventListener('keydown', (e) => {
    if ($('#app').hidden || $('#settings').open || e.ctrlKey || e.metaKey || e.altKey) return;
    if (/^(INPUT|SELECT|TEXTAREA)$/.test(document.activeElement.tagName)) return;
    if (e.key === 'm' || e.key === 'M') { e.preventDefault(); run('toggle_mute'); return; }
    if (e.key === 'p' || e.key === 'P') { e.preventDefault(); run('toggle_power'); return; }
    if (e.key === ' ') {
      e.preventDefault();
      if (!e.repeat) { ircc(playing ? 'pause' : 'play'); playing = !playing; }
      return;
    }
    const code = KEYMAP[e.key];
    if (!code) return;
    e.preventDefault();
    ircc(code, { quiet: e.repeat });
  });
}

// ----------------------------------------------------------------- init --
document.addEventListener('DOMContentLoaded', async () => {
  $('#login-form').addEventListener('submit', login);
  $('#logout').addEventListener('click', logout);
  $('#addForm').addEventListener('submit', addDevice);
  $('#scanBtn').addEventListener('click', scanNetwork);
  $('#openSettings').addEventListener('click', openSettings);
  $('#closeSettings').addEventListener('click', () => $('#settings').close());
  wireRemote();
  wireKeyboard();
  setInterval(() => {
    if (!$('#app').hidden && !document.hidden) { refreshStatus(); autoLoadApps(); }
  }, 20000);
  setInterval(() => { if (!$('#app').hidden && !document.hidden) checkScreenReader(); }, 120000);
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && !$('#app').hidden) checkScreenReader();
  });
  $('#a11yOff').addEventListener('click', screenReaderOff);
  $('#a11yCheck').addEventListener('click', () => checkScreenReader({ quiet: false }));

  try {
    await api('GET', '/api/auth/me');
    await showApp();
  } catch { showLogin(); }
});

// ---------------------------------------------------------------- debug --
let debugEntries = [];
let debugLastId = 0;
let debugTimer = null;
let lastSeenErrorId = 0;

const hhmmss = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour12: false });

async function pollDebug() {
  try {
    const { entries } = await api('GET', `/api/debug/log?after=${debugLastId}`);
    if (entries.length) {
      debugEntries = debugEntries.concat(entries).slice(-500);
      debugLastId = entries[entries.length - 1].id;
    }
  } catch { return; }
  const newest = debugEntries.filter((e) => e.level === 'error').pop();
  $('#openDebug').classList.toggle('has-errors', !!newest && newest.id > lastSeenErrorId);
  if ($('#debug').open) renderDebug();
}

function renderDebug() {
  const list = $('#dbgList');
  const errorsOnly = $('#dbgErrorsOnly').checked;
  const rows = debugEntries.filter((e) => !errorsOnly || e.level === 'error');
  list.innerHTML = '';
  if (!rows.length) {
    list.innerHTML = '<p class="empty">Nothing logged yet. Try a command.</p>';
    return;
  }
  rows.slice().reverse().forEach((e) => {
    const row = document.createElement('div');
    row.className = `dbg-row ${e.level}`;
    const top = document.createElement('div');
    top.className = 'top-line';
    [hhmmss(e.ts), e.device, e.action, e.duration_ms != null ? `${e.duration_ms} ms` : null]
      .filter(Boolean)
      .forEach((t) => { const s = document.createElement('span'); s.textContent = t; top.appendChild(s); });
    const msg = document.createElement('div');
    msg.className = 'msg';
    msg.textContent = e.message;
    row.append(top, msg);
    const extra = [e.params ? `params: ${JSON.stringify(e.params)}` : null, e.detail].filter(Boolean).join('\n');
    if (extra) { const pre = document.createElement('pre'); pre.textContent = extra; row.appendChild(pre); }
    list.appendChild(row);
  });
}

function debugAsText() {
  return debugEntries.map((e) =>
    `${new Date(e.ts * 1000).toISOString()} ${e.level.toUpperCase()} ${e.device || '-'} ${e.action || '-'}` +
    `${e.duration_ms != null ? ` (${e.duration_ms}ms)` : ''}: ${e.message}` +
    `${e.params ? ` params=${JSON.stringify(e.params)}` : ''}${e.detail ? `\n    ${e.detail}` : ''}`).join('\n');
}

function wireDebug() {
  $('#openDebug').addEventListener('click', async () => {
    await pollDebug();
    const last = debugEntries.filter((e) => e.level === 'error').pop();
    if (last) lastSeenErrorId = last.id;
    $('#openDebug').classList.remove('has-errors');
    renderDebug();
    $('#debug').showModal();
  });
  $('#closeDebug').addEventListener('click', () => $('#debug').close());
  $('#dbgErrorsOnly').addEventListener('change', renderDebug);
  $('#dbgClear').addEventListener('click', async () => {
    try { await api('DELETE', '/api/debug/log'); } catch { /* ignore */ }
    debugEntries = [];
    renderDebug();
  });
  $('#dbgCopy').addEventListener('click', async () => {
    const text = debugAsText();
    try {
      if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(text);
      else { // plain http on the LAN: the async clipboard API is blocked
        // Must live inside the open <dialog>: everything outside a modal is inert.
        const ta = document.createElement("textarea");
        ta.value = text; ta.readOnly = true; ta.className = "dbg-copy";
        $("#debug").appendChild(ta); ta.focus(); ta.select();
        const ok = document.execCommand("copy");
        ta.remove();
        if (!ok) throw new Error("copy blocked");
      }
      toast("Copied");
    } catch { showDebugText(text); }
  });
  debugTimer = setInterval(() => { if (!$('#app').hidden && !document.hidden) pollDebug(); }, 3000);
}
document.addEventListener('DOMContentLoaded', wireDebug);

// Last-resort copy: show the log in a text box the user can select by hand.
function showDebugText(text) {
  let box = $('#dbgText');
  if (!box) {
    box = document.createElement('textarea');
    box.id = 'dbgText';
    box.readOnly = true;
    box.className = 'dbg-text';
    $('#dbgList').before(box);
  }
  box.value = text;
  box.focus();
  box.select();
  toast('Copy blocked by the browser. Text selected: press Ctrl+C', true, 6000);
}

// ---------------------------------------------------------- TV settings --
function singleDevice() {
  const d = currentDevice();
  if (!d || selected === ALL) { toast('Pick a single TV tab first', true); return null; }
  return d;
}

async function loadTvSettings() {
  const d = singleDevice();
  if (!d) return;
  const note = $('#tvSettingsNote');
  note.textContent = 'Loading…';
  try {
    const { result } = await api('POST', `/api/devices/${d.id}/action`, { action: 'settings' });
    renderTvSettings(d, result);
  } catch (err) {
    if (err instanceof AuthError) return;
    note.textContent = `Could not load settings: ${err.message} (see Debug log)`;
  }
}

function renderTvSettings(d, result) {
  const box = $('#tvSettings');
  box.innerHTML = '';
  const items = (result.settings || []).filter((s) => s.available);
  const problems = Object.entries(result.problems || {}).map(([g, m]) => `${g}: ${m}`);
  $('#tvSettingsNote').textContent = items.length
    ? `${items.length} settings from ${d.name}.` + (problems.length ? ` Not available: ${problems.join('; ')}` : '')
    : `${d.name} reported no adjustable settings.` + (problems.length ? ` ${problems.join('; ')}` : '');
  let group = null;
  items.forEach((s) => {
    if (s.group !== group) {
      group = s.group;
      const h = document.createElement('h4');
      h.className = 'set-group';
      h.textContent = { picture: 'Picture', sound: 'Sound', remote: 'HDMI / remote devices', system: 'System' }[group] || group;
      box.appendChild(h);
    }
    const row = document.createElement('div');
    row.className = 'set-row';
    const label = document.createElement('label');
    label.textContent = s.title || s.target;
    row.appendChild(label);
    const send = (value) => run('set_setting', { group: s.group, target: s.target, value: String(value) });
    let input;
    if (s.kind === 'range') {
      input = document.createElement('input');
      input.type = 'range';
      input.min = s.min; input.max = s.max; input.step = s.step || 1;
      input.value = s.value;
      const out = document.createElement('output');
      out.textContent = s.value;
      input.addEventListener('input', () => { out.textContent = input.value; });
      input.addEventListener('change', () => send(input.value));
      row.append(input, out);
    } else if (s.kind === 'toggle') {
      input = document.createElement('input');
      input.type = 'checkbox';
      input.checked = s.value === 'on';
      input.addEventListener('change', () => send(input.checked ? 'on' : 'off'));
      row.appendChild(input);
    } else {
      input = document.createElement('select');
      const opts = s.options.includes(s.value) ? s.options : [s.value, ...s.options];
      opts.forEach((o) => {
        const opt = document.createElement('option');
        opt.value = o; opt.textContent = o;
        input.appendChild(opt);
      });
      input.value = s.value;
      input.addEventListener('change', () => send(input.value));
      row.appendChild(input);
    }
    label.title = s.target;
    box.appendChild(row);
  });
}

async function showMethods() {
  const d = singleDevice();
  if (!d) return;
  const out = $('#methodsOut');
  out.hidden = false;
  out.textContent = 'Asking the TV…';
  try {
    const { result } = await api('POST', `/api/devices/${d.id}/action`, { action: 'methods' });
    out.textContent = Object.entries(result.methods)
      .map(([svc, list]) => `${svc}:\n  ${list.join('\n  ')}`).join('\n\n');
  } catch (err) {
    if (!(err instanceof AuthError)) out.textContent = `Failed: ${err.message}`;
  }
}

async function testAdb() {
  const d = singleDevice();
  if (!d) return;
  const out = $('#adbOut');
  out.hidden = false;
  out.textContent = 'Looking for the TV’s ADB port (can take ~10s), then connecting… if the TV shows an "Allow debugging?" prompt, choose Always allow.';
  try {
    const { result } = await api('POST', `/api/devices/${d.id}/action`, { action: 'adb_info' });
    out.textContent = `Connected.\n${Object.entries(result).map(([k, v]) => `${k}: ${v}`).join('\n')}`;
  } catch (err) {
    if (!(err instanceof AuthError)) out.textContent = `Failed: ${err.message}`;
  }
}

const SETTINGS_PAGES = [
  ['settings', 'Settings home'], ['network', 'Network & Internet'], ['display', 'Display'],
  ['picture', 'Picture'], ['sound', 'Sound'], ['sony_sound', 'Sound (picture menu)'],
  ['speakers', 'Speakers'], ['inputs', 'External inputs'], ['remote_start', 'Remote start'],
  ['power', 'Power & Eco'], ['apps', 'Apps'], ['accessibility', 'Accessibility'],
  ['bluetooth', 'Bluetooth & accessories'], ['datetime', 'Date & time'], ['language', 'Language'],
  ['keyboard', 'Keyboard'], ['storage', 'Storage'], ['privacy', 'Privacy'],
  ['location', 'Location'], ['about', 'About'], ['developer', 'Developer options'],
];

async function openSettingsPage(params) {
  const d = singleDevice();
  if (!d) return;
  // Not checked yet: the server checks this TV's pages first (or waits for the check that
  // is already running), then opens the page it found.
  const checking = params.page && !savedPages(d);
  if (checking) toast('Finding this page on the TV…');
  const r = await run('adb_open', params);
  if (r) toast('Opened on the TV');
  if (checking) await loadDevices();     // pick up the pages the check saved
}

async function listActivities() {
  const d = singleDevice();
  if (!d) return;
  const out = $('#adbActivitiesOut');
  out.hidden = false;
  out.textContent = 'Reading the TV’s Settings pages…';
  try {
    const { result } = await api('POST', `/api/devices/${d.id}/action`, { action: 'adb_activities' });
    out.textContent = Object.entries(result.packages)
      .map(([pkg, acts]) => `${pkg}:\n  ${acts.join('\n  ')}`).join('\n\n') || 'Nothing found';
  } catch (err) {
    if (!(err instanceof AuthError)) out.textContent = `Failed: ${err.message}`;
  }
}

// Which Settings pages each TV has is checked over ADB when it is added (or when its
// TV settings / All commands panel is first opened) and saved with the device. TV settings
// shows only the shortcuts that work; every other page the TV has goes to All commands.
const pagesScanTried = new Set();
const pagesScanRunning = new Set();

function savedPages(d) {
  return d && d.config && d.config.settings_pages ? d.config.settings_pages : null;
}

function applyPagesSupport() {
  const d = currentDevice();
  const saved = selected !== ALL ? savedPages(d) : null;
  const ok = saved ? new Set(saved.pages) : null;
  document.querySelectorAll('#settingsPages [data-page]').forEach((b) => {
    b.hidden = !!ok && !ok.has(b.dataset.page);
  });
  $('#settingsPagesNote').textContent = saved
    ? `Settings pages on ${d.name} (the rest are under All commands):`
    : 'Open a Settings page on the TV (not checked against this TV yet):';
  renderExtraPages();
}

function renderExtraPages() {
  const d = currentDevice();
  const saved = selected !== ALL ? savedPages(d) : null;
  const box = $('#extraPages');
  box.innerHTML = '';
  if (!d || selected === ALL) {
    $('#extraPagesNote').textContent = 'Pick a single device to see its other Settings pages.';
    return;
  }
  if (!saved) {
    $('#extraPagesNote').textContent = pagesScanRunning.has(d.id)
      ? 'Checking this TV’s Settings pages over ADB…'
      : 'Not checked yet: open this panel with the TV on and Network debugging enabled.';
    return;
  }
  $('#extraPagesNote').textContent = saved.extra.length
    ? `${saved.extra.length} more Settings pages on ${d.name}.`
    : `No other Settings pages found on ${d.name}.`;
  saved.extra.forEach(({ label, target }) => {
    const b = document.createElement('button');
    b.className = 'app';
    b.textContent = label;
    b.title = target;
    b.addEventListener('click', () => openSettingsPage({ intent: target }));
    box.appendChild(b);
  });
}

async function scanPages(d, { quiet = false } = {}) {
  if (!d) return;
  if (pagesScanRunning.has(d.id)) {
    if (!quiet) toast(`Already checking ${d.name}'s Settings pages…`);
    return;
  }
  pagesScanRunning.add(d.id);
  pagesScanTried.add(d.id);
  const out = $('#adbOut');
  if (!quiet) {
    out.hidden = false;
    out.textContent = `Checking which Settings pages ${d.name} has… if the TV asks to allow debugging, choose Always allow.`;
  }
  renderExtraPages();
  try {
    const { device } = await api('POST', `/api/devices/${d.id}/scan-pages`);
    const i = devices.findIndex((x) => x.id === device.id);
    if (i >= 0) devices[i] = device;
    const saved = savedPages(device);
    const names = Object.fromEntries(SETTINGS_PAGES);
    const missing = SETTINGS_PAGES.map(([k]) => k).filter((k) => !saved.pages.includes(k));
    const msg = `${device.name}: ${saved.pages.length} shortcuts work, ${saved.extra.length} more pages under All commands.`
      + (missing.length ? `\nNot on this TV (hidden): ${missing.map((k) => names[k]).join(', ')}` : '');
    if (quiet) toast(msg.split('\n')[0]); else out.textContent = msg;
  } catch (err) {
    if (err instanceof AuthError) return;
    const msg = `Could not check ${d.name}'s Settings pages: ${err.message}`;
    if (quiet) toast(msg, true, 6000); else out.textContent = msg;
  } finally {
    pagesScanRunning.delete(d.id);
    if (currentDevice() && currentDevice().id === d.id) applyPagesSupport();
  }
}

function autoScanPages() {
  const d = currentDevice();
  if (!d || selected === ALL || d.type !== 'sony_bravia' || savedPages(d) || pagesScanTried.has(d.id)) return;
  scanPages(d, { quiet: true });
}

// Screen preview: fetch one frame, show it, then fetch the next (never overlapping).
let screenTimer = null;
let screenRunning = false;

// Live video: H.264 over a WebSocket, played by WebCodecs (HTTPS/localhost) or MSE (plain HTTP).
// Still frames are the fallback for browsers with neither (e.g. iPhone).
let liveWs = null;
let livePlayer = null;
let liveTimer = null;
let preferMse = false;   // set once WebCodecs fails, so the preview falls back instead of stopping
let liveNote = '';
const canPlayLive = () => WebCodecsPlayer.supported() || MsePlayer.supported();

function stopLive() {
  clearInterval(liveTimer);
  if (liveWs) { liveWs.onclose = null; liveWs.close(); liveWs = null; }
  if (livePlayer) { livePlayer.destroy(); livePlayer = null; }
}

// Recorded diagnostic: runs the preview for 15 s, logs everything on both sides, offers a file.
let diag = null;

function diagLine(text) {
  if (!diag) return;
  diag.lines.push(`${((performance.now() - diag.t0) / 1000).toFixed(2).padStart(6)}s ${text}`);
  const out = $('#diagOut');
  out.textContent = diag.lines.join('\n');
  out.scrollTop = out.scrollHeight;
}

function finishDiagnostic() {
  if (!diag) return;
  diagLine('--- end of recording ---');
  const finished = diag;
  diag = null;
  clearTimeout(finished.timeout);
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const link = $('#diagDownload');
  URL.revokeObjectURL(link.href || '');
  link.href = URL.createObjectURL(new Blob([finished.lines.join('\n') + '\n'], { type: 'text/plain' }));
  link.download = `gremote-preview-diagnostic-${stamp}.txt`;
  link.hidden = false;
  $('#diagStart').disabled = false;
}

function startDiagnostic() {
  const d = singleDevice();
  if (!d) return;
  if (screenRunning) stopScreen();
  diag = { lines: [], t0: performance.now(), timeout: null };
  $('#diagOut').hidden = false;
  $('#diagDownload').hidden = true;
  $('#diagStart').disabled = true;
  const quality = $('#screenQuality');
  diagLine(`gremote preview diagnostic, ${new Date().toString()}`);
  diagLine(`device: ${d.name} (${d.host}) type=${d.type}`);
  diagLine(`quality setting: ${quality.value} (${quality.selectedOptions[0].textContent})`);
  diagLine(`browser: ${navigator.userAgent}`);
  diagLine(`page: ${location.protocol}//${location.host} secureContext=${window.isSecureContext} `
    + `screen=${screen.width}x${screen.height} dpr=${window.devicePixelRatio}`);
  diagLine(`support: WebCodecs=${WebCodecsPlayer.supported()} MSE=${MsePlayer.supported()} `
    + `VideoDecoder=${'VideoDecoder' in window} MediaSource=${!!window.MediaSource} JMuxer=${!!window.JMuxer}`);
  diagLine(`player chosen: ${WebCodecsPlayer.supported() ? 'WebCodecs' : MsePlayer.supported() ? 'MSE (jmuxer)' : 'still frames'}`);
  screenRunning = true;
  $('#screenToggle').textContent = 'Stop preview';
  if (canPlayLive()) startLive(d); else { diagLine('no live player available in this browser'); stopScreen(); return; }
  diag.timeout = setTimeout(() => stopScreen('15 s recording complete'), 15000);
}

function startLive(d) {
  $('#screenImg').hidden = true;
  if (WebCodecsPlayer.supported() && !preferMse) {
    livePlayer = new WebCodecsPlayer($('#screenCanvas'), (msg) => {
      diagLine(`WebCodecs failed: ${msg}; falling back to MSE`);
      if (!MsePlayer.supported()) { stopScreen(`Failed: ${msg}`); return; }
      preferMse = true;
      stopLive();
      startLive(d);
      liveNote = `WebCodecs failed (${msg}), using MSE · `;
    });
  } else {
    livePlayer = new MsePlayer($('#screenVideo'));
  }
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const quality = $('#screenQuality').value;   // already "size=WxH&bitrate=N"; must not be escaped
  const ws = new WebSocket(`${proto}://${location.host}/api/devices/${d.id}/screen/stream?${quality}${diag ? "&diag=1" : ""}`);
  ws.onopen = () => diagLine("websocket open");
  ws.binaryType = 'arraybuffer';
  liveWs = ws;
  let bytes = 0;
  let since = performance.now();
  ws.onmessage = (ev) => {
    if (typeof ev.data === 'string') {
      let msg = {};
      try { msg = JSON.parse(ev.data); } catch { /* not JSON */ }
      if (msg.diag) { diagLine(`server: ${msg.diag}`); return; }
      diagLine(`server error: ${msg.error || ev.data}`);
      stopScreen(msg.error ? `Failed: ${msg.error}` : 'Stream error');
      return;
    }
    bytes += ev.data.byteLength;
    try { livePlayer.feed(new Uint8Array(ev.data)); } catch (err) { stopScreen(`Failed: ${err.message}`); }
  };
  ws.onclose = (ev) => { diagLine(`websocket closed code=${ev.code}`); if (screenRunning) stopScreen('Stream closed'); };
  liveTimer = setInterval(() => {
    const secs = (performance.now() - since) / 1000;
    $('#screenStatus').hidden = false;
    $('#screenStatus').textContent = `${liveNote}${Math.round(bytes * 8 / 1000 / secs)} kbit/s · ${livePlayer.stats()}`;
    diagLine(`client: ${$("#screenStatus").textContent} | ${livePlayer.debug()} | ws=${ws.readyState}`);
    bytes = 0;
    since = performance.now();
  }, 1000);
}

function stopScreen(message) {
  if (message) diagLine(`stopped: ${message}`);
  stopLive();
  screenRunning = false;
  clearTimeout(screenTimer);
  $('#screenToggle').textContent = 'Start preview';
  const status = $('#screenStatus');
  status.hidden = !message;
  status.textContent = message || '';
  finishDiagnostic();
}

async function screenLoop(deviceId) {
  if (!screenRunning) return;
  const started = performance.now();
  try {
    const res = await fetch(`/api/devices/${deviceId}/screen?t=${Date.now()}`, { credentials: 'same-origin' });
    if (res.status === 401) { stopScreen(); location.reload(); return; }
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch { /* not JSON */ }
      stopScreen(`Failed: ${detail}`);
      return;
    }
    const url = URL.createObjectURL(await res.blob());
    const img = $('#screenImg');
    const old = img.src;
    img.src = url;
    img.hidden = false;
    if (old.startsWith('blob:')) URL.revokeObjectURL(old);
    const ms = Math.round(performance.now() - started);
    $('#screenStatus').hidden = false;
    $('#screenStatus').textContent = `Live · ${ms} ms per frame`;
  } catch (err) {
    stopScreen(`Failed: ${err.message}`);
    return;
  }
  if (screenRunning && singleDevice() && currentDevice().id === deviceId) {
    screenTimer = setTimeout(() => screenLoop(deviceId), 150);
  } else {
    stopScreen();
  }
}

function toggleScreen() {
  if (screenRunning) { stopScreen(); return; }
  const d = singleDevice();
  if (!d) return;
  screenRunning = true;
  liveNote = '';
  $('#screenToggle').textContent = 'Stop preview';
  $('#screenStatus').hidden = false;
  $('#screenStatus').textContent = 'Connecting to the TV (first frame can take a few seconds)…';
  if (canPlayLive()) startLive(d); else screenLoop(d.id);
}

async function downloadSample() {
  const d = singleDevice();
  if (!d) return;
  if (screenRunning) stopScreen();
  const btn = $('#sampleBtn');
  const link = $('#sampleLink');
  btn.disabled = true;
  link.hidden = true;
  const status = $('#screenStatus');
  status.hidden = false;
  status.textContent = 'Recording a 5 s sample from the TV…';
  try {
    const res = await fetch(`/api/devices/${d.id}/screen/sample?${$('#screenQuality').value}`, { credentials: 'same-origin' });
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch { /* not JSON */ }
      throw new Error(detail);
    }
    const blob = await res.blob();
    URL.revokeObjectURL(link.href || '');
    link.href = URL.createObjectURL(blob);
    link.download = 'tv-sample.h264';
    link.hidden = false;
    link.textContent = `Save the video sample (${Math.round(blob.size / 1024)} KB)`;
    status.textContent = 'Sample ready: use the link below to save it.';
  } catch (err) {
    status.textContent = `Failed: ${err.message}`;
  } finally {
    btn.disabled = false;
  }
}

async function runBench() {
  const d = singleDevice();
  if (!d) return;
  if (screenRunning) stopScreen();
  const out = $('#benchOut');
  out.hidden = false;
  out.textContent = 'Benchmarking (about 30 seconds)…';
  try {
    const { result } = await api('POST', `/api/devices/${d.id}/action`, { action: 'adb_bench' });
    out.textContent = result.steps.map((s) => `[${s.ms} ms] ${s.step}\n${s.output}`).join('\n\n')
      + `\n\nOn the gremote server: ${Object.entries(result.server_tools).map(([k, v]) => `${k}=${v || 'not installed'}`).join(', ')}`;
  } catch (err) {
    if (!(err instanceof AuthError)) out.textContent = `Failed: ${err.message}`;
  }
}

document.addEventListener('DOMContentLoaded', () => {
  $('#screenBench').addEventListener('click', runBench);
  $('#diagStart').addEventListener('click', startDiagnostic);
  $('#sampleBtn').addEventListener('click', downloadSample);
  ['loadedmetadata', 'playing', 'waiting', 'stalled', 'error', 'resize', 'emptied'].forEach((t) => $('#screenVideo').addEventListener(t, () => diagLine(`video event: ${t}`)));
  window.addEventListener('error', (e) => diagLine(`page error: ${e.message}`));
  window.addEventListener('unhandledrejection', (e) => diagLine(`unhandled rejection: ${e.reason}`));
  $('#screenToggle').addEventListener('click', toggleScreen);
  // Remember the preview quality per browser (smaller = less work for the TV, more frames).
  const qualitySel = $('#screenQuality');
  const savedQuality = store.get('screenQuality');
  if (savedQuality && [...qualitySel.options].some((o) => o.value === savedQuality)) qualitySel.value = savedQuality;
  qualitySel.addEventListener('change', () => store.set('screenQuality', qualitySel.value));
  $('#adbSupported').addEventListener('click', () => { const d = singleDevice(); if (d) scanPages(d); });
  ['#tvSettingsPanel', '#allCommandsPanel'].forEach((id) => $(id).addEventListener('toggle', (e) => {
    if (e.target.open) autoScanPages();
  }));
  $('#adbActivities').addEventListener('click', listActivities);
  const grid = $('#settingsPages');
  SETTINGS_PAGES.forEach(([page, label]) => {
    const b = document.createElement('button');
    b.className = 'app';
    b.dataset.page = page;
    b.textContent = label;
    b.addEventListener('click', () => {
      // A checked TV opens exactly the page found for this shortcut on that TV.
      const saved = savedPages(currentDevice());
      const target = saved && saved.targets ? saved.targets[page] : null;
      openSettingsPage(target ? { intent: target } : { page });
    });
    grid.appendChild(b);
  });
  $('#adbIntentGo').addEventListener('click', () => {
    const intent = $('#adbIntent').value.trim();
    if (intent) openSettingsPage({ intent });
  });
  $('#adbTest').addEventListener('click', testAdb);
  $('#loadTvSettings').addEventListener('click', loadTvSettings);
  $('#showMethods').addEventListener('click', showMethods);
  $('#openTvSettingsApp').addEventListener('click', () => run('launch_app', { title: 'Settings' }));
});

// ------------------------------------------------------- screen reader --
// A held Mute button can switch a TV's screen reader on by accident. Check (over ADB) when a
// TV is selected and every 2 minutes, show a banner while it is on, and turn it off in one tap.
const a11yState = {};   // id -> last accessibility status
let a11yBusy = false;

async function checkScreenReader({ quiet = true } = {}) {
  const d = currentDevice();
  if (!d || selected === ALL || d.type !== 'sony_bravia' || a11yBusy) return;
  a11yBusy = true;
  try {
    const { result } = await api('POST', `/api/devices/${d.id}/action`, { action: 'accessibility' });
    a11yState[d.id] = result;
    if (!quiet) {
      toast(result.screen_reader_on ? 'The screen reader is ON'
        : result.enabled ? `Screen reader is off (on: ${result.services.map((s) => s.name).join(', ')})`
          : 'Screen reader is off');
    }
  } catch (err) {
    if (!quiet && !(err instanceof AuthError)) toast(`Could not check: ${err.message}`, true);
  } finally {
    a11yBusy = false;
    renderScreenReader();
  }
}

function renderScreenReader() {
  const d = currentDevice();
  const st = d && selected !== ALL ? a11yState[d.id] : null;
  const on = !!(st && st.screen_reader_on);
  $('#a11yBanner').hidden = !on;
  if (on) {
    const names = st.services.filter((s) => s.screen_reader).map((s) => s.name).join(', ');
    $('#a11yText').textContent = `${names || 'The screen reader'} is on for ${d.name}.`;
  }
}

async function screenReaderOff() {
  const d = currentDevice();
  if (!d) return;
  const btn = $('#a11yOff');
  btn.disabled = true;
  try {
    const { result } = await api('POST', `/api/devices/${d.id}/action`, { action: 'screen_reader_off' });
    a11yState[d.id] = result;
    toast(`Turned off: ${(result.turned_off || []).join(', ') || 'screen reader'}`);
  } catch (err) {
    if (!(err instanceof AuthError)) toast(err.message, true, 6000);
  } finally {
    btn.disabled = false;
    renderScreenReader();
  }
}

// --------------------------------------------------------------- inputs --
// Each TV reports its own inputs (number of HDMI ports, their names, composite, screen
// mirroring, HDMI-CEC devices). The fixed TV/HDMI 1-4 buttons are only the fallback.
let defaultInputsHtml = '';

async function loadInputs() {
  const d = currentDevice();
  if (!d || selected === ALL || d.type !== 'sony_bravia' || inputsCache[d.id]
      || inputsRequested.has(d.id)) return;
  inputsRequested.add(d.id);
  try {
    const data = await api('POST', `/api/devices/${d.id}/action`, { action: 'inputs' });
    const list = (data.result && data.result.inputs) || [];
    if (list.length) inputsCache[d.id] = list;
    if (currentDevice() && currentDevice().id === d.id) renderInputs();
  } catch (err) {
    inputsRequested.delete(d.id);   // TV off or unreachable: try again next time
  }
}

function renderInputs() {
  const d = currentDevice();
  const list = d && selected !== ALL ? inputsCache[d.id] : null;
  const row = $('#inputButtons');
  if (!list) {
    if (defaultInputsHtml) row.innerHTML = defaultInputsHtml;
    return;
  }
  row.innerHTML = '';
  list.forEach((inp) => {
    const b = document.createElement('button');
    b.className = `btn small${inp.connected ? '' : ' dim'}`;
    b.dataset.uri = inp.uri;
    b.textContent = inp.title;
    b.title = inp.connected ? inp.uri : `${inp.uri} (nothing connected)`;
    row.appendChild(b);
  });
}

// ------------------------------------------------------------- commands --
// The TV reports which IRCC commands it supports. Use that to show the extra
// keys (Action/Sync/Audio/STB) only when available and to list everything else.
async function loadCommands() {
  const d = currentDevice();
  if (!d || selected === ALL || commandsCache[d.id] || commandsRequested.has(d.id)) return;
  commandsRequested.add(d.id);
  try {
    const data = await api('POST', `/api/devices/${d.id}/action`, { action: 'commands' });
    commandsCache[d.id] = (data.result && data.result.commands) || [];
    if (currentDevice() && currentDevice().id === d.id) renderCommands();
  } catch (err) {
    commandsRequested.delete(d.id);   // retry next time the tab is opened
  }
}

function renderCommands() {
  const d = currentDevice();
  const names = d && selected !== ALL ? commandsCache[d.id] : null;
  const lower = new Map((names || []).map((n) => [n.toLowerCase(), n]));

  document.querySelectorAll('#extraKeys [data-ircc]').forEach((el) => {
    // Unknown list (All tab / not loaded / unreachable): keep the default keys visible.
    el.hidden = !!names && names.length > 0 && !lower.has(el.dataset.ircc.toLowerCase());
  });
  document.querySelectorAll('#extraKeys [data-ircc-match]').forEach((el) => {
    const re = new RegExp(el.dataset.irccMatch, 'i');
    const hit = (names || []).find((n) => re.test(n));
    el.dataset.resolved = hit || '';
    el.hidden = !hit;    // STB only appears when the TV lists such a command
  });

  const box = $('#allCommands');
  box.innerHTML = '';
  $('#allCommandsNote').textContent = names
    ? `${names.length} commands reported by ${d.name}. Names vary by model.`
    : 'Every remote command this TV reports. Pick a single device to load them.';
  (names || []).forEach((name) => {
    const b = document.createElement('button');
    b.className = 'app';
    b.textContent = name;
    onTap(b, () => ircc(name));
    box.appendChild(b);
  });
}
