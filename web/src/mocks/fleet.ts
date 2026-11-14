/**
 * The fleet map and node depth, shaped exactly as the types in src/api/types.ts say.
 * Everything is derived from the same demo world as every other route, so the map cannot
 * disagree with the rest of the panel. All of it is fictional.
 */
import type {
  CheckStatus,
  ConnectionKind,
  EntityState,
  HealthCheck,
  Lane,
  LogLevel,
  LogLine,
  LogSource,
  NodeConnections,
  NodeProgram,
  NodeSchedule,
  NodeService,
  NodeSystem,
  RunStatus,
  ScheduleFire,
  ToolDef,
  Topology,
  TopologyActive,
  TopologyEntity,
  TopologyEvent,
  TopologyLink,
  TraceEvent,
} from '@/api/types';
import { laneName } from '@/lib/labels';
import { laneStatus } from '@/lib/lanes';
import { siteOf as knownSite } from '@/lib/sites';
import { emit } from './bus';
import { lastFires, nextFires } from './cron';
import { previewTopology } from './fleet-preview';
import { laneHealth } from './rebuild';
import { scenario } from './scenario';
import type { NodeRecord, OrgData } from './seed';
import { principalId } from '@/lib/principal';

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** hetzner-1 lost its link six hours ago. The metrics, replay and connection log all agree on it. */
export const OUTAGE = { node: 'hetzner-1', from: 6.4 * HOUR, to: 5.7 * HOUR };

const AGENTS = [
  { id: 'claude-code', label: 'claude-code', surface: 'Claude Code' },
  { id: 'codex', label: 'codex', surface: 'Codex' },
  { id: 'claude-app', label: 'claude-app', surface: 'Claude app' },
  { id: 'chatgpt-app', label: 'chatgpt-app', surface: 'ChatGPT app' },
];

const TOOLS: { tool: string; effect: TopologyEvent['effect']; weight: number }[] = [
  { tool: 'browser_read', effect: 'read', weight: 6 },
  { tool: 'browser_act', effect: 'draft', weight: 4 },
  { tool: 'browser_screenshot', effect: 'read', weight: 3 },
  { tool: 'browser_post', effect: 'commit', weight: 1 },
];

function rng(seed: number) {
  let state = seed | 0;
  return () => {
    state = (state + 0x6d2b79f5) | 0;
    let t = Math.imul(state ^ (state >>> 15), 1 | state);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function pickTool(random: () => number) {
  const total = TOOLS.reduce((sum, item) => sum + item.weight, 0);
  let roll = random() * total;
  for (const item of TOOLS) {
    roll -= item.weight;
    if (roll <= 0) return item;
  }
  return TOOLS[0] as (typeof TOOLS)[number];
}

/** `aster.instagram.post` runs on `aster/instagram`. */
export const laneOfFlow = (flow: string) => flow.split('.').slice(0, 2).join('/');

export const siteOf = (lane: Lane) => lane.account?.site ?? (lane.kind === 'browser' ? `${lane.name}.com` : null);

/** As the cloud names a site entity: `instagram` for a known site, the host for any other. */
const siteKey = (site: string) => knownSite(site)?.key ?? knownSite(site.replace(/\.com$/, ''))?.key ?? site;

function entityState(lane: Lane, nodeDown: boolean): EntityState {
  if (nodeDown) return 'down';
  const tone = laneStatus(lane).tone;
  if (tone === 'bad') return 'blocked';
  if (tone === 'warn') return 'attention';
  if (tone === 'idle') return 'idle';
  return 'ok';
}

/** Which agent tends to work where, so paths on the map stay recognisable from day to day. */
function agentFor(lane: Lane): string {
  if (lane.workspace === 'personal') return lane.kind === 'browser' ? 'claude-app' : 'codex';
  if (lane.workspace === 'meridian') return 'claude-app';
  if (lane.workspace === 'solstice' && lane.name === 'blog') return 'chatgpt-app';
  if (lane.workspace === 'harbor' && (lane.name === 'blog' || lane.name === 'medium')) return 'codex';
  return 'claude-code';
}

/** Open sessions by lane, kept by the traffic below. `still` keeps the two the seed starts with. */
const live = new Map<string, TopologyActive>();

export function topology(data: OrgData, windowSeconds: number): Topology {
  if (scenario.has('scale')) return scaledTopology(windowSeconds);
  const preview = scenario.has('preview-3') ? 3 : scenario.has('preview-120') ? 120 : 0;
  if (preview) return previewTopology(preview, windowSeconds);
  const now = Date.now();
  const since = now - windowSeconds * 1_000;
  const down = new Set(data.nodes.filter((node) => node.state === 'down').map((node) => node.id));
  const events = replayEvents(data, new Date(since).toISOString(), new Date(now).toISOString(), 20_000);

  const calls = new Map<string, { calls: number; failed: number; last: string | null }>();
  const count = (key: string, event: TopologyEvent) => {
    const entry = calls.get(key) ?? { calls: 0, failed: 0, last: null };
    entry.calls += 1;
    if (event.status === 'failed') entry.failed += 1;
    entry.last = event.at;
    calls.set(key, entry);
  };
  const laneById = new Map(data.lanes.map((lane) => [lane.id, lane]));
  for (const event of events) {
    count(`lane:${event.lane}`, event);
    count(`agent:${event.principal}`, event);
    count(`agent:${event.principal}|${event.lane}`, event);
    const lane = laneById.get(event.lane);
    const site = lane ? siteOf(lane) : null;
    if (site) count(`site:${siteKey(site)}`, event);
  }
  const stats = (key: string) => calls.get(key) ?? { calls: 0, failed: 0, last: null };
  const link = (from: string, to: string, kind: TopologyLink['kind'], key?: string): TopologyLink => {
    const entry = key ? stats(key) : { calls: 0, failed: 0, last: null };
    return { from, to, kind, calls: entry.calls, failed: entry.failed, last_at: entry.last };
  };

  const entities: TopologyEntity[] = [
    { id: 'cloud:agentos', type: 'cloud', label: 'agentos cloud', state: 'ok', ref: null, stats: { calls: events.length } },
  ];
  const links: TopologyLink[] = [];

  for (const agent of AGENTS) {
    const entry = stats(`agent:${agent.id}`);
    entities.push({
      id: `agent:${agent.id}`,
      type: 'agent',
      label: agent.label,
      state: entry.calls > 0 ? 'ok' : 'idle',
      ref: { href: `/activity?principal=${agent.id}` },
      stats: { surface: agent.surface, kind: 'agent', brand: agent.id.startsWith('claude') ? 'claude' : agent.id.startsWith('chatgpt') ? 'chatgpt' : agent.id.startsWith('codex') ? 'codex' : null, calls: entry.calls, failed: entry.failed },
    });
    links.push(link(`agent:${agent.id}`, 'cloud:agentos', 'attached', `agent:${agent.id}`));
  }

  for (const node of data.nodes) {
    entities.push({
      id: `node:${node.id}`,
      type: 'node',
      label: node.id,
      state: node.state === 'down' ? 'down' : node.state === 'degraded' ? 'attention' : 'ok',
      ref: { href: `/nodes/${node.id}` },
      stats: { lanes: data.lanes.filter((lane) => lane.node === node.id).length, version: node.version },
    });
    links.push(link(`node:${node.id}`, 'cloud:agentos', 'attached'));
  }

  for (const workspace of data.workspaces) {
    const lanes = data.lanes.filter((lane) => lane.workspace === workspace.id);
    const worst = lanes.map((lane) => entityState(lane, down.has(lane.node)));
    const state: EntityState = worst.includes('blocked')
      ? 'blocked'
      : worst.includes('attention')
        ? 'attention'
        : worst.includes('down')
          ? 'down'
          : worst.every((item) => item === 'idle')
            ? 'idle'
            : 'ok';
    entities.push({
      id: `workspace:${workspace.id}`,
      type: 'workspace',
      label: workspace.name,
      state,
      ref: { href: `/?workspace=${workspace.id}` },
      stats: { lanes: lanes.length, color: workspace.color },
    });
  }

  const sites = new Map<string, number>();
  const proxies = new Map<string, number>();
  for (const lane of data.lanes) {
    const key = `lane:${lane.id}`;
    entities.push({
      id: key,
      type: 'lane',
      label: lane.id,
      state: entityState(lane, down.has(lane.node)),
      ref: { href: `/lanes/${lane.id}` },
      stats: { kind: lane.kind, calls: stats(key).calls, failed: stats(key).failed, health: laneHealth(lane).state, site: lane.kind === 'terminal' ? 'terminal' : siteOf(lane) ? siteKey(siteOf(lane) as string) : null },
    });
    links.push(link(key, `node:${lane.node}`, 'runs_on'), link(key, `workspace:${lane.workspace}`, 'belongs_to'));
    const site = siteOf(lane);
    if (site) {
      sites.set(siteKey(site), (sites.get(siteKey(site)) ?? 0) + 1);
      links.push(link(key, `site:${siteKey(site)}`, 'acts_on', key));
    }
    if (lane.proxy) {
      proxies.set(lane.proxy.label, (proxies.get(lane.proxy.label) ?? 0) + 1);
      links.push(link(key, `proxy:${lane.proxy.label}`, 'exits_via'));
    }
    for (const agent of AGENTS) {
      const entry = calls.get(`agent:${agent.id}|${lane.id}`);
      // As the API sends it: agent to lane is `attached`.
      if (entry) links.push(link(`agent:${agent.id}`, key, 'attached', `agent:${agent.id}|${lane.id}`));
    }
  }
  for (const [site, lanes] of sites) {
    const entry = stats(`site:${site}`);
    const known = knownSite(site);
    entities.push({ id: `site:${site}`, type: 'site', label: known?.name ?? site, state: 'ok', ref: null, stats: { lanes, key: site, domain: known?.domains[0] ?? site, calls: entry.calls, failed: entry.failed } });
  }
  for (const [label, lanes] of proxies) {
    entities.push({ id: `proxy:${label}`, type: 'proxy', label, state: 'ok', ref: null, stats: { lanes } });
  }

  const brainHome: Record<string, string> = { 'claude-max': 'home-mini', 'ollama-local': 'home-mini', 'anthropic-api': 'hetzner-1' };
  for (const brain of data.brains) {
    entities.push({
      id: `brain:${brain.name}`,
      type: 'brain',
      label: brain.name,
      state: brain.state === 'ready' ? 'ok' : brain.state === 'down' ? 'down' : 'attention',
      ref: { href: '/settings/brains' },
      stats: { kind: brain.kind, provider: brain.provider },
    });
    const home = brainHome[brain.name] ?? data.nodes[0]?.id;
    if (home && data.nodes.some((node) => node.id === home)) links.push(link(`node:${home}`, `brain:${brain.name}`, 'uses_brain'));
  }

  for (const schedule of data.schedules) {
    const lane = laneOfFlow(schedule.flow);
    if (!laneById.has(lane)) continue;
    entities.push({
      id: `schedule:${schedule.id}`,
      type: 'schedule',
      label: schedule.flow,
      state: schedule.enabled === false ? 'idle' : 'ok',
      ref: { href: `/nodes/${laneById.get(lane)?.node}?tab=schedules` },
      stats: { cron: schedule.cron ?? null, watch: schedule.watch ?? null, next_fire_at: schedule.next_fire_at ?? null },
    });
    links.push(link(`schedule:${schedule.id}`, `lane:${lane}`, 'triggers'));
  }

  const seeded: TopologyActive[] = data.lanes
    .filter((lane) => lane.state === 'active' && !live.has(lane.id))
    .map((lane) => ({ lane: lane.id, principal: agentFor(lane), tool: 'browser_act', since: new Date(now - 90_000).toISOString(), run_id: null }));

  // The last ten minutes of calls, newest first, as the cloud sends them.
  const recent = events
    .filter((event) => Date.parse(event.at) >= now - 10 * MINUTE)
    .reverse()
    .slice(0, 100)
    .map((event) => ({ at: event.at, lane: event.lane, principal: event.principal, kind: 'agent' as const, tool: event.tool, status: event.status }));
  return { generated_at: new Date(now).toISOString(), window_seconds: windowSeconds, entities, links, active: [...live.values(), ...seeded], recent };
}

/** 200 lanes across eight nodes, for proving the map holds up. `?mock=scale`. */
function scaledTopology(windowSeconds: number): Topology {
  const random = rng(11);
  const sites = ['instagram.com', 'facebook.com', 'reddit.com', 'medium.com', 'linkedin.com', 'tiktok.example', 'pinterest.example', 'youtube.example', 'x.example', 'threads.example'];
  const entities: TopologyEntity[] = [{ id: 'cloud:agentos', type: 'cloud', label: 'agentos cloud', state: 'ok', ref: null, stats: {} }];
  const links: TopologyLink[] = [];
  for (const agent of [...AGENTS, { id: 'night-shift', label: 'night-shift', surface: 'Hosted agent' }]) {
    entities.push({ id: `agent:${agent.id}`, type: 'agent', label: agent.label, state: 'ok', ref: null, stats: { surface: agent.surface } });
    links.push({ from: `agent:${agent.id}`, to: 'cloud:agentos', kind: 'attached', calls: 40, failed: 1, last_at: null });
  }
  const nodes = ['ams-1', 'ams-2', 'fra-1', 'fra-2', 'hel-1', 'home-mini', 'lon-1', 'vno-1'];
  for (const node of nodes) {
    entities.push({ id: `node:${node}`, type: 'node', label: node, state: node === 'fra-2' ? 'down' : 'ok', ref: null, stats: {} });
    links.push({ from: `node:${node}`, to: 'cloud:agentos', kind: 'attached', calls: 0, failed: 0, last_at: null });
  }
  for (const site of sites) entities.push({ id: `site:${site}`, type: 'site', label: site, state: 'ok', ref: null, stats: {} });
  for (let index = 0; index < 34; index += 1) {
    const id = `brand-${String(index + 1).padStart(2, '0')}`;
    const node = nodes[index % nodes.length] as string;
    entities.push({ id: `workspace:${id}`, type: 'workspace', label: `Brand ${index + 1}`, state: 'ok', ref: null, stats: { color: '#6b7280' } });
    const count = 4 + Math.floor(random() * 4);
    const offset = Math.floor(random() * sites.length);
    for (let surface = 0; surface < count; surface += 1) {
      const site = sites[(offset + surface) % sites.length] as string;
      const name = site.split('.')[0] as string;
      const lane = `lane:${id}/${name}`;
      const roll = random();
      const state: EntityState = node === 'fra-2' ? 'down' : roll > 0.94 ? 'blocked' : roll > 0.86 ? 'attention' : roll > 0.45 ? 'ok' : 'idle';
      entities.push({ id: lane, type: 'lane', label: `${id}/${name}`, state, ref: null, stats: { kind: 'browser' } });
      links.push(
        { from: lane, to: `node:${node}`, kind: 'runs_on', calls: 0, failed: 0, last_at: null },
        { from: lane, to: `workspace:${id}`, kind: 'belongs_to', calls: 0, failed: 0, last_at: null },
        { from: lane, to: `site:${site}`, kind: 'acts_on', calls: 3, failed: 0, last_at: null },
      );
    }
  }
  return { generated_at: new Date().toISOString(), window_seconds: windowSeconds, entities, links, active: [] };
}

/**
 * Tool calls for a time range. The seed's activity list stays short for the screens that page
 * through it, so this fills in the same fleet at a realistic rate, the same way every time.
 */
export function replayEvents(data: OrgData, from: string, to: string, limit: number): TopologyEvent[] {
  const start = new Date(from).getTime();
  const end = new Date(to).getTime();
  const now = Date.now();
  const SLOT = 10 * MINUTE;
  const lanes = data.lanes.filter((lane) => lane.kind === 'browser' && lane.state !== 'quarantined');
  const events: TopologyEvent[] = [];
  if (lanes.length === 0) return events;
  const outage = { from: now - OUTAGE.from, to: now - OUTAGE.to };

  for (let slot = Math.floor(start / SLOT); slot * SLOT < end; slot += 1) {
    const random = rng(slot);
    const at = slot * SLOT;
    const hour = new Date(at).getHours();
    // Quiet at night, busy in the working day, and the overnight schedules still fire.
    const pace = hour >= 8 && hour < 20 ? 7 : hour >= 20 ? 3 : 2;
    const sessions = Math.floor(random() * pace);
    for (let index = 0; index < sessions; index += 1) {
      const lane = lanes[Math.floor(random() * lanes.length)] as Lane;
      const offset = random() * SLOT;
      const calls = 1 + Math.floor(random() * 6);
      for (let call = 0; call < calls; call += 1) {
        const time = at + offset + call * (5_000 + random() * 25_000);
        if (time < start || time >= end) continue;
        if (lane.node === OUTAGE.node && time >= outage.from && time <= outage.to) continue;
        const { tool, effect } = pickTool(random);
        const failed = random() < 0.035;
        const status: RunStatus = failed ? 'failed' : random() < 0.8 ? 'succeeded_verified' : 'succeeded_unverified';
        events.push({ at: new Date(time).toISOString(), lane: lane.id, principal: agentFor(lane), tool, effect, status });
      }
    }
  }

  // A captcha wall in the small hours: the story Replay exists to tell.
  const wall = now - 3.2 * HOUR;
  if (wall >= start && wall < end) {
    for (let index = 0; index < 7; index += 1) {
      events.push({
        at: new Date(wall + index * 70_000).toISOString(),
        lane: 'harbor/reddit',
        principal: 'claude-code',
        tool: 'browser_act',
        effect: 'draft',
        status: index < 6 ? 'failed' : 'waiting_approval',
      });
    }
  }

  for (const trace of data.activity) {
    const time = new Date(trace.at).getTime();
    if (time >= start && time < end && trace.kind === 'tool_call' && trace.id.startsWith('trc_live')) {
      events.push({ at: trace.at, lane: trace.lane, principal: principalId(trace.principal), tool: trace.tool, effect: trace.effect, status: trace.status });
    }
  }

  return events.sort((a, b) => a.at.localeCompare(b.at)).slice(-limit);
}

// Node depth

const SYSTEMS: Record<string, Omit<NodeSystem, 'os' | 'arch' | 'version' | 'timezone' | 'reported_at'>> = {
  'home-mini': {
    kernel: 'Darwin 24.1.0',
    cpu_model: 'Apple M4',
    cores: 10,
    mem_total_mb: 16_384,
    disk_total_mb: 460_000,
    gpu: 'Apple M4, 10-core GPU',
    uptime_s: 19 * 86_400 + 4 * 3_600,
    enrolled_at: new Date(Date.now() - 74 * DAY).toISOString(),
    enrolled_by: 'demo@agentos.local',
    key_fingerprint: 'ed25519:7c1e 44a9 02bb f1d3 9e60 ac17 58d2 0f3b',
    protocol: 1,
  },
  'hetzner-1': {
    kernel: 'Linux 6.1.0-28-amd64',
    cpu_model: 'AMD EPYC 7502P',
    cores: 8,
    mem_total_mb: 32_768,
    disk_total_mb: 240_000,
    gpu: null,
    uptime_s: 41 * 86_400 + 11 * 3_600,
    enrolled_at: new Date(Date.now() - 58 * DAY).toISOString(),
    enrolled_by: 'demo@agentos.local',
    key_fingerprint: 'ed25519:e2f0 6b1c 7d48 a3c5 11fe 90d7 c6a2 4b8e',
    protocol: 1,
  },
};

/** `?mock=bare` is a node on an older build, which does not answer depth reports yet. */
export const reportsDepth = (node: NodeRecord) => !(scenario.has('bare') && node.id === 'hetzner-1') && node.id in SYSTEMS;

export function nodeSystem(node: NodeRecord): NodeSystem {
  const base = SYSTEMS[node.id] as (typeof SYSTEMS)[string];
  return { ...base, os: node.os, arch: node.arch, version: node.version, timezone: node.timezone ?? 'UTC', reported_at: new Date(Date.now() - 40_000).toISOString() };
}

const hashOf = (text: string) => {
  const random = rng(text.length * 7919 + (text.codePointAt(0) ?? 0));
  return `sha256:${Array.from({ length: 12 }, () => Math.floor(random() * 16).toString(16)).join('')}`;
};

export function nodePrograms(data: OrgData, node: NodeRecord): NodeProgram[] {
  const lanes = data.lanes.filter((lane) => lane.node === node.id);
  const browsers = lanes.filter((lane) => lane.kind === 'browser').map((lane) => lane.id);
  const installed = (days: number) => new Date(Date.now() - days * DAY).toISOString();
  const program = (name: string, version: string, source: string, scope: NodeProgram['scope'], laneIds: string[], signed: boolean, pinned: boolean, days: number): NodeProgram => ({
    name, version, source, scope, lanes: laneIds, signed, pinned_hash: pinned ? hashOf(name + version) : null, installed_at: installed(days),
  });
  const common = [
    program('agentosd', node.version, 'agentos', 'node', [], true, true, 3),
    program('camoufox', '135.0.1-beta', 'agentos', 'base_image', browsers, true, true, 12),
    program('novnc', '1.5.0', 'agentos', 'base_image', [], true, true, 12),
    program('wireguard-tools', '1.0.20210914', 'apt', 'node', [], true, false, 31),
    program('python', '3.12.7', 'agentos', 'base_image', [], true, true, 12),
    program('ffmpeg', '7.1', 'apt', 'node', [], true, false, 22),
  ];
  if (node.id === 'home-mini') {
    return [
      ...common,
      program('ollama', '0.5.4', 'brew', 'node', [], true, false, 9),
      program('android-platform-tools', '35.0.2', 'brew', 'node', ['personal/pixel'], true, false, 40),
      program('ripgrep', '14.1.1', 'brew', 'lane', ['personal/shell'], true, false, 5),
      program('rclone', '1.68.2', 'brew', 'lane', ['personal/notes-sync'], false, false, 27),
    ];
  }
  return [
    ...common,
    program('imagemagick', '7.1.1-41', 'apt', 'lane', ['meridian/instagram', 'solstice/instagram'], true, false, 18),
    program('pandoc', '3.5', 'apt', 'lane', ['harbor/blog', 'solstice/blog'], true, false, 18),
    program('sharp-cli', '4.2.0', 'npm', 'lane', ['solstice/instagram'], false, false, 2),
  ];
}

export function nodeServices(_data: OrgData, node: NodeRecord): NodeService[] {
  const started = (hours: number) => new Date(Date.now() - hours * HOUR).toISOString();
  const base: NodeService[] = [
    { name: 'agentosd', lane: null, state: 'running', restarts: 0, started_at: started(72), ports: [], cpu_pct: 2.4, mem_mb: 212 },
    { name: 'scheduler', lane: null, state: 'running', restarts: 0, started_at: started(72), ports: [], cpu_pct: 0.3, mem_mb: 48 },
    { name: 'websockify', lane: null, state: 'running', restarts: 1, started_at: started(19), ports: [6080], cpu_pct: 0.9, mem_mb: 34 },
  ];
  if (node.id === 'home-mini') {
    return [
      ...base,
      { name: 'ollama', lane: null, state: 'running', restarts: 0, started_at: started(40), ports: [11434], cpu_pct: 4.1, mem_mb: 1_840 },
      { name: 'notes-sync', lane: 'personal/notes-sync', state: 'running', restarts: 3, started_at: started(6), ports: [], cpu_pct: 0.6, mem_mb: 92 },
      { name: 'adb-bridge', lane: 'personal/pixel', state: 'stopped', restarts: 0, started_at: null, ports: [5037], cpu_pct: null, mem_mb: null },
    ];
  }
  return base.map((service) => (service.name === 'websockify' ? { ...service, restarts: 4 } : service));
}

export function nodeTools(): ToolDef[] {
  return [
    { name: 'browser_open', description: 'Open or focus a page in a lane', effect: 'read' },
    { name: 'browser_read', description: 'Read the page as text', effect: 'read' },
    { name: 'browser_screenshot', description: 'Capture the lane', effect: 'read' },
    { name: 'browser_act', description: 'Click, type and scroll', effect: 'draft' },
    { name: 'browser_post', description: 'Submit something public', effect: 'commit' },
    { name: 'vault_fill', description: 'Fill a login the model never sees', effect: 'secret' },
    { name: 'terminal_exec', description: 'Run a command in a terminal lane', effect: 'exec' },
    { name: 'files_put', description: 'Place a file in a lane', effect: 'draft' },
    { name: 'human_ask', description: 'Ask a person and wait', effect: 'read' },
    { name: 'flow_run', description: 'Run a saved flow', effect: 'commit' },
    { name: 'program_install', description: 'Install into a lane overlay', effect: 'install_overlay' },
    { name: 'lane_request_control', description: 'Ask for control back', effect: 'read' },
  ];
}

export function nodeHealth(data: OrgData, node: NodeRecord): HealthCheck[] {
  const at = new Date(Date.now() - 4 * MINUTE).toISOString();
  const check = (name: string, status: CheckStatus, detail: string, fix?: string): HealthCheck => ({
    name,
    status,
    detail,
    at,
    id: name.toLowerCase().replace(/[^a-z0-9]+/g, '-'),
    fix: status === 'ok' ? null : (fix ?? null),
  });
  const stuck = data.lanes.filter((lane) => lane.node === node.id && lane.signal !== 'ok' && lane.signal !== 'unknown');
  const lowDisk = node.load.disk_free_mb < 100_000;
  return [
    check('Control channel', 'ok', 'Connected, last heartbeat a few seconds ago'),
    check('Clock', 'ok', 'Within 40 ms of the controller'),
    check('Disk space', lowDisk ? 'warn' : 'ok', lowDisk ? 'Under 100 GB free. Profiles grow about 1 GB a day' : 'Plenty free', 'Free some disk, or keep the backups elsewhere: agentos backup create --to /mnt/backup'),
    check('Browser engine', 'ok', 'camoufox starts in 1.8 s'),
    check('Remote view', node.id === 'hetzner-1' ? 'warn' : 'ok', node.id === 'hetzner-1' ? 'websockify restarted 4 times this week' : 'Answering', 'Restart it from Environments, and read its log if it happens again'),
    check('Vault', 'ok', 'Sealed at rest, unsealed for this session'),
    check('Lane profiles', 'ok', 'All writable, none locked'),
    check('Lane signals', stuck.length ? 'warn' : 'ok', stuck.length ? `${stuck.length} lanes need a look` : 'No lane is stuck', 'Open the account that needs a look. Its page says what is stuck'),
    check('Outbox', 'ok', 'Empty, nothing waiting to send'),
    check('Base image', 'ok', 'Matches its pinned hash'),
  ];
}

export function nodeSchedules(data: OrgData, node: NodeRecord): NodeSchedule[] {
  const now = Date.now();
  return data.schedules
    .filter((schedule) => data.lanes.find((lane) => lane.id === laneOfFlow(schedule.flow))?.node === node.id)
    .map((schedule) => {
      const random = rng(schedule.flow.length * 31 + (schedule.flow.codePointAt(1) ?? 0));
      const lane = laneOfFlow(schedule.flow);
      const blocked = data.lanes.find((item) => item.id === lane);
      const stuck = blocked ? blocked.hold !== null && blocked.signal !== 'ok' : false;
      const past = schedule.cron ? lastFires(schedule.cron, now, 12) : [];
      const recent: ScheduleFire[] = past.map((at, index) => {
        const roll = random();
        const status: ScheduleFire['status'] =
          stuck && index < 2 ? 'missed' : roll > 0.93 ? 'failed' : roll > 0.8 ? 'succeeded_unverified' : 'succeeded_verified';
        return { at: new Date(at).toISOString(), status, run_id: status === 'missed' ? null : `run_${Math.floor(at / 1000).toString(36)}` };
      });
      const enabled = schedule.enabled !== false;
      const next = enabled && schedule.cron ? nextFires(schedule.cron, now, 400).filter((at) => at < now + 8 * DAY) : [];
      const first = recent[0];
      return {
        ...schedule,
        lane,
        last_run: first?.run_id ? { id: first.run_id, status: first.status as RunStatus } : (schedule.last_run ?? null),
        next_fire_at: next[0] ? new Date(next[0]).toISOString() : null,
        next_run: next[0] ? new Date(next[0]).toISOString() : null,
        recent_fires: recent,
        next_fires: next.map((at) => new Date(at).toISOString()),
      };
    });
}

export function nodeConnections(node: NodeRecord): NodeConnections {
  const now = Date.now();
  const random = rng(node.id.length * 101);
  const days = Array.from({ length: 30 }, (_, index) => {
    const day = new Date(now - (29 - index) * DAY);
    day.setHours(0, 0, 0, 0);
    const roll = random();
    const incidents = roll > 0.93 ? 1 : 0;
    return { day: day.toISOString(), up_pct: incidents ? 96 + Math.round(random() * 30) / 10 : 100, incidents };
  });
  const events: NodeConnections['events'] = [];
  const add = (ago: number, kind: ConnectionKind, detail: string | null) => events.push({ at: new Date(now - ago).toISOString(), kind, detail });
  if (node.id === OUTAGE.node) {
    const today = days[days.length - 1];
    if (today) Object.assign(today, { up_pct: 97.1, incidents: 1 });
    add(OUTAGE.to - 4 * MINUTE, 'backlog_flushed', '318 events sent in 41 s');
    add(OUTAGE.to - 1 * MINUTE, 'recovered', 'Outbox drained');
    add(OUTAGE.to, 'connected', 'Handshake on protocol 1');
    add(OUTAGE.to + 20_000, 'degraded', 'Reconnected with a backlog');
    add(OUTAGE.from, 'disconnected', 'Heartbeat missed 3 times');
    add(9 * DAY, 'connected', 'Handshake on protocol 1');
    add(9 * DAY + 4 * MINUTE, 'disconnected', 'Socket closed by the network');
  } else {
    add(19 * DAY, 'connected', 'Handshake on protocol 1');
    add(19 * DAY + 2 * MINUTE, 'disconnected', 'Node restarted');
  }
  const mean = days.reduce((sum, day) => sum + day.up_pct, 0) / days.length;
  return { uptime_pct: Math.round(mean * 100) / 100, days, events };
}

/** Per-lane share of the machine, as a heartbeat may report it. Warm lanes only, like a real one. */
export function laneShare(lane: Lane): Pick<Lane, 'cpu_pct' | 'mem_mb'> {
  if (lane.state !== 'warm' && lane.state !== 'active') return { cpu_pct: null, mem_mb: null };
  const random = rng(lane.id.length * 977 + (lane.id.codePointAt(2) ?? 0));
  const busy = lane.state === 'active' ? 2.6 : 1;
  return { cpu_pct: Math.round((1 + random() * 3.4) * busy * 10) / 10, mem_mb: Math.round(340 + random() * 620 * busy) };
}

// Logs

const LOG_TEMPLATES: { source: LogSource; level: LogLevel; text: (lane: string) => string; weight: number }[] = [
  { source: 'agentosd', level: 'info', text: () => 'heartbeat sent, 5 fields', weight: 5 },
  { source: 'agentosd', level: 'debug', text: () => 'gc: freed 41 MB of page cache', weight: 2 },
  { source: 'channel', level: 'debug', text: () => 'ack upto_seq advanced', weight: 3 },
  { source: 'channel', level: 'info', text: () => 'stream_credit +65536 bytes on vnc stream', weight: 2 },
  { source: 'scheduler', level: 'info', text: (lane) => `fired ${lane.replace('/', '.')}.read, run started`, weight: 2 },
  { source: 'scheduler', level: 'debug', text: () => 'next wake in 42 s', weight: 2 },
  { source: 'lane', level: 'info', text: () => 'browser_read returned 14 KB of text', weight: 6 },
  { source: 'lane', level: 'info', text: () => 'browser_act click on "Save draft"', weight: 4 },
  { source: 'lane', level: 'debug', text: () => 'screenshot captured, 1280 x 800', weight: 3 },
  { source: 'lane', level: 'warn', text: () => 'page took 8.2 s to settle, continuing', weight: 2 },
  { source: 'lane', level: 'warn', text: () => 'vault_fill for login form, value [redacted]', weight: 1 },
  { source: 'lane', level: 'error', text: () => 'submit returned an error page, run marked failed', weight: 1 },
  { source: 'service', level: 'info', text: () => 'websockify: client connected, read only', weight: 2 },
  { source: 'service', level: 'warn', text: () => 'notes-sync: remote slow, retrying in 30 s', weight: 1 },
];

function logLine(data: OrgData, node: NodeRecord, random: () => number, at: number): LogLine {
  const total = LOG_TEMPLATES.reduce((sum, item) => sum + item.weight, 0);
  let roll = random() * total;
  let template = LOG_TEMPLATES[0] as (typeof LOG_TEMPLATES)[number];
  for (const item of LOG_TEMPLATES) {
    roll -= item.weight;
    if (roll <= 0) {
      template = item;
      break;
    }
  }
  const lanes = data.lanes.filter((lane) => lane.node === node.id);
  const lane = lanes[Math.floor(random() * lanes.length)];
  const named = template.source === 'lane' || template.source === 'scheduler';
  return { at: new Date(at).toISOString(), source: template.source, level: template.level, message: template.text(lane?.id ?? 'lane'), lane: named ? (lane?.id ?? null) : null };
}

export function logBacklog(data: OrgData, node: NodeRecord, count: number): LogLine[] {
  const random = rng(node.id.length * 13);
  const now = Date.now();
  return Array.from({ length: count }, (_, index) => logLine(data, node, random, now - (count - index) * 3_200));
}

export function nextLog(data: OrgData, node: NodeRecord): LogLine {
  return logLine(data, node, Math.random, Date.now());
}

// Traffic

const MOCK_SESSIONS = 3;

/** Agents working: a few sessions at a time, each a burst of tool calls on one lane. */
export function startTraffic(data: OrgData, slug: string) {
  const sessions = new Map<string, { lane: string; principal: string; left: number; since: string }>();
  const scaled = scenario.has('scale') ? scaledTopology(3_600) : null;
  const lanes = scaled
    ? scaled.entities.filter((entity) => entity.type === 'lane' && entity.state !== 'down').map((entity) => entity.label)
    : null;

  const open = () => {
    const pool = lanes
      ? lanes
      : data.lanes
          .filter((lane) => lane.kind === 'browser' && lane.signal === 'ok' && lane.state !== 'cold' && lane.control.state === 'agent')
          .map((lane) => lane.id);
    const id = pool[Math.floor(Math.random() * pool.length)];
    if (!id || sessions.has(id)) return;
    const lane = data.lanes.find((item) => item.id === id);
    const principal = lane ? agentFor(lane) : (AGENTS[Math.floor(Math.random() * AGENTS.length)]?.id ?? 'claude-code');
    sessions.set(id, { lane: id, principal, left: 3 + Math.floor(Math.random() * 9), since: new Date().toISOString() });
  };

  const step = () => {
    while (sessions.size < MOCK_SESSIONS) {
      const before = sessions.size;
      open();
      if (sessions.size === before) break;
    }
    const all = [...sessions.values()];
    const session = all[Math.floor(Math.random() * all.length)];
    if (!session) return;
    const { tool, effect } = pickTool(Math.random);
    session.left -= 1;
    const failed = Math.random() < 0.04;
    const trace: TraceEvent = {
      id: `trc_live${Math.random().toString(36).slice(2, 14).toUpperCase()}`,
      at: new Date().toISOString(),
      kind: 'tool_call',
      tool,
      lane: session.lane,
      principal: session.principal,
      effect,
      status: failed ? 'failed' : 'succeeded_verified',
      summary: `${tool.replace('browser_', '')} on ${laneName(session.lane.split('/')[1] ?? '')}`,
      cost: { tokens: 0, usd: 0 },
      run_id: null,
    };
    data.activity.unshift(trace);
    if (data.activity.length > 4_000) data.activity.length = 4_000;
    live.set(session.lane, { lane: session.lane, principal: session.principal, tool, since: session.since, run_id: null });
    emit(slug, { type: 'trace', payload: trace });
    if (session.left <= 0) {
      sessions.delete(session.lane);
      live.delete(session.lane);
    }
  };

  const loop = () => {
    step();
    timer = setTimeout(loop, 500 + Math.random() * 1_400);
  };
  let timer = setTimeout(loop, 900);
  return () => clearTimeout(timer);
}
