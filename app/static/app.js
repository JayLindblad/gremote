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
  applyPagesSupport();
  loadCommands();
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
  document.querySelectorAll('#remote [data-input]').forEach((el) => {
    onTap(el, () => run('set_input', { input: el.dataset.input }));
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

  $('#loadApps').addEventListener('click', loadApps);
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
    p.textContent = selected === ALL ? 'Select a single device to see its apps.' : 'No apps loaded yet.';
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

async function loadApps() {
  const d = currentDevice();
  if (!d) { toast('Select a single device first', true); return; }
  try {
    const data = await api('POST', `/api/devices/${d.id}/action`, { action: 'apps' });
    appsCache[d.id] = (data.result && data.result.apps) || [];
    renderApps();
    if (!appsCache[d.id].length) toast('The TV returned no apps', true);
  } catch (err) {
    if (!(err instanceof AuthError)) toast(err.message, true);
  }
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
      meta.textContent = dev.host + (dev.model ? ` · ${dev.model}` : '');
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
  setInterval(() => { if (!$('#app').hidden && !document.hidden) refreshStatus(); }, 20000);

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
  if (!singleDevice()) return;
  const r = await run('adb_open', params);
  if (r) toast('Opened on the TV');
}

async function listActivities() {
  const d = singleDevice();
  if (!d) return;
  const out = $('#adbOut');
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

// Which shortcuts this TV can resolve; unsupported buttons are hidden (kept per TV).
const pagesSupport = {};

function applyPagesSupport() {
  const d = currentDevice();
  const support = d && selected !== ALL ? pagesSupport[d.id] : null;
  document.querySelectorAll('#settingsPages [data-page]').forEach((b) => {
    b.hidden = !!support && support[b.dataset.page] === false;
  });
}

async function checkSupportedPages() {
  const d = singleDevice();
  if (!d) return;
  const out = $('#adbOut');
  out.hidden = false;
  out.textContent = 'Checking which Settings pages this TV supports…';
  try {
    const { result } = await api('POST', `/api/devices/${d.id}/action`, { action: 'adb_supported' });
    pagesSupport[d.id] = result.pages;
    const names = Object.fromEntries(SETTINGS_PAGES);
    const yes = Object.keys(result.pages).filter((k) => result.pages[k]).map((k) => names[k]);
    const no = Object.keys(result.pages).filter((k) => !result.pages[k]).map((k) => names[k]);
    out.textContent = `Supported: ${yes.join(', ') || 'none'}\nNot available on this TV (hidden): ${no.join(', ') || 'none'}`;
    applyPagesSupport();
  } catch (err) {
    if (!(err instanceof AuthError)) out.textContent = `Failed: ${err.message}`;
  }
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
  $('#adbSupported').addEventListener('click', checkSupportedPages);
  $('#adbActivities').addEventListener('click', listActivities);
  const grid = $('#settingsPages');
  SETTINGS_PAGES.forEach(([page, label]) => {
    const b = document.createElement('button');
    b.className = 'app';
    b.dataset.page = page;
    b.textContent = label;
    b.addEventListener('click', () => openSettingsPage({ page }));
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
