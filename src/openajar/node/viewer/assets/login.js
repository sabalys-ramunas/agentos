// openajar live view: the assisted login form, in a side section of its own.
//
// A person who opens the live view to log a lane in gets this form first, and VNC only if they
// choose it (Take control). The node reads the sign-in page, this section shows the fields that
// step asks for, and the node types them into the lane's browser itself
// (openajar.node.login_assist). Values go to the node in one POST and are cleared from the page at
// once. Page text (heading, message, labels) is shown with textContent only: it comes from the site.

const boot = JSON.parse(document.getElementById('openajar-boot').textContent);
const path = boot.paths && boot.paths.login;

const STATE_WORDS = {
  working: 'Working on it',
  ask: 'Your turn',
  waiting: 'Waiting for your phone',
  stopped: 'Needs you in the live view',
  paused: 'You have the live view',
  signed_in: 'Signed in',
  cancelled: 'Cancelled',
  done: 'Finished',
  failed: 'Stopped',
};

const ui = {};
let login = null;
let version = -1;
let open = false;
let busy = false;
let shotUrl = null;
let shotNo = null;

function token() {
  return new URL(location.href).searchParams.get('t') || boot.token;
}

function url(suffix = '', query = {}) {
  const q = new URLSearchParams({ ...query, t: token() });
  return `${path}${suffix}?${q}`;
}

function el(tag, attrs = {}, ...kids) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === 'class') node.className = v;
    else if (k === 'text') node.textContent = v;
    else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids) if (kid) node.append(kid);
  return node;
}

async function post(body) {
  const res = await fetch(url(), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    credentials: 'omit',
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data?.error?.message || `The node answered ${res.status}.`);
    err.code = data?.error?.code;
    throw err;
  }
  return data;
}

function mount() {
  ui.root = el('section', { class: 'la', id: 'login-assist', 'aria-labelledby': 'la-title', hidden: true });
  ui.launch = el('button', { type: 'button', class: 'la-launch', hidden: true, onclick: () => show(true) },
    el('span', { class: 'la-launch-dot', 'aria-hidden': 'true' }), el('span', { id: 'la-launch-text', text: 'Log in' }));
  ui.root.append(
    el('header', { class: 'la-head' },
      el('div', {},
        el('h2', { id: 'la-title', text: 'Log in' }),
        el('p', { class: 'la-sub', id: 'la-sub' })),
      el('button', { type: 'button', class: 'icon-btn la-close', 'aria-label': 'Hide the login form', onclick: () => show(false) },
        el('span', { 'aria-hidden': 'true', text: '×' }))),
    el('div', { class: 'la-body', id: 'la-body' }));
  document.body.append(ui.root);
  // In the bar under the view, before its buttons: floating in the corner it covered "Take control".
  const actions = document.getElementById('actions');
  if (actions) actions.before(ui.launch);
  else document.body.append(ui.launch);
}

function show(value) {
  open = value;
  render();
  if (value) {
    const first = ui.root.querySelector('input, button.btn-primary');
    if (first) first.focus();
  }
}

async function refreshShot() {
  if (!login || !login.screenshot || login.screenshot === shotNo) return;
  shotNo = login.screenshot;
  try {
    const res = await fetch(url('.png'), { credentials: 'omit' });
    if (!res.ok) return;
    const blob = await res.blob();
    if (shotUrl) URL.revokeObjectURL(shotUrl);
    shotUrl = URL.createObjectURL(blob);
    const img = document.getElementById('la-shot');
    if (img) img.src = shotUrl;
  } catch { /* the form works without the picture */ }
}

async function act(body, done) {
  if (busy) return;
  busy = true;
  render();
  try {
    login = await post(body);
    if (done) done();
  } catch (err) {
    if (login) login = { ...login, error: err.message };
    else login = { state: 'none', error: err.message };
  } finally {
    busy = false;
    render();
  }
}

function submitStep(form) {
  const values = {};
  for (const input of form.querySelectorAll('input[data-name]')) {
    values[input.dataset.name] = input.value;
  }
  for (const input of form.querySelectorAll('input')) input.value = '';
  act({ action: 'step', step: login.step.id, values });
}

function stepForm(step) {
  const form = el('form', { class: 'la-form', autocomplete: 'off', onsubmit: (e) => { e.preventDefault(); submitStep(form); } });
  step.fields.forEach((f, i) => {
    const id = `la-f-${i}`;
    const input = el('input', {
      id, 'data-name': f.name, required: true, autocomplete: f.autocomplete, spellcheck: 'false',
      type: f.name === 'password' ? 'password' : 'text',
      inputmode: f.name === 'code' ? 'numeric' : undefined,
      autocapitalize: 'off', maxlength: f.name === 'code' && f.length ? String(f.length + 4) : undefined,
    });
    form.append(el('label', { class: 'field-label', for: id, text: f.label }), input);
  });
  form.append(el('div', { class: 'la-actions' },
    el('button', { type: 'submit', class: 'btn btn-primary', disabled: busy, text: step.submit || 'Continue' })));
  return form;
}

function choiceList(step, primary) {
  if (!step.choices.length) return null;
  const list = el('div', { class: 'la-choices', role: 'group', 'aria-label': primary ? 'Pick one' : 'Other ways' });
  for (const c of step.choices) {
    list.append(el('button', {
      type: 'button', class: primary ? 'btn la-choice' : 'btn btn-quiet la-choice', disabled: busy,
      onclick: () => act({ action: 'step', step: step.id, choice: c.id }), text: c.label,
    }));
  }
  return list;
}

function render() {
  const has = login && login.state && login.state !== 'none';
  ui.launch.hidden = open || !boot.paths.login;
  document.getElementById('la-launch-text').textContent = has && !['signed_in', 'cancelled', 'done'].includes(login.state)
    ? (login.state === 'paused' ? 'Back to the login form' : 'Login form') : 'Log in';
  ui.root.hidden = !open;
  if (!open) return;
  const body = document.getElementById('la-body');
  body.replaceChildren();
  const sub = document.getElementById('la-sub');
  sub.textContent = has ? `${login.site_name || login.host || boot.lane.id} · ${STATE_WORDS[login.state] || login.state}`
    : boot.lane.id;
  if (!has) {
    body.append(
      el('p', { text: 'Type your email, password or code into a form here. openajar types it into the site for you, at a person’s pace. Nothing you type reaches an agent.' }),
      login && login.requested ? el('p', { class: 'la-note', text: `An agent asked: ${login.requested.why}` }) : null,
      login && login.error ? el('p', { class: 'la-error', role: 'alert', text: login.error }) : null,
      el('div', { class: 'la-actions' },
        el('button', { type: 'button', class: 'btn btn-primary', disabled: busy, onclick: () => act({ action: 'start' }), text: busy ? 'Opening' : 'Start the login' })));
    return;
  }
  const step = login.step;
  if (login.screenshot) {
    body.append(el('figure', { class: 'la-shot' },
      el('img', { id: 'la-shot', alt: 'The sign-in page now. Password and code fields are hidden.', src: shotUrl || '' })));
    refreshShot();
  }
  if (step && step.heading) body.append(el('h3', { class: 'la-heading', text: step.heading }));
  if (step && step.message) body.append(el('p', { class: 'la-message', text: step.message }));
  if (step && step.number) body.append(el('p', { class: 'la-number', 'aria-label': `Number ${step.number}`, text: step.number }));
  if (login.note) body.append(el('p', { class: 'la-note', role: 'status', text: login.note }));
  if (login.error) body.append(el('p', { class: 'la-error', role: 'alert', text: login.error }));

  const st = login.state;
  const row = el('div', { class: 'la-actions la-secondary' });
  if (st === 'ask' && step) {
    if (step.fields.length) body.append(stepForm(step), choiceList(step, false));
    else body.append(choiceList(step, true));
  } else if (st === 'waiting' && step) {
    body.append(choiceList(step, false));
    row.append(el('button', { type: 'button', class: 'btn btn-primary', disabled: busy, onclick: () => act({ action: 'refresh' }), text: 'I approved it' }));
  } else if (st === 'working') {
    body.append(el('div', { class: 'la-working' }, el('div', { class: 'spinner', 'aria-hidden': 'true' }), el('span', { text: 'One moment' })));
  } else if (st === 'stopped') {
    row.append(el('button', { type: 'button', class: 'btn btn-primary', disabled: busy, onclick: () => act({ action: 'take' }, () => show(false)), text: 'Take control in the live view' }));
    row.append(el('button', { type: 'button', class: 'btn', disabled: busy, onclick: () => act({ action: 'refresh' }), text: 'Read the page again' }));
  } else if (st === 'paused') {
    row.append(el('button', { type: 'button', class: 'btn btn-primary', disabled: busy, onclick: () => act({ action: 'resume' }), text: 'Back to the form' }));
  } else if (st === 'signed_in' || st === 'done') {
    if (login.can_save && boot.owner) {
      row.append(el('button', { type: 'button', class: 'btn btn-primary', disabled: busy, onclick: () => act({ action: 'save' }), text: 'Save these to the vault for next time' }));
      row.append(el('button', { type: 'button', class: 'btn', disabled: busy, onclick: () => act({ action: 'cancel' }), text: 'No thanks' }));
    } else {
      row.append(el('button', { type: 'button', class: 'btn btn-primary', onclick: () => show(false), text: 'Close' }));
    }
  } else {
    row.append(el('button', { type: 'button', class: 'btn btn-primary', disabled: busy, onclick: () => act({ action: 'start' }), text: 'Start again' }));
  }
  if (['ask', 'waiting', 'working'].includes(st)) {
    row.append(el('button', { type: 'button', class: 'btn', disabled: busy, onclick: () => act({ action: 'take' }, () => show(false)), text: 'Take control instead' }));
  }
  if (!['signed_in', 'cancelled', 'done', 'failed'].includes(st)) {
    row.append(el('button', { type: 'button', class: 'btn btn-quiet-danger', disabled: busy, onclick: () => act({ action: 'cancel' }), text: 'Cancel' }));
  }
  if (row.childElementCount) body.append(row);
}

async function follow() {
  // Long-polls this lane's login: the node answers when it changes, or after 25 seconds.
  for (;;) {
    try {
      const query = version >= 0 ? { after: String(version), wait: '25' } : {};
      const res = await fetch(url('', query), { credentials: 'omit' });
      if (res.status === 403) return;
      const data = await res.json();
      const was = login && login.state;
      if (!busy) login = data;
      version = typeof data.version === 'number' ? data.version : 0;
      if (!was && data.state && data.state !== 'none' && data.state !== 'paused') open = true;
      if (was === 'paused' && data.state && data.state !== 'paused') open = true; // handed back: the form again
      render();
    } catch {
      await new Promise((r) => setTimeout(r, 4000));
    }
  }
}

if (path && boot.mode === 'control') {
  mount();
  // Signed out or never signed in: the form comes first, the live view if the person chooses it.
  if (boot.lane && boot.lane.signal === 'logged_out') open = true;
  render();
  follow();
}
