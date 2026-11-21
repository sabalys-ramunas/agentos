import { api, el, session } from './api.js';

const $ = (id) => document.getElementById(id);

function when(iso) {
  if (!iso) return 'never';
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

function nodeState(node) {
  if (!node.online) return el('span', { class: 'pill bad' }, 'offline');
  if (node.degraded) return el('span', { class: 'pill warn' }, 'catching up');
  return el('span', { class: 'pill ok' }, 'online');
}

function renderApprovals(list) {
  const box = $('approval-list');
  box.replaceChildren();
  if (!list.length) {
    box.append(el('p', { class: 'empty' }, 'Nothing waits for you.'));
    return;
  }
  for (const ap of list) {
    const decide = (decision) => async () => {
      try {
        await api('POST', `/api/approvals/${encodeURIComponent(ap.node_id)}/${encodeURIComponent(ap.id)}`, { decision });
      } catch (err) { alert(err.message); }
      refresh();
    };
    const who = ap.requested_by ? `${ap.requested_by.kind || 'agent'} ${ap.requested_by.id || ''}` : 'an agent';
    box.append(el('div', { class: 'card' },
      el('div', {}, el('strong', {}, ap.summary || '(no summary)')),
      el('div', { class: 'muted' }, [ap.effect, ap.lane, `asked by ${who}`].filter(Boolean).join(' · ')),
      ap.preview && ap.preview.text ? el('pre', {}, ap.preview.text) : null,
      el('div', { class: 'actions' },
        el('button', { class: 'primary', onclick: decide('approved') }, 'Approve'),
        el('button', { class: 'danger', onclick: decide('denied') }, 'Deny'))));
  }
}

function renderNodes(nodes) {
  const box = $('node-list');
  box.replaceChildren();
  if (!nodes.length) {
    box.append(el('p', { class: 'empty' }, 'No nodes yet. Add one below.'));
    return;
  }
  for (const node of nodes) {
    const lanes = el('table', {}, el('tr', {}, el('th', {}, 'Lane'), el('th', {}, 'Kind'), el('th', {}, 'State'),
      el('th', {}, 'Signal'), el('th', {}, '')));
    for (const lane of node.lanes) {
      const [ws, name] = lane.id.split('/');
      const view = node.online && lane.kind === 'browser' && ['warm', 'active'].includes(lane.state)
        ? el('a', { href: `/nodes/${encodeURIComponent(node.id)}/lanes/${encodeURIComponent(ws)}/${encodeURIComponent(name)}/view` }, 'Live view')
        : null;
      lanes.append(el('tr', {}, el('td', { class: 'mono' }, lane.id), el('td', {}, lane.kind || ''),
        el('td', {}, lane.state || ''), el('td', {}, lane.signal || ''), el('td', {}, view)));
    }
    const remove = async () => {
      if (!confirm(`Remove node ${node.name}? It must join again to come back.`)) return;
      try { await api('DELETE', `/api/nodes/${encodeURIComponent(node.id)}`); } catch (err) { alert(err.message); }
      refresh();
    };
    box.append(el('div', { class: 'card' },
      el('div', {}, el('strong', {}, node.name), ' ', nodeState(node)),
      el('div', { class: 'muted' }, `version ${node.version || '?'} · ${node.os || '?'}/${node.arch || '?'} · last seen ${when(node.last_seen)}`),
      node.lanes.length ? lanes : el('p', { class: 'empty' }, 'No lanes reported yet.'),
      el('div', { class: 'actions' }, el('button', { class: 'danger', onclick: remove }, 'Remove'))));
  }
}

function renderGrants(grants) {
  const box = $('grant-list');
  box.replaceChildren();
  const table = el('table', {}, el('tr', {}, el('th', {}, 'Agent'), el('th', {}, 'Scope'), el('th', {}, 'Up to'),
    el('th', {}, '')));
  for (const g of grants) {
    const revoke = async () => {
      if (!confirm(`Revoke ${g.subject.id}? Every node refuses it at once.`)) return;
      try { await api('DELETE', `/api/grants/${encodeURIComponent(g.id)}`); } catch (err) { alert(err.message); }
      refresh();
    };
    const scope = [...(g.workspaces || []).map((w) => `${w}/*`), ...(g.lanes || [])].join(', ');
    table.append(el('tr', {}, el('td', {}, g.subject.id), el('td', { class: 'mono' }, scope),
      el('td', {}, g.ceiling || 'read'),
      el('td', {}, g.revoked ? el('span', { class: 'muted' }, 'revoked')
        : el('button', { class: 'danger', onclick: revoke }, 'Revoke'))));
  }
  box.append(table);
}

function renderActivity(events) {
  const box = $('activity');
  box.replaceChildren();
  if (!events.length) {
    box.append(el('p', { class: 'empty' }, 'No activity yet.'));
    return;
  }
  const table = el('table', {}, el('tr', {}, el('th', {}, 'When'), el('th', {}, 'Tool'), el('th', {}, 'Lane'),
    el('th', {}, 'Status'), el('th', {}, 'Summary')));
  for (const t of events.slice(0, 50)) {
    table.append(el('tr', {}, el('td', {}, when(t.at)), el('td', { class: 'mono' }, t.tool || t.kind || ''),
      el('td', { class: 'mono' }, t.lane || ''), el('td', {}, t.status || ''), el('td', {}, t.summary || t.error || '')));
  }
  box.append(table);
}

async function refresh() {
  try {
    const [status, approvals, nodes, grants, activity] = await Promise.all([
      api('GET', '/api/status'), api('GET', '/api/approvals?state=pending'), api('GET', '/api/nodes'),
      api('GET', '/api/grants'), api('GET', '/api/activity?limit=50')]);
    $('org').textContent = `${status.org.name} · ${status.online} of ${status.nodes} nodes online`;
    $('mcp-url').textContent = status.mcp_url;
    renderApprovals(approvals);
    renderNodes(nodes);
    renderGrants(grants);
    renderActivity(activity);
  } catch (err) {
    console.warn(err);
  }
}

async function start() {
  const me = await session();
  if (!me) {
    $('signin').classList.remove('hidden');
    for (const id of ['approvals']) $(id).classList.add('hidden');
    return;
  }
  $('signout').classList.remove('hidden');
  $('signout').addEventListener('click', async () => { await api('DELETE', '/api/session'); location.reload(); });
  $('add-node').addEventListener('click', async () => {
    try {
      const jt = await api('POST', '/api/join-tokens', {});
      $('join').textContent = `On the new node (valid for one hour, once):\n${jt.join_command}`;
      $('join').classList.remove('hidden');
    } catch (err) { alert(err.message); }
  });
  $('add-agent').addEventListener('click', async () => {
    const subject = prompt('Name this agent (for example claude-code):', 'claude-code');
    if (!subject) return;
    try {
      const g = await api('POST', '/api/grants', { subject });
      $('agent-token').textContent = `Shown once. Connect with:\nclaude mcp add --transport http openajar ${g.mcp_url} --header "Authorization: Bearer ${g.token}"`;
      $('agent-token').classList.remove('hidden');
      refresh();
    } catch (err) { alert(err.message); }
  });
  refresh();
  setInterval(refresh, 5000);
}

start();
