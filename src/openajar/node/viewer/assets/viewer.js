// openajar lane viewer: live view, take over, hand back, stop agent.
//
// The server decides everything that matters: the relay drops this page's input unless
// the lane's control is "human" and the link is a control link. The page only mirrors
// that state (viewOnly) so it does not send input that would be dropped anyway.

import RFB from './vendor/novnc/core/rfb.js';
import keysyms from './vendor/novnc/core/input/keysymdef.js';
import { Clip, LIMIT_S, clipName, elapsed, recordFormat, save } from './record.js';

const boot = JSON.parse(document.getElementById('openajar-boot').textContent);
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
  said: null, // the mode text last said to screen readers
  phase: null, // what the card shows: connecting, live, reconnecting, idle or ended
  notices: [], // off-list documents while a human has control (let through, agents would be refused), newest first
};

const RETRY_DELAYS = [1000, 2000, 4000, 8000, 10000];
// Tight with JPEG at this quality, and this much zlib. Measured on a local link (docs/LOCAL.md,
// "The live view"): levels from 2 to 9 moved the frame rate by a few frames at most, because x11vnc's
// encoding is the limit, so these keep a sharp picture at a moderate bit rate.
const QUALITY = 6;
const COMPRESSION = 2;
const FIRST_FRAME_MS = 150; // noVNC says connect after the handshake; the first frame lands just after
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
const SCREEN_LABEL = ['Ready', 'ok']; // the desktop runs, the browser not (yet)
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
// an environment's desktop outlives its browser: a lane whose browser is cold or starting can still be live
const running = () => state.lane.state === 'warm' || state.lane.state === 'active'
  || (Boolean(state.lane.live) && state.lane.state !== 'quarantined');

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

// Who has control, in plain words: [name in bold, the rest]. The agent's name is the one its MCP
// client gave the node (boot.agent), never a guess.
function whoWords() {
  const c = state.control || {};
  const by = c.by && c.by !== 'unknown' ? c.by : null;
  switch (controlState()) {
    case 'human':
      if (canControl() && by === boot.me) return ['You', ' have control'];
      return by ? [capitalize(by), ' has control'] : ['Someone', ' has control'];
    case 'paused':
      return [boot.agent || 'The agent', by ? ` is stopped by ${by}` : ' is stopped'];
    default:
      return [boot.agent || 'The agent', ' has control'];
  }
}

function whoText() {
  return whoWords().join('');
}

function untilText() {
  if (!Number.isFinite(state.expiresAt)) return '';
  return `works until ${clock(new Date(state.expiresAt).toISOString())}`;
}

function render() {
  const c = controlState();
  const changed = app.dataset.control !== c;
  app.dataset.control = c;
  app.dataset.mode = state.mode;
  app.dataset.interactive = String(interactive());

  const [ws, name] = state.lane.id.split('/');
  $('lane-ws').textContent = boot.title ? '' : ws;
  $('lane-id').classList.toggle('titled', Boolean(boot.title));
  $('lane-name').textContent = boot.title || name;

  const screenOnly = state.lane.live && (state.lane.state === 'cold' || state.lane.state === 'warming');
  const [label, tone] = screenOnly ? SCREEN_LABEL : STATE_LABELS[state.lane.state] || [state.lane.state, 'muted'];
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
      ? `Websites see this browser coming from ${exitIp}, through the proxy ${proxy}.`
      : `This browser goes out through the proxy ${proxy}. Its address shows here after its next check.`;
  } else {
    exitText = exitIp ? `Exit IP ${exitIp} · no proxy` : 'No proxy';
    exitTitle = 'This browser has no proxy, so websites see the address of the computer it runs on.';
  }
  $('exit-label').textContent = exitText;
  $('exit-chip').title = exitTitle;
  $('exit-info').textContent = exitTitle;

  // the pill: who has control, the name in bold
  const [who, rest] = whoWords();
  const strong = document.createElement('strong');
  strong.textContent = who;
  $('who-text').replaceChildren(strong, rest);
  const since = state.control?.since;
  $('who-since').textContent = since && c !== 'agent' ? `since ${clock(since)}` : '';
  $('who-since').title = since ? new Date(since).toLocaleString() : '';
  $('who-tag').hidden = canControl() || Boolean(state.ended);
  if (changed && state.phase) {  // a short rise of the words when control changes hands
    $('who').classList.remove('changed');
    void $('who').offsetWidth;
    $('who').classList.add('changed');
  }

  // for screen readers: what this page's input does now (the pill's own words are announced too)
  let said;
  if (interactive()) said = 'You have control. Your mouse and keyboard go to the screen.';
  else if (!canControl()) said = 'View only. This link can watch but not take over.';
  else if (c === 'human' && !state.person) said = 'View only. Sign in to the panel here to use the screen.';
  else if (c === 'paused') said = 'View only. The agent is stopped.';
  else if (c === 'human') said = 'View only. Someone else has control.';
  else said = 'View only. The agent has control.';
  if (said !== state.said) {
    state.said = said;
    $('mode-status').textContent = said;
  }

  // actions: one primary at a time
  const ended = Boolean(state.ended);
  $('btn-take').hidden = !canControl() || c === 'human' || ended;
  $('btn-handback').hidden = !canControl() || c === 'agent' || ended;
  $('btn-stop').hidden = !canControl() || c === 'paused' || ended;
  $('btn-take').classList.toggle('btn-primary', c !== 'paused');
  $('btn-paste').hidden = !interactive() || !state.connected;
  $('btn-keyboard').hidden = !interactive() || !state.connected || !touchFirst();
  if ($('btn-keyboard').hidden && document.activeElement === $('kbd-input')) $('kbd-input').blur();
  $('btn-copy-out').hidden = !interactive() || !state.connected || !boot.paths.clipboard;
  $('actions').hidden = !canControl() || ended;
  for (const id of ['btn-take', 'btn-handback', 'btn-stop', 'btn-request-handback', 'btn-request-keep']) {
    $(id).disabled = state.busy;
  }

  // the line under the pill: only what the pill does not say already
  let hint;
  if (ended) hint = '';
  else if (!canControl()) hint = 'You can watch, but this link cannot take over.';
  else if (!state.person && (c === 'paused' || state.control?.reason)) {
    hint = 'Sign in to the panel in this browser (the same address, at /login), then take over and hand it back. An agent may hold this link, so it cannot do that alone.';
  } else if (!state.person && c === 'human') {
    hint = 'Agents are kept off the screen. To click or type in it, sign in to the panel in this browser (the same address, at /login), because an agent may hold this link.';
  } else if (c === 'human') {
    hint = state.lane.state === 'active'
      ? 'The agent is finishing its current step. Then the screen is all yours.'
      : touchFirst()
        ? `A tap on the screen is a click, and the keyboard button types. ${boot.agent || 'The agent'} gets it back after ${minutes(boot.control_idle_seconds)} without input.`
        : `Your mouse and keyboard go to the screen. ${boot.agent || 'The agent'} gets it back after ${minutes(boot.control_idle_seconds)} without input.`;
    // an environment is the agent's own computer: its tools stay off the screen now, but its commands run as
    // root there (docs/THREAT-MODEL.md, "An environment's desktop")
    if (state.lane.state !== 'active' && String(state.lane.id || '').startsWith('env/')) {
      hint += ' This is the agent\'s computer: what you type here, the agent could read.';
    }
  } else if (c === 'paused') hint = 'The agent is stopped. Take over to use the screen, or hand it back to let the agent continue.';
  else hint = '';
  if ($('hint').textContent !== hint) $('hint').textContent = hint;

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

  if (ended && clip) clip.stop('ended');
  $('btn-record').hidden = !RECORD_FORMAT || (ended && !clip);

  const until = untilText();
  $('link-info').textContent = `${boot.node} · ${canControl() ? 'control link' : 'view-only link'}${until ? ` · ${until}` : ''}`;
  renderOverlay();
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
    toast(notice.rule === 'noted' ? `Added ${notice.allow} to this browser's site list for agents.`
      : `Added ${notice.allow} to this browser's site list. Reload the page to load it.`);
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

function setPhase(phase) {
  if (state.phase === phase) return;
  state.phase = phase;
  app.dataset.phase = phase;
}

function overlay(title, text = '', spinner = false, phase = 'idle') {
  setPhase(phase);
  $('overlay').hidden = false;
  $('overlay-title').textContent = title;
  $('overlay-text').textContent = text;
  $('overlay-spinner').hidden = !spinner;
}

function renderOverlay() {
  if (state.ended === 'expired') {
    return overlay('This link has expired', 'Ask the agent for a new one with browser_view.', false, 'ended');
  }
  if (state.ended === 'gone') {
    return overlay('This screen was removed', 'There is nothing left to show here.', false, 'ended');
  }
  if (state.ended === 'frozen') {
    return overlay('This screen is frozen', 'Agents and their links are stopped here. To watch or take over, open it in your panel.', false, 'ended');
  }
  if (state.ended === 'revoked') {
    return overlay('This link was revoked', 'Ask the agent for a new one with browser_view.', false, 'ended');
  }
  if (state.lane.state === 'quarantined') {
    return overlay('The lane is quarantined', state.lane.quarantine_reason || 'It failed its network self-check.');
  }
  if (!running()) {
    if (state.lane.state === 'warming') return overlay('Starting the screen', 'The live view appears in a moment.', true, 'connecting');
    return overlay('The screen is asleep', 'It wakes when the agent uses it, and this view connects on its own.');
  }
  if (state.connected && state.drawn) {
    setPhase('live');
    return undefined;
  }
  if (state.attempts > 1) return overlay('Reconnecting', 'The live view dropped for a moment. Trying again.', true, 'reconnecting');
  return overlay(boot.agent ? `Connecting to ${boot.agent}'s screen` : "Connecting to the agent's screen", 'The live view opens in a moment.', true, 'connecting');
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
    ? 'No agent actions here yet.'
    : 'Action history is not available on this node yet.';
}

// ---- the card: the stream's own shape, as large as the stage allows ----
//
// Sized in px from the stream's size and the room there is, on a resize only (never per frame).
// Rounded to whole device pixels so the picture is not resampled twice. noVNC then scales its
// canvas to the card (scaleViewport), so a stream of any size and shape fits.

const fb = { w: 1920, h: 1080 };  // the usual screen until the stream says its own size
let layoutQueued = false;

function layoutCard() {
  layoutQueued = false;
  const frame = $('frame');
  const card = $('screen-wrap');
  const dock = $('actionbar');
  const cs = getComputedStyle(frame);
  const row = cs.flexDirection === 'row';
  const gap = parseFloat(row ? cs.columnGap : cs.rowGap) || 0;
  const w = frame.clientWidth - parseFloat(cs.paddingLeft) - parseFloat(cs.paddingRight) - (row ? dock.offsetWidth + gap : 0);
  const h = frame.clientHeight - parseFloat(cs.paddingTop) - parseFloat(cs.paddingBottom) - (row ? 0 : dock.offsetHeight + gap);
  if (w <= 0 || h <= 0) return;
  const scale = Math.min(w / fb.w, h / fb.h);
  const dpr = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.floor(fb.w * scale * dpr) / dpr);
  const height = Math.max(1, Math.floor(fb.h * scale * dpr) / dpr);
  card.style.width = `${width}px`;
  card.style.height = `${height}px`;
  card.dataset.sized = '';
  // the agent's pointer: big on a big card, smaller on a phone, never tiny
  $('agent-cursor').style.setProperty('--cursor-scale', String(Math.min(1, Math.max(0.62, width / 1552))));
  placeCursor(true);
}

// ---- the agent's pointer ----

// Where the agent acts in its browser, in the screen's pixels, from the event stream (the node knows where it
// aims; the machine says where the browser's window is). Drawn over the card and moved with a transform only;
// it fades after a few quiet seconds, and hides while a person has control (CSS) or the agent is elsewhere.
const CURSOR_FADE_MS = 4500;
const cursor = { x: 0, y: 0, w: 1920, h: 1080, shown: false, timer: null };

function placeCursor(jump = false) {
  const el = $('agent-cursor');
  const k = $('screen-wrap').clientWidth / cursor.w;
  if (!Number.isFinite(k) || k <= 0) return;
  if (jump) {
    el.dataset.jump = 'true';
    requestAnimationFrame(() => requestAnimationFrame(() => { el.dataset.jump = 'false'; }));
  }
  el.style.setProperty('--x', `${Math.round(cursor.x * k)}px`);
  el.style.setProperty('--y', `${Math.round(cursor.y * k)}px`);
}

function hideCursor() {
  clearTimeout(cursor.timer);
  cursor.shown = false;
  $('agent-cursor').dataset.shown = 'false';
}

function onPointer(p) {
  if (!p || p.hide) {
    hideCursor();
    return;
  }
  const nums = [p.x, p.y, p.w, p.h];
  if (!nums.every(Number.isFinite) || p.w <= 0 || p.h <= 0 || p.x < 0 || p.y < 0 || p.x > p.w || p.y > p.h) return;
  const el = $('agent-cursor');
  Object.assign(cursor, { x: p.x, y: p.y, w: p.w, h: p.h });
  placeCursor(!cursor.shown);  // a pointer that was gone appears where it is, without a glide from before
  cursor.shown = true;
  el.dataset.shown = 'true';
  if (p.kind === 'press') {
    el.dataset.press = 'false';
    void el.offsetWidth;
    el.dataset.press = 'true';
  }
  clearTimeout(cursor.timer);
  cursor.timer = setTimeout(hideCursor, CURSOR_FADE_MS);
}

function queueLayout() {
  if (layoutQueued) return;
  layoutQueued = true;
  requestAnimationFrame(layoutCard);
}

// The far screen can change size (Builder D's desktop, a phone-sized one): the canvas follows it,
// and so does the card. A MutationObserver on the canvas's own size, not a poll.
let frameWatch = null;
function watchFrameSize(rfb) {
  frameWatch?.disconnect();
  const canvas = $('screen').querySelector('canvas');
  if (!canvas || state.rfb !== rfb) return;
  const read = () => {
    if (!canvas.width || !canvas.height || (canvas.width === fb.w && canvas.height === fb.h)) return;
    fb.w = canvas.width;
    fb.h = canvas.height;
    queueLayout();
  };
  read();
  frameWatch = new MutationObserver(read);
  frameWatch.observe(canvas, { attributes: true, attributeFilter: ['width', 'height'] });
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
  // noVNC scales the picture to the card (the card is sized to the stream in layoutCard), never
  // resizes the far screen, and draws frames as they come: nothing is copied or re-encoded here.
  rfb.scaleViewport = true;
  rfb.resizeSession = false;
  rfb.clipViewport = false;
  rfb.qualityLevel = QUALITY;
  rfb.compressionLevel = COMPRESSION;
  rfb.background = 'transparent';
  rfb.viewOnly = !interactive();
  rfb.focusOnClick = interactive();
  let firstFrame = null;
  rfb.addEventListener('connect', () => {
    state.connected = true;
    state.attempts = 0;
    watchFrameSize(rfb);
    render();
    // the picture fades in once its first frame has landed, not over a black box
    firstFrame = setTimeout(() => {
      if (state.rfb !== rfb) return;
      state.drawn = true;
      render();
    }, FIRST_FRAME_MS);
    if (interactive()) rfb.focus();
  });
  rfb.addEventListener('disconnect', () => {
    clearTimeout(firstFrame);
    if (state.rfb !== rfb) return;
    state.rfb = null;
    state.connected = false;
    state.drawn = false;
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
  state.drawn = false;
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

const XK = { Control_L: 0xffe3, Control_R: 0xffe4, Shift_L: 0xffe1, Alt_L: 0xffe9, Return: 0xff0d, BackSpace: 0xff08, v: 0x76 };
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

// ---- a phone's keyboard ----
// A touch screen has no keys: the keyboard button focuses a field the size of a pixel, which opens the phone's
// own keyboard, and what is typed there goes to the screen as keys. The field always holds KBD_PAD, so a
// backspace has something to delete and is seen; each character added is typed, each removed is a Backspace.
const KBD_PAD = '________';
const touchFirst = () => window.matchMedia('(pointer: coarse)').matches;

function kbdReset(el) {
  el.value = KBD_PAD;
  el.setSelectionRange(KBD_PAD.length, KBD_PAD.length);
}

function kbdInput(e) {
  const el = e.target;
  if (e.isComposing) return;  // a word still being composed is typed when it is done
  const rfb = state.rfb;
  const value = el.value;
  if (rfb && interactive()) {
    let same = 0;
    while (same < KBD_PAD.length && same < value.length && value[same] === KBD_PAD[same]) same += 1;
    for (let i = same; i < KBD_PAD.length; i += 1) rfb.sendKey(XK.BackSpace, 'Backspace');
    for (const ch of value.slice(same)) {
      if (ch === '\n') rfb.sendKey(XK.Return, 'Enter');
      else if (ch.codePointAt(0) >= 0x20) rfb.sendKey(keysyms.lookup(ch.codePointAt(0)), '');
    }
  }
  kbdReset(el);
}

function toggleKeyboard() {
  const el = $('kbd-input');
  if (document.activeElement === el) {
    el.blur();
    return;
  }
  kbdReset(el);
  el.focus({ preventScroll: true });
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
const COPY_REFUSED = 'This browser did not let the page use your clipboard. Press "Copy from the screen" (the copy button under the screen).';

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

// What the lane copied: the text, or an error with a code ('nothing' when the lane has no text copied).
async function readLaneClipboard(settle) {
  if (settle) await sleep(settle);
  const data = await post(boot.paths.clipboard);
  const text = typeof data?.text === 'string' ? data.text : '';
  if (!text) throw Object.assign(new Error('Nothing is copied on the screen.'), { code: 'nothing' });
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
  if (err?.code === 'nothing') toast('Nothing is copied on the screen.');
  else if (err?.code === 'presence_required') {
    toast('Only a person can copy from the screen. Sign in to the panel in this browser (the same address, at /login), then copy again.', 'error');
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

// ---- a clip of the screen, recorded here and saved on this computer (record.js) ----
//
// Anyone who can see the screen here can record what they see, a view link too: the clip is made from this
// page's own picture, so it shows nothing the page does not, and nothing leaves this browser.

const RECORD_FORMAT = recordFormat();
let clip = null;

function setRecording(on, seconds = 0) {
  const btn = $('btn-record');
  btn.dataset.recording = String(on);
  btn.setAttribute('aria-pressed', String(on));
  btn.setAttribute('aria-label', on ? 'Stop recording' : 'Record a clip');
  btn.title = on ? `Stop recording (R). It stops by itself at ${elapsed(LIMIT_S)}.` : 'Record a clip of the screen (R)';
  $('record-time').textContent = elapsed(seconds);
}

async function startRecording() {
  if (clip || !RECORD_FORMAT) return;
  const made = new Clip({
    format: RECORD_FORMAT,
    screen: () => $('screen').querySelector('canvas'),
    card: $('screen-wrap'),
    cursor: $('agent-cursor'),
    arrow: $('agent-cursor').querySelector('.agent-cursor-arrow'),
    tag: $('agent-cursor-tag'),
    interactive,
    markUrl: new URL('./mark.svg', import.meta.url).href,
    onTick: (s) => { if (clip === made) $('record-time').textContent = elapsed(s); },
    onStop: ({ blob, why }) => {
      if (clip === made) clip = null;
      setRecording(false);
      if (!blob.size) {
        toast('The clip came out empty, so nothing was saved. Try again.', 'error');
        return;
      }
      const name = clipName(boot.title || state.lane.id.split('/')[1], RECORD_FORMAT.ext);
      save(blob, name);
      const lead = why === 'limit' ? `Recording stopped at ${elapsed(LIMIT_S)}, the longest a clip can be. ` : why === 'ended' ? 'The screen closed, so the recording stopped. ' : '';
      toast(`${lead}Saved the clip to your downloads: ${name}`);
    },
  });
  clip = made;
  setRecording(true);
  try {
    await made.start();
    toast(`Recording what you see here. Press again to stop (it stops by itself at ${elapsed(LIMIT_S)}). It stays on this computer.`);
  } catch (err) {
    console.warn('could not record', err);
    if (clip === made) clip = null;
    setRecording(false);
    toast('This browser could not record the screen.', 'error');
  }
}

function toggleRecording() {
  if (!RECORD_FORMAT) return;
  if (clip) clip.stop('person');
  else startRecording();
}

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
  if (controlState() !== 'agent') hideCursor();
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
  es.addEventListener('pointer', (e) => onPointer(JSON.parse(e.data)));
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
    render();
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
  $('theme-label').textContent = `Theme: ${THEME_NAMES[theme]}`;
  try { localStorage.setItem('agentos.viewer.theme', theme); } catch { /* private mode */ }
}

// The activity panel starts closed; a browser remembers it open once the person opens it.
// (A new key: the old one was written "open" on every visit, so it holds no real choice.)
const ACTIVITY_KEY = 'agentos.viewer.activity-panel';

function setActivity(open, remember = true) {
  app.dataset.activity = open ? 'open' : 'closed';
  $('btn-activity').setAttribute('aria-expanded', String(open));
  $('activity').inert = !open;
  if (!remember) return;
  try { localStorage.setItem(ACTIVITY_KEY, open ? 'open' : 'closed'); } catch { /* private mode */ }
}

// The whole page goes full screen, so the pill, the menu, the dialogs and the login form come along.
function toggleFullscreen() {
  if (document.fullscreenElement) document.exitFullscreen?.();
  else if (document.documentElement.requestFullscreen) {
    document.documentElement.requestFullscreen().catch(() => toast('Full screen is not available here.', 'error'));
  } else toast('Full screen is not available here.', 'error');
}

function setMenu(open) {
  $('menu').hidden = !open;
  $('btn-menu').setAttribute('aria-expanded', String(open));
  if (open) $('menu').querySelector('.menu-item:not([hidden])')?.focus();
}

const menuOpen = () => !$('menu').hidden;

function openHelp() {
  if (!$('dlg-help').open) $('dlg-help').showModal();
}

const SHORTCUTS = {
  KeyT: take,
  KeyH: handBack,
  KeyS: confirmStop,
  KeyA: () => setActivity(app.dataset.activity !== 'open', true),
  KeyF: toggleFullscreen,
  KeyR: toggleRecording,
  Slash: openHelp,
};

// F alone goes full screen and R records while you watch, never while your keys go to the screen or a text box.
const typing = (el) => el instanceof HTMLElement && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName));

window.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && menuOpen()) {
    setMenu(false);
    $('btn-menu').focus();
    return;
  }
  if ((e.code !== 'KeyF' && e.code !== 'KeyR') || e.altKey || e.shiftKey || e.ctrlKey || e.metaKey || e.repeat) return;
  if (typing(e.target) || document.querySelector('dialog[open]')) return;
  if (interactive() && onScreen(document.activeElement)) return;
  if (e.code === 'KeyR' && !RECORD_FORMAT) return;
  e.preventDefault();
  if (e.code === 'KeyR') toggleRecording();
  else toggleFullscreen();
});

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
  $('btn-stop').addEventListener('click', () => { setMenu(false); confirmStop(); });
  $('btn-request-handback').addEventListener('click', handBack);
  $('btn-request-keep').addEventListener('click', keepControl);

  $('btn-stop-cancel').addEventListener('click', () => $('dlg-stop').close());
  $('btn-stop-confirm').addEventListener('click', () => {
    $('dlg-stop').close();
    act('stop_agent', 'The agent is stopped.');
  });

  const kbd = $('kbd-input');
  kbd.addEventListener('input', kbdInput);
  kbd.addEventListener('compositionend', kbdInput);
  kbd.addEventListener('focus', () => $('btn-keyboard').setAttribute('aria-pressed', 'true'));
  kbd.addEventListener('blur', () => $('btn-keyboard').setAttribute('aria-pressed', 'false'));
  // a tap keeps the field focused, so the phone keeps its keyboard up
  $('btn-keyboard').addEventListener('pointerdown', (e) => e.preventDefault());
  $('btn-keyboard').addEventListener('click', toggleKeyboard);

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

  $('btn-help').addEventListener('click', () => { setMenu(false); openHelp(); });
  $('btn-help-close').addEventListener('click', () => $('dlg-help').close());
  $('btn-activity').addEventListener('click', () => { setMenu(false); setActivity(app.dataset.activity !== 'open'); });
  $('btn-activity-close').addEventListener('click', () => { setActivity(false); $('btn-menu').focus(); });
  $('btn-fullscreen').addEventListener('click', toggleFullscreen);
  $('btn-record').addEventListener('click', toggleRecording);
  $('shortcut-record').hidden = !RECORD_FORMAT;
  window.addEventListener('beforeunload', (e) => {  // a clip still recording would be lost with the page
    if (!clip) return;
    e.preventDefault();
    e.returnValue = '';
  });
  $('btn-theme').addEventListener('click', () => {
    const now = document.documentElement.dataset.theme || 'auto';
    setTheme(THEMES[(THEMES.indexOf(now) + 1) % THEMES.length]);
  });
  $('btn-menu').addEventListener('click', () => setMenu(!menuOpen()));
  document.addEventListener('pointerdown', (e) => {
    if (menuOpen() && !e.target.closest?.('.menu-wrap')) setMenu(false);
  });
  $('menu').addEventListener('keydown', (e) => {  // arrows move between the menu's items
    if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
    const items = [...$('menu').querySelectorAll('.menu-item:not([hidden])')];
    const at = items.indexOf(document.activeElement);
    items[(at + (e.key === 'ArrowDown' ? 1 : items.length - 1)) % items.length]?.focus();
    e.preventDefault();
  });
  document.addEventListener('fullscreenchange', () => {
    const full = Boolean(document.fullscreenElement);
    $('btn-fullscreen').setAttribute('aria-label', full ? 'Leave full screen' : 'Full screen');
    $('btn-fullscreen').title = full ? 'Leave full screen (F)' : 'Full screen (F)';
    queueLayout();
  });
  const sizes = new ResizeObserver(queueLayout);
  sizes.observe($('frame'));
  sizes.observe($('actionbar'));

  for (const kbd of document.querySelectorAll('kbd[data-mod]')) {
    kbd.textContent = isMac ? 'Option Shift' : 'Alt Shift';
  }
  const pasteKey = isMac ? 'Command+V' : 'Ctrl+V';
  const copyKey = isMac ? 'Command+C' : 'Ctrl+C';
  $('help-clipboard').textContent = `While you have control, ${copyKey} on the screen also copies to this computer, and ${pasteKey} pastes into the screen: what you copied last, here or there. When a page's own Copy button or the right-click menu copied something, press "Copy from the screen" (the copy button in the bar under the screen).`;

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
  $('agent-cursor-tag').textContent = boot.agent || 'Agent';
  wire();
  render();
  layoutCard();
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
