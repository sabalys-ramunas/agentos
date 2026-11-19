// agentos lane viewer: live view, take over, hand back, stop agent.
//
// The server decides everything that matters: the relay drops this page's input unless
// the lane's control is "human" and the link is a control link. The page only mirrors
// that state (viewOnly) so it does not send input that would be dropped anyway.

import RFB from './vendor/novnc/core/rfb.js';
import keysyms from './vendor/novnc/core/input/keysymdef.js';

const boot = JSON.parse(document.getElementById('agentos-boot').textContent);
const $ = (id) => document.getElementById(id);
const app = $('app');
const isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);

const state = {
  lane: boot.lane,
  control: boot.lane.control,
  mode: boot.mode,
  token: boot.token,
  person: Boolean(boot.person), // an owner link, or the owner signed in to the panel here: may hand back
  expiresAt: Date.parse(boot.expires_at),
  actions: [],
  actionsAvailable: boot.traces,
  rfb: null,
  connected: false,
  attempts: 0,
  retryTimer: null,
  events: null,
  busy: false,
  ended: null, // "expired" | "gone" | "frozen" | "revoked": nothing more to do
  pill: null, // the mode text last announced on the live view
  notices: [], // off-list documents while a human has control (let through, agents would be refused), newest first
};

const RETRY_DELAYS = [1000, 2000, 4000, 8000, 10000];
const PILL_SECONDS = 4; // the mode pill shows this long over the live view, then folds into the header
const MAX_NOTICES = 4;
const KIND_TEXT = {
  subframe: 'a frame in this page',
  redirect: 'a redirect',
  navigation: 'a page',
  popup: 'a popup window',
  committed: 'a page that had already opened',
};
const STATE_LABELS = {
  cold: ['Not running', 'muted'],
  warming: ['Starting', 'busy'],
  warm: ['Ready', 'ok'],
  active: ['Agent working', 'busy'],
  quarantined: ['Quarantined', 'danger'],
};
const SIGNAL_LABELS = {
  ok: ['Signal ok', 'ok'],
  captcha: ['CAPTCHA', 'warn'],
  rate_limited: ['Rate limited', 'warn'],
  blocked: ['Blocked', 'danger'],
  logged_out: ['Logged out', 'warn'],
};

// ---- small helpers ----

const canControl = () => state.mode === 'control';
const controlState = () => state.control?.state || 'agent';
// the server passes input only from a person (an owner link, the owner signed in here, or the holder of an
// agent environment's own browser link: boot.person), so the page sends none otherwise and says so
const interactive = () => canControl() && state.person && controlState() === 'human';
const running = () => state.lane.state === 'warm' || state.lane.state === 'active';

function clock(iso, seconds = false) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const opts = { hour: '2-digit', minute: '2-digit' };
  if (seconds) opts.second = '2-digit';
  return d.toLocaleTimeString([], opts);
}

function capitalize(text) {
  return text ? text.charAt(0).toUpperCase() + text.slice(1) : text;
}

function minutes(seconds) {
  const m = Math.round(seconds / 60);
  return m <= 1 ? '1 minute' : `${m} minutes`;
}

let toastTimer = null;
function toast(message, tone = 'info') {
  const el = $('toast');
  el.textContent = message;
  el.dataset.tone = tone;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, tone === 'error' ? 6000 : 3500);
}

function withToken(path) {
  return `${path}?t=${encodeURIComponent(state.token)}`;
}

// ---- rendering ----

function whoText() {
  const c = state.control || {};
  const by = c.by && c.by !== 'unknown' ? c.by : null;
  switch (controlState()) {
    case 'human':
      if (canControl() && by === boot.me) return 'You have control';
      return by ? `${capitalize(by)} has control` : 'A human has control';
    case 'paused':
      return by ? `Agent stopped by ${by}` : 'Agent stopped';
    default:
      return 'Agent in control';
  }
}

function render() {
  const c = controlState();
  app.dataset.control = c;
  app.dataset.mode = state.mode;
  app.dataset.interactive = String(interactive());

  const [ws, name] = state.lane.id.split('/');
  $('lane-ws').textContent = ws;
  $('lane-name').textContent = name;

  const [label, tone] = STATE_LABELS[state.lane.state] || [state.lane.state, 'muted'];
  $('state-label').textContent = label;
  $('state-chip').dataset.tone = tone;
  $('state-chip').title = state.lane.quarantine_reason || '';

  const signal = state.lane.signal && SIGNAL_LABELS[state.lane.signal];
  $('signal-chip').hidden = !signal;
  if (signal) {
    $('signal-label').textContent = signal[0];
    $('signal-chip').dataset.tone = signal[1];
  }

  // where websites see this lane coming from: always shown, proxy or not
  const proxy = state.lane.proxy?.label;
  const exitIp = state.lane.exit_ip;
  let exitText;
  let exitTitle;
  if (proxy) {
    exitText = exitIp ? `Exit IP ${exitIp} · proxy ${proxy}` : `Proxy ${proxy} · exit IP not checked yet`;
    exitTitle = exitIp
      ? `Websites see this lane coming from ${exitIp}, through the proxy ${proxy}.`
      : `This lane goes out through the proxy ${proxy}. Its address shows here after the lane's next check.`;
  } else {
    exitText = exitIp ? `Exit IP ${exitIp} · no proxy` : 'No proxy';
    exitTitle = 'This lane has no proxy, so websites see the address of the computer it runs on.';
  }
  $('exit-label').textContent = exitText;
  $('exit-chip').title = exitTitle;

  $('who-text').textContent = whoText();
  const since = state.control?.since;
  $('who-since').textContent = since ? `since ${clock(since)}` : '';
  $('who-since').title = since ? new Date(since).toLocaleString() : '';

  // the mode pill: over the live view for a few seconds when the mode changes, then only the
  // header says it (the dot by "who", and the same text for screen readers)
  let pill;
  if (interactive()) pill = 'You have control. Your input goes to the lane.';
  else if (!canControl()) pill = 'View only';
  else if (c === 'human' && !state.person) pill = 'View only. Sign in to the panel here to use the lane.';
  else if (c === 'paused') pill = 'View only. The agent is stopped.';
  else if (c === 'human') pill = 'View only. Someone else has control.';
  else pill = 'View only. The agent has control.';
  $('mode-status').textContent = pill;
  if (pill !== state.pill) {
    state.pill = pill;
    showPill(pill);
  }

  // actions
  $('btn-take').hidden = !canControl() || c === 'human';
  $('btn-handback').hidden = !canControl() || c === 'agent';
  $('btn-stop').hidden = !canControl() || c === 'paused';
  $('btn-take').classList.toggle('btn-primary', c !== 'paused');  // one primary action at a time
  $('btn-paste').hidden = !interactive() || !state.connected;
  $('btn-copy-out').hidden = !interactive() || !state.connected || !boot.paths.clipboard;
  $('actions').hidden = !canControl();
  for (const id of ['btn-take', 'btn-handback', 'btn-stop', 'btn-request-handback', 'btn-request-keep']) {
    $(id).disabled = state.busy;
  }

  // hint under the view
  let hint;
  if (!canControl()) hint = 'This link can watch but not control.';
  else if (!state.person && (c === 'paused' || state.control?.reason)) {
    hint = 'Sign in to the panel in this browser (the same address, at /login), then take over and hand it back. An agent may hold this link, so it cannot do that alone.';
  } else if (!state.person && c === 'human') {
    hint = 'Agents are kept off the lane. To click or type in it, sign in to the panel in this browser (the same address, at /login); an agent may hold this link, so its input does not reach the lane.';
  } else if (c === 'human') {
    hint = state.lane.state === 'active'
      ? 'The agent is finishing its current step. Then the lane is all yours.'
      : `Your mouse and keyboard go to the lane. Control returns to the agent after ${minutes(boot.control_idle_seconds)} without input.`;
  } else if (c === 'paused') hint = 'The agent is stopped. Take control to use the browser, or hand it back to let the agent continue.';
  else hint = 'You are watching the agent. Take control to use the browser yourself.';
  $('hint').textContent = hint;

  // the agent's request for control back
  const asked = Boolean(state.control?.takeback_requested) && c !== 'agent';
  $('request-banner').hidden = !asked;
  if (asked) {
    $('request-reason').textContent = state.control.takeback_reason || 'no reason given';
    $('request-actions').hidden = !canControl();
  }

  // the remote screen follows control
  if (state.rfb) {
    state.rfb.viewOnly = !interactive();
    state.rfb.focusOnClick = interactive();
  }

  $('link-info').textContent = `${boot.node} · ${canControl() ? 'control link' : 'view-only link'}`;
  renderOverlay();
}

let pillTimer = null;
function showPill(text) {
  const el = $('mode-pill');
  $('mode-text').textContent = text;
  el.dataset.shown = 'true';
  clearTimeout(pillTimer);
  pillTimer = setTimeout(() => { el.dataset.shown = 'false'; }, PILL_SECONDS * 1000);
}

// ---- block notices ----

function addNotice(notice) {
  const key = `${notice.rule} ${notice.host} ${notice.kind}`;
  state.notices = [{ ...notice, key }, ...state.notices.filter((n) => n.key !== key)].slice(0, MAX_NOTICES);
  renderNotices();
}

function dismissNotice(key) {
  state.notices = state.notices.filter((n) => n.key !== key);
  renderNotices();
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    toast('Copied. Run it on the node.');
  } catch {
    toast('Select the command and copy it.', 'error');
  }
}

async function allowNotice(notice) {
  if (!boot.owner || !notice.allow || notice.busy) return;
  notice.busy = true;
  renderNotices();
  try {
    await post(boot.paths.sites, { add: notice.allow });
    notice.allowed = true;
    toast(notice.rule === 'noted' ? `Added ${notice.allow} to the lane's site list for agents.`
      : `Added ${notice.allow} to the lane's site list. Reload the page in the lane to load it.`);
  } catch (err) {
    toast(err.message, 'error');
  } finally {
    notice.busy = false;
    renderNotices();
  }
}

function make(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function renderNotices() {
  const box = $('notices');
  box.replaceChildren();
  box.hidden = state.notices.length === 0;
  for (const notice of state.notices) {
    const card = make('article', 'notice');
    card.dataset.kind = notice.kind || '';
    const what = KIND_TEXT[notice.kind] || 'a page';
    const where = notice.frame && notice.kind === 'redirect' ? ' in a frame' : '';
    const head = make('p', 'notice-title');
    // "noted": you loaded it (a person in control is never blocked), but an agent would be refused
    const noted = notice.rule === 'noted';
    head.append(make('strong', '', noted ? 'Not on the list: ' : 'Blocked: '), `${what}${where} from `,
      make('span', 'notice-host', notice.host));
    const body = make('p', 'notice-text');
    const more = notice.repeats ? ` (and ${notice.repeats} more like it)` : '';
    body.textContent = noted
      ? `${notice.reason}. Allow it if agents should load it too${more}.`
      : `${notice.reason}. Path ${notice.path || '/'}, rule ${notice.rule}${more}.`;
    card.append(head, body);
    if (notice.command) {
      const row = make('div', 'notice-command');
      const code = make('code', '', notice.command);
      const copy = make('button', 'btn btn-small', 'Copy');
      copy.type = 'button';
      copy.setAttribute('aria-label', `Copy the command that allows ${notice.allow}`);
      copy.addEventListener('click', () => copyText(notice.command));
      row.append(code, copy);
      card.append(row);
    }
    const actions = make('div', 'notice-actions');
    if (boot.owner && notice.allow) {
      const allow = make('button', 'btn btn-small btn-primary', notice.allowed ? 'Allowed' : `Allow ${notice.allow}`);
      allow.type = 'button';
      allow.disabled = Boolean(notice.busy || notice.allowed);
      allow.addEventListener('click', () => allowNotice(notice));
      actions.append(allow);
    }
    const close = make('button', 'btn btn-small', 'Dismiss');
    close.type = 'button';
    close.setAttribute('aria-label', `Dismiss the notice about ${notice.host}`);
    close.addEventListener('click', () => dismissNotice(notice.key));
    actions.append(close);
    card.append(actions);
    box.append(card);
  }
}

function overlay(title, text = '', spinner = false) {
  $('overlay').hidden = false;
  $('overlay-title').textContent = title;
  $('overlay-text').textContent = text;
  $('overlay-spinner').hidden = !spinner;
}

function renderOverlay() {
  if (state.ended === 'expired') {
    return overlay('This link has expired', 'Ask the agent for a new one with browser_view.');
  }
  if (state.ended === 'gone') {
    return overlay('This lane was removed', 'There is nothing left to show here.');
  }
  if (state.ended === 'frozen') {
    return overlay('This lane is frozen', 'Agents and their links are stopped here. To watch or take over, open the lane in your panel.');
  }
  if (state.ended === 'revoked') {
    return overlay('This link was revoked', 'Ask the agent for a new one with browser_view.');
  }
  if (state.lane.state === 'quarantined') {
    return overlay('The lane is quarantined', state.lane.quarantine_reason || 'It failed its network self-check.');
  }
  if (!running()) {
    if (state.lane.state === 'warming') return overlay('The lane is starting', 'The live view appears in a moment.', true);
    return overlay('The lane is not running', 'Its browser starts when the agent uses it. This view connects on its own.');
  }
  if (state.connected) {
    $('overlay').hidden = true;
    return undefined;
  }
  if (state.attempts > 1) return overlay('Reconnecting', 'The live view dropped. Trying again.', true);
  return overlay('Connecting', 'Opening the live view.', true);
}

function renderActions() {
  const list = $('activity-list');
  list.replaceChildren();
  for (const action of state.actions.slice(0, 60)) {
    const li = document.createElement('li');
    li.className = 'act';
    li.dataset.status = action.status || '';
    const row = document.createElement('div');
    row.className = 'act-row';
    const dot = document.createElement('span');
    dot.className = 'act-dot';
    dot.setAttribute('aria-hidden', 'true');
    const tool = document.createElement('span');
    tool.className = 'act-tool';
    tool.textContent = action.tool || 'action';
    const time = document.createElement('time');
    time.className = 'act-time';
    time.dateTime = action.at || '';
    time.textContent = clock(action.at, true);
    row.append(dot, tool, time);
    li.append(row);
    const detail = [action.summary, action.status && action.status !== 'ok' ? action.status : null]
      .filter(Boolean).join(' · ');
    if (detail) {
      const p = document.createElement('p');
      p.className = 'act-summary';
      p.textContent = detail;
      li.append(p);
    }
    li.setAttribute('aria-label', `${action.tool || 'action'} at ${clock(action.at, true)}${detail ? `: ${detail}` : ''}`);
    list.append(li);
  }
  const empty = $('activity-empty');
  empty.hidden = state.actions.length > 0;
  empty.textContent = state.actionsAvailable
    ? 'No agent actions on this lane yet.'
    : 'Action history is not available on this node yet.';
}

// ---- live view ----

function wsUrl() {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  return `${scheme}://${location.host}${withToken(boot.paths.ws)}`;
}

function connect() {
  clearTimeout(state.retryTimer);
  if (state.rfb || state.ended || !running()) {
    render();
    return;
  }
  state.attempts += 1;
  let rfb;
  try {
    rfb = new RFB($('screen'), wsUrl(), { shared: true, wsProtocols: ['binary'] });
  } catch (err) {
    console.warn('live view failed to start', err);
    scheduleReconnect();
    return;
  }
  rfb.scaleViewport = true;
  rfb.resizeSession = false;
  rfb.clipViewport = false;
  rfb.qualityLevel = 6;
  rfb.compressionLevel = 2;
  rfb.background = 'transparent';
  rfb.viewOnly = !interactive();
  rfb.focusOnClick = interactive();
  rfb.addEventListener('connect', () => {
    state.connected = true;
    state.attempts = 0;
    render();
    if (interactive()) rfb.focus();
  });
  rfb.addEventListener('disconnect', () => {
    if (state.rfb !== rfb) return;
    state.rfb = null;
    state.connected = false;
    scheduleReconnect();
  });
  rfb.addEventListener('credentialsrequired', () => rfb.disconnect());
  state.rfb = rfb;
  render();
}

function disconnect() {
  const rfb = state.rfb;
  state.rfb = null;
  state.connected = false;
  if (rfb) rfb.disconnect();
}

function scheduleReconnect() {
  clearTimeout(state.retryTimer);
  render();
  if (state.ended || !running()) return;
  const delay = RETRY_DELAYS[Math.min(state.attempts, RETRY_DELAYS.length - 1)];
  state.retryTimer = setTimeout(connect, delay);
}

// ---- paste from this computer into the lane, and copy from the lane to this computer ----
//
// While you have control, pasting with the keyboard (or the "Paste text" button) types this
// computer's clipboard text into the lane. On a Mac the Command key works as Ctrl for C, X and A,
// because the lane is a Linux browser, so copy and paste inside the lane use the keys a Mac user
// knows. Everything goes through the relay, which drops it unless control is human.
//
// Copying out: x11vnc and the lane's browser do not exchange selections, so the VNC clipboard
// messages carry nothing in either direction. Instead, a moment after a copy or cut made in the
// lane with the keyboard, or on a click of "Copy from the lane" (for a page's own Copy button or the
// right-click menu, which the keyboard never tells us about), the page asks the node for the lane's
// clipboard (POST .../clipboard, only for a person in control) and puts it on this computer's. Only
// the person's own key or click starts that, never a timer, so a page in the lane cannot push text
// onto this computer's clipboard by itself.
//
// There are two clipboards, this computer's and the lane's, and the newest copy wins: after a copy
// made in the lane (with the keyboard, or brought over with the button), pasting pastes the lane's
// own clipboard, until this page loses focus (the only time this computer's clipboard can change).
// A copy brought over puts the same text on both, so the rule holds either way.

const XK = { Control_L: 0xffe3, Control_R: 0xffe4, Shift_L: 0xffe1, Alt_L: 0xffe9, Return: 0xff0d, v: 0x76 };
const TYPE_LIMIT = 5000;  // longer text is not typed key by key
const CMD_AS_CTRL = { KeyC: 0x63, KeyX: 0x78, KeyA: 0x61 };
const cmd = { held: false, sent: false };  // a Mac's Command key: kept from the lane until a combination needs it there
let laneCopyAt = 0;          // when the keyboard last copied or cut in the lane
let leftPageAt = Date.now(); // when this page last lost focus, or loaded

const onScreen = (el) => el instanceof Node && $('screen').contains(el);

// A Command key already passed on to the lane (as Alt) must be up before keys are sent for the person.
function liftCmd(rfb) {
  if (cmd.sent) rfb.sendKey(XK.Alt_L, 'AltLeft', false);
  cmd.sent = false;
}

function sendCtrl(keysym, code) {
  const rfb = state.rfb;
  if (!rfb || !interactive()) return;
  liftCmd(rfb);
  rfb.sendKey(XK.Control_L, 'ControlLeft', true);
  rfb.sendKey(keysym, code);
  rfb.sendKey(XK.Control_L, 'ControlLeft', false);
}

// Pasting types the text into the lane, key by key, so every letter arrives ("š", "ė", "€").
// A line break is typed as Shift+Enter, which is a new line in a text box and in a chat box, and
// never sends the message the way Enter would.
function typeIntoLane(rfb, text) {
  for (const ch of text.replace(/\r\n?/g, '\n')) {
    const point = ch.codePointAt(0);
    if (ch === '\n') {
      rfb.sendKey(XK.Shift_L, 'ShiftLeft', true);
      rfb.sendKey(XK.Return, 'Enter');
      rfb.sendKey(XK.Shift_L, 'ShiftLeft', false);
    } else if (ch === '\t') rfb.sendKey(0x20, 'Space');  // a Tab key would move to the next field
    else if (point >= 0x20 && point !== 0x7f) rfb.sendKey(keysyms.lookup(point), '');
  }
}

function pasteToLane(text) {
  const rfb = state.rfb;
  if (!text || !rfb || !interactive()) return;
  liftCmd(rfb);
  if (!isMac) {  // the Ctrl of Ctrl+V is still down in the lane: typed under it, every letter would be a shortcut
    rfb.sendKey(XK.Control_L, 'ControlLeft', false);
    rfb.sendKey(XK.Control_R, 'ControlRight', false);
  }
  if (text.length > TYPE_LIMIT) {
    toast(`That is too long to paste here (over ${TYPE_LIMIT} characters). Paste it in parts.`, 'error');
    return;
  }
  typeIntoLane(rfb, text);
}

function releaseCmd() {
  if (state.rfb) liftCmd(state.rfb);
  cmd.held = false;
  cmd.sent = false;
}

const COPY_SETTLE_MS = 300;  // after Ctrl+C the lane's browser needs a moment to own the clipboard
const COPY_REFUSED = 'This browser did not let the page use your clipboard. Press "Copy from the lane" in the bar under the view.';

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// What the lane copied: the text, or an error with a code ('nothing' when the lane has no text copied).
async function readLaneClipboard(settle) {
  if (settle) await sleep(settle);
  const data = await post(boot.paths.clipboard);
  const text = typeof data?.text === 'string' ? data.text : '';
  if (!text) throw Object.assign(new Error('Nothing is copied in the lane.'), { code: 'nothing' });
  return text;
}

// The last way to the clipboard, where the Clipboard API is missing (an http address that is not localhost).
function execCopy(text) {
  const area = document.createElement('textarea');
  area.value = text;
  area.setAttribute('readonly', '');
  area.style.position = 'fixed';
  area.style.opacity = '0';
  document.body.append(area);
  area.select();
  let ok = false;
  try { ok = document.execCommand('copy'); } catch { ok = false; }
  area.remove();
  state.rfb?.focus();
  if (!ok) throw new Error('refused');
}

function copyFailed(err) {
  if (err?.code === 'nothing') toast('Nothing is copied in the lane.');
  else if (err?.code === 'presence_required') {
    toast('Only a person can copy from the lane. Sign in to the panel in this browser (the same address, at /login), then copy again.', 'error');
  } else if (err?.code) toast(err.message, 'error');  // the node's own words, with what to do (xclip missing, say)
  else toast(COPY_REFUSED, 'error');
}

// Bring the lane's clipboard to this computer's. The write starts here, inside the key or click
// handler, with the text still on its way (a ClipboardItem takes a promise): Safari allows a write
// only from there. Elsewhere, or when that form is refused, it falls back to fetch, then writeText.
function copyFromLane(settle = 0) {
  if (!boot.paths.clipboard || !interactive() || !state.rfb) return;
  let failure = null;  // why no text came, when none did
  const text = readLaneClipboard(settle).catch((err) => {
    failure = err.code ? err : Object.assign(new Error(err instanceof TypeError
      ? 'The node did not answer, so nothing was copied. Copy again in a moment.' : err.message), { code: 'node' });
    throw failure;
  });
  text.catch(() => {});  // handled below; no unhandled rejection while the write still holds it
  const copied = () => {
    laneCopyAt = Date.now();  // the lane's copy is the newest one now, on both clipboards
    toast('Copied to this computer.');
  };
  let write;
  try {
    write = window.ClipboardItem && navigator.clipboard?.write
      ? navigator.clipboard.write([new ClipboardItem({ 'text/plain': text.then((t) => new Blob([t], { type: 'text/plain' })) })])
      : Promise.reject(new Error('no promise form'));
  } catch (err) {
    write = Promise.reject(err);
  }
  write.then(copied, async () => {
    let got;
    try { got = await text; } catch { copyFailed(failure); return; }
    try {
      if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(got);
      else execCopy(got);
      copied();
    } catch {
      try { execCopy(got); copied(); } catch { copyFailed(null); }
    }
  });
}

// Capture phase on window: before noVNC's keyboard handler, which would swallow the paste.
window.addEventListener('keydown', (e) => {
  if (!interactive() || !state.rfb || !onScreen(document.activeElement)) return;
  if (isMac && (e.code === 'MetaLeft' || e.code === 'MetaRight')) {
    cmd.held = true;  // not sent yet: Command+C is a copy, not the lane's Alt+C
    e.stopPropagation();
    return;
  }
  const mod = isMac ? e.metaKey && !e.ctrlKey : e.ctrlKey && !e.metaKey;
  const plain = mod && !e.altKey && !e.shiftKey;
  if (plain && e.code === 'KeyV') {
    e.stopPropagation();  // no preventDefault: the browser's own paste follows, and the listener below takes it
    return;
  }
  if (plain && (e.code === 'KeyC' || e.code === 'KeyX')) {
    laneCopyAt = Date.now();
    if (e.isTrusted && !e.repeat) copyFromLane(COPY_SETTLE_MS);  // the person's own key, never a page's
  }
  if (!isMac || !e.metaKey) return;
  if (plain && CMD_AS_CTRL[e.code]) {
    e.preventDefault();
    e.stopPropagation();
    sendCtrl(CMD_AS_CTRL[e.code], e.code);
    return;
  }
  if (!cmd.sent) {  // any other Command combination reaches the lane as it did before, as Alt
    state.rfb.sendKey(XK.Alt_L, 'AltLeft', true);
    cmd.sent = true;
  }
}, true);

window.addEventListener('keyup', (e) => {
  if (!isMac || (e.code !== 'MetaLeft' && e.code !== 'MetaRight') || !(cmd.held || cmd.sent)) return;
  e.stopPropagation();
  releaseCmd();
}, true);

window.addEventListener('blur', () => {
  leftPageAt = Date.now();
  releaseCmd();
});

document.addEventListener('paste', (e) => {
  if (!interactive() || !state.rfb || !onScreen(document.activeElement)) return;  // a text box of this page pastes as usual
  e.preventDefault();
  if (laneCopyAt > leftPageAt) sendCtrl(XK.v, 'KeyV');  // the lane's own copy is the newest one
  else pasteToLane(e.clipboardData?.getData('text/plain') || '');
}, true);

// ---- server state ----

function applyLane(lane) {
  const wasRunning = running();
  state.lane = lane;
  state.control = lane.control;
  if (running() && !wasRunning) {
    state.attempts = 0;
    connect();
  } else if (!running() && wasRunning) {
    disconnect();
  }
  render();
}

function applyControl(control) {
  const before = interactive();
  state.control = control;
  state.lane = { ...state.lane, control };
  render();
  if (interactive() && !before) {
    toast('You have control. The agent waits until you hand it back.');
    state.rfb?.focus();
  } else if (!interactive() && before) {
    releaseCmd();
    state.rfb?.blur();
    if (controlState() === 'agent') {
      toast(control.by === 'idle' ? 'Control went back to the agent after a while without input.' : 'The agent has control again.');
    }
  }
}

function openEvents() {
  state.events?.close();
  if (state.ended) return;
  const es = new EventSource(withToken(boot.paths.events));
  state.events = es;
  es.addEventListener('hello', (e) => {
    const data = JSON.parse(e.data);
    applyLane(data.lane);
    state.actionsAvailable = data.actions !== null;
    state.actions = data.actions || [];
    renderActions();
  });
  es.addEventListener('control', (e) => applyControl(JSON.parse(e.data).control));
  es.addEventListener('lane', (e) => applyLane(JSON.parse(e.data)));
  es.addEventListener('egress', (e) => addNotice(JSON.parse(e.data)));
  es.addEventListener('action', (e) => {
    const action = JSON.parse(e.data);
    state.actions = [action, ...state.actions.filter((a) => a.id !== action.id)].slice(0, 100);
    renderActions();
  });
  es.addEventListener('actions', (e) => {
    state.actions = JSON.parse(e.data).actions || [];
    renderActions();
  });
  es.addEventListener('ended', (e) => {
    const reason = String(JSON.parse(e.data).reason || '');
    state.ended = reason.startsWith('frozen') ? 'frozen' : reason.includes('revoked') ? 'revoked' : 'expired';
    es.close();
    disconnect();
    render();
  });
  es.addEventListener('gone', () => {
    state.ended = 'gone';
    es.close();
    disconnect();
    render();
  });
  es.onerror = () => {
    if (es.readyState !== EventSource.CLOSED) return; // the browser retries on its own
    if (Date.now() >= state.expiresAt) {
      state.ended = 'expired';
      disconnect();
      render();
      return;
    }
    setTimeout(() => { if (state.events === es) openEvents(); }, 4000);
  };
}

async function post(path, body) {
  const res = await fetch(withToken(path), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
    cache: 'no-store',
  });
  let data = null;
  try { data = await res.json(); } catch { /* not JSON */ }
  if (!res.ok) {
    const message = data?.error?.message || `The node answered ${res.status}.`;
    const err = new Error(message);
    err.code = data?.error?.code;
    throw err;
  }
  return data;
}

// The owner proved themselves with their panel session: from here the page works on their own link,
// and the live view reconnects on it so their input reaches a lane a captcha holds.
function adoptOwnerLink(own) {
  if (!own?.token) return;
  state.token = own.token;
  state.expiresAt = Date.parse(own.expires_at);
  state.person = true;
  boot.owner = true;
  history.replaceState(null, '', withToken(boot.paths.page));
  disconnect();
  connect();
}

async function act(action, done) {
  if (!canControl() || state.busy) return;
  state.busy = true;
  render();
  try {
    const data = await post(boot.paths.control, { action });
    adoptOwnerLink(data.owner_link);
    applyLane(data.lane);
    if (done) toast(done);
  } catch (err) {
    if (err.code === 'presence_required') {
      state.person = false;
      toast('Sign in to the panel in this browser first (the same address, at /login), then press again.', 'error');
    } else toast(err.message, 'error');
  } finally {
    state.busy = false;
    render();
  }
}

const take = () => { if (controlState() !== 'human') act('take'); };
const handBack = () => { if (controlState() !== 'agent') act('hand_back', 'Handed back. The agent continues.'); };
const keepControl = () => act('deny_request', 'You keep control. The agent was told no.');
const confirmStop = () => { if (canControl() && controlState() !== 'paused') $('dlg-stop').showModal(); };

async function renew() {
  try {
    const data = await post(boot.paths.renew);
    state.token = data.token;
    state.expiresAt = Date.parse(data.expires_at);
    history.replaceState(null, '', withToken(boot.paths.page));
  } catch (err) {
    console.warn('could not renew the viewer link', err);
  }
  scheduleRenew();
}

function scheduleRenew() {
  const left = state.expiresAt - Date.now();
  if (left <= 0) return;
  setTimeout(renew, Math.max(20000, left / 2));
}

// ---- controls, theme, shortcuts ----

const THEMES = ['auto', 'light', 'dark'];
const THEME_NAMES = { auto: 'automatic', light: 'light', dark: 'dark' };

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  $('btn-theme').setAttribute('aria-label', `Theme: ${THEME_NAMES[theme]}`);
  $('btn-theme').title = `Theme: ${THEME_NAMES[theme]}`;
  try { localStorage.setItem('agentos.viewer.theme', theme); } catch { /* private mode */ }
}

// The activity panel starts closed; a browser remembers it open once the person opens it.
// (A new key: the old one was written "open" on every visit, so it holds no real choice.)
const ACTIVITY_KEY = 'agentos.viewer.activity-panel';

function setActivity(open, remember = true) {
  app.dataset.activity = open ? 'open' : 'closed';
  $('btn-activity').setAttribute('aria-expanded', String(open));
  $('btn-activity').title = open ? 'Hide agent activity' : 'Show agent activity';
  if (!remember) return;
  try { localStorage.setItem(ACTIVITY_KEY, open ? 'open' : 'closed'); } catch { /* private mode */ }
}

function toggleFullscreen() {
  if (document.fullscreenElement) document.exitFullscreen?.();
  else $('stage').requestFullscreen?.().catch(() => toast('Full screen is not available here.', 'error'));
}

function openHelp() {
  if (!$('dlg-help').open) $('dlg-help').showModal();
}

const SHORTCUTS = {
  KeyT: take,
  KeyH: handBack,
  KeyS: confirmStop,
  KeyA: () => setActivity(app.dataset.activity !== 'open'),
  KeyF: toggleFullscreen,
  Slash: openHelp,
};

// Capture phase on window: runs before noVNC's keyboard handler, so the combination
// never reaches the lane.
window.addEventListener('keydown', (e) => {
  if (!e.altKey || !e.shiftKey || e.ctrlKey || e.metaKey) return;
  const fn = SHORTCUTS[e.code];
  if (!fn) return;
  e.preventDefault();
  e.stopPropagation();
  fn();
}, true);

function wire() {
  $('btn-take').addEventListener('click', take);
  $('btn-handback').addEventListener('click', handBack);
  $('btn-stop').addEventListener('click', confirmStop);
  $('btn-request-handback').addEventListener('click', handBack);
  $('btn-request-keep').addEventListener('click', keepControl);

  $('btn-stop-cancel').addEventListener('click', () => $('dlg-stop').close());
  $('btn-stop-confirm').addEventListener('click', () => {
    $('dlg-stop').close();
    act('stop_agent', 'The agent is stopped on this lane.');
  });

  $('btn-paste').addEventListener('click', () => {
    $('paste-text').value = '';
    $('dlg-paste').showModal();
    $('paste-text').focus();
  });
  $('btn-paste-cancel').addEventListener('click', () => $('dlg-paste').close());
  $('btn-paste-send').addEventListener('click', () => {
    const text = $('paste-text').value;
    $('dlg-paste').close();
    if (text && state.rfb && interactive()) {
      state.rfb.focus();
      pasteToLane(text);
    }
  });

  $('btn-copy-out').addEventListener('click', (e) => {
    if (!e.isTrusted) return;
    copyFromLane();
    state.rfb?.focus();  // keys go back to the lane
  });

  $('btn-help').addEventListener('click', openHelp);
  $('btn-help-close').addEventListener('click', () => $('dlg-help').close());
  $('btn-activity').addEventListener('click', () => setActivity(app.dataset.activity !== 'open'));
  $('btn-fullscreen').addEventListener('click', toggleFullscreen);
  $('btn-theme').addEventListener('click', () => {
    const now = document.documentElement.dataset.theme || 'auto';
    setTheme(THEMES[(THEMES.indexOf(now) + 1) % THEMES.length]);
  });

  for (const kbd of document.querySelectorAll('kbd[data-mod]')) {
    kbd.textContent = isMac ? 'Option Shift' : 'Alt Shift';
  }
  const pasteKey = isMac ? 'Command+V' : 'Ctrl+V';
  const copyKey = isMac ? 'Command+C' : 'Ctrl+C';
  $('help-clipboard').textContent = `While you have control, ${copyKey} in the lane also copies to this computer, and ${pasteKey} pastes into the lane: what you copied last, here or in the lane. When a page's own Copy button or the right-click menu copied something, press "Copy from the lane" in the bar under the view.`;

  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible' && !state.rfb && running()) {
      state.attempts = 0;
      connect();
    }
  });
}

function start() {
  let theme = 'auto';
  let activity = 'closed';
  try {
    theme = localStorage.getItem('agentos.viewer.theme') || theme;
    activity = localStorage.getItem(ACTIVITY_KEY) || activity;
  } catch { /* private mode */ }
  setTheme(THEMES.includes(theme) ? theme : 'auto');
  setActivity(activity === 'open', false);
  wire();
  render();
  renderActions();
  if (Date.now() >= state.expiresAt) {
    state.ended = 'expired';
    render();
    return;
  }
  openEvents();
  connect();
  scheduleRenew();
  setInterval(() => {
    if (!state.ended && Date.now() >= state.expiresAt && !(state.events && state.events.readyState === EventSource.OPEN)) {
      state.ended = 'expired';
      disconnect();
      render();
    }
  }, 15000);
}

start();
