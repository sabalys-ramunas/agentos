import RFB from './novnc/core/rfb.js';
import { api, session } from './api.js';

const $ = (id) => document.getElementById(id);
const parts = location.pathname.split('/');  // /nodes/{node}/lanes/{ws}/{name}/view
const node = decodeURIComponent(parts[2]);
const lane = `${decodeURIComponent(parts[4])}/${decodeURIComponent(parts[5])}`;
let rfb = null;
let mode = 'view';

$('lane').textContent = lane;

function connect() {
  if (rfb) rfb.disconnect();
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  const [ws, name] = lane.split('/');
  const url = `${scheme}://${location.host}/v1/view/${encodeURIComponent(node)}/${encodeURIComponent(ws)}/${encodeURIComponent(name)}?mode=${mode}`;
  rfb = new RFB($('screen'), url, { shared: true, wsProtocols: ['binary'] });
  rfb.scaleViewport = true;
  rfb.resizeSession = false;
  rfb.viewOnly = mode !== 'control';
  rfb.addEventListener('connect', () => { $('status').textContent = mode === 'control' ? 'You have control.' : 'Live.'; });
  rfb.addEventListener('disconnect', () => { $('status').textContent = 'Disconnected. Reload to try again.'; });
}

async function control(action) {
  const [ws, name] = lane.split('/');
  try {
    await api('POST', `/api/nodes/${encodeURIComponent(node)}/lanes/${encodeURIComponent(ws)}/${encodeURIComponent(name)}/control`, { action });
  } catch (err) {
    alert(err.message);
    return;
  }
  mode = action === 'take' ? 'control' : 'view';
  $('take').classList.toggle('hidden', mode === 'control');
  $('handback').classList.toggle('hidden', mode !== 'control');
  $('hint').textContent = mode === 'control'
    ? 'You have control. Agents wait until you hand it back.'
    : 'Watching. Agents keep control until you take it.';
  connect();
}

$('take').addEventListener('click', () => control('take'));
$('handback').addEventListener('click', () => control('hand_back'));

session().then((me) => {
  if (!me) { location.replace('/login'); return; }
  connect();
});
