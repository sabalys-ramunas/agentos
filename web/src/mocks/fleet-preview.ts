/**
 * The map at other sizes, for proving it fits without zoom (canvas.md P8): `?mock=preview-3` is
 * one person with three accounts on one Mac mini, `?mock=preview-120` is fifteen businesses with
 * 120 accounts on five computers, one of them offline. Shaped exactly as the real cloud sends the
 * topology (api-for-rebuild.md section 6), with ids that never meet the demo world's.
 */
import type { HealthState, Topology, TopologyActive, TopologyEntity, TopologyLink, TopologyRecent } from '@/api/types';
import { SITES } from '@/lib/sites';

function rng(seed: number) {
  let state = seed | 0;
  return () => {
    state = (state + 0x6d2b79f5) | 0;
    let t = Math.imul(state ^ (state >>> 15), 1 | state);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

interface Plan {
  businesses: { name: string; color: string; machine: string; sites: string[] }[];
  machines: { id: string; down?: boolean; max: number }[];
  /** Account states by `business/site`, everything else is drawn by chance. */
  states: Record<string, HealthState>;
  working: { lane: string; principal: string; kind: 'agent' | 'flow' }[];
}

const COLORS = ['#a0763f', '#5f8a3a', '#3f76a0', '#c2553d', '#b0477a', '#3f9a8c', '#6b7280', '#3f76a0', '#5f5fd0', '#5f8a3a', '#b0477a', '#a0763f', '#3f9a8c', '#8c5fa8', '#525252'];
const NAMES = ['Aster Coffee', 'Birch & Co', 'Cobalt Labs', 'Dune Outfitters', 'Ember Bakery', 'Fjord Rentals', 'Granite Legal', 'Harbor Supply', 'Indigo Dental', 'Juniper Yoga', 'Kite Media', 'Lumen Optics', 'Meridian Travel', 'Solstice Studio', 'Personal'];
const SIZES = [9, 8, 8, 9, 7, 8, 9, 8, 8, 7, 9, 8, 8, 9, 5];
const SITE_LIST = ['instagram', 'facebook', 'linkedin', 'youtube', 'x', 'reddit', 'medium', 'blog', 'shopify'];

// Personal is `own` here, so it never meets the demo world's `personal`.
const slug = (name: string) => (name === 'Personal' ? 'own' : name.toLowerCase().replaceAll(/[^a-z0-9]+/g, '-').replace(/-$/, ''));

function plan(size: number): Plan {
  if (size <= 3) {
    return {
      businesses: [{ name: 'Personal', color: '#525252', machine: 'my-mini', sites: ['instagram', 'linkedin', 'terminal'] }],
      machines: [{ id: 'my-mini', max: 4 }],
      states: { 'own/terminal': 'asleep', 'own/instagram': 'ok' },
      working: [{ lane: 'own/linkedin', principal: 'claude-code', kind: 'agent' }],
    };
  }
  const machines = ['office-mini', 'studio-imac', 'hetzner-2', 'cloud-3', 'cloud-4'];
  return {
    businesses: NAMES.map((name, index) => ({
      name,
      color: COLORS[index] ?? '#6b7280',
      machine: name === 'Ember Bakery' ? 'cloud-4' : name === 'Personal' ? 'office-mini' : (machines[index % 4] as string),
      sites: name === 'Personal' ? ['instagram', 'facebook', 'linkedin', 'reddit', 'terminal'] : SITE_LIST.slice(0, SIZES[index] ?? 8),
    })),
    machines: [{ id: 'office-mini', max: 4 }, { id: 'studio-imac', max: 8 }, { id: 'hetzner-2', max: 6 }, { id: 'cloud-3', max: 8 }, { id: 'cloud-4', max: 8, down: true }],
    states: {
      'aster-coffee/instagram': 'logged_out',
      'granite-legal/youtube': 'captcha',
      'kite-media/linkedin': 'checkpoint',
      'lumen-optics/reddit': 'quarantined',
      'solstice-studio/instagram': 'blocked',
    },
    working: [
      { lane: 'aster-coffee/medium', principal: 'claude-code', kind: 'agent' },
      { lane: 'cobalt-labs/facebook', principal: 'chatgpt-app', kind: 'agent' },
      { lane: 'indigo-dental/medium', principal: 'indigo-dental.medium.post', kind: 'flow' },
    ],
  };
}

/** The states the cloud counts as a thing waiting for a person (api-for-rebuild.md, counting rules). */
const WAITING_FOR_A_PERSON: HealthState[] = ['captcha', 'checkpoint', 'logged_out', 'blocked', 'rate_limited', 'quarantined'];

/** `status.needs_you` for a preview, so the bell and the map's sentence count the preview's own accounts. */
export function previewNeedYou(size: number): number {
  return Object.values(plan(size).states).filter((health) => WAITING_FOR_A_PERSON.includes(health)).length;
}

export function previewTopology(size: number, windowSeconds: number): Topology {
  const now = Date.now();
  const random = rng(size);
  const { businesses, machines, states, working } = plan(size);
  const entities: TopologyEntity[] = [{ id: 'cloud:agentos', type: 'cloud', label: 'agentos Cloud', state: 'ok', ref: null, stats: {} }];
  const links: TopologyLink[] = [];
  const link = (from: string, to: string, kind: TopologyLink['kind']): TopologyLink => ({ from, to, kind, calls: 0, failed: 0, last_at: null });

  const lanesOn = new Map<string, number>();
  for (const business of businesses) lanesOn.set(business.machine, (lanesOn.get(business.machine) ?? 0) + business.sites.length);
  for (const machine of machines) {
    const lanes = lanesOn.get(machine.id) ?? 0;
    entities.push({
      id: `node:${machine.id}`,
      type: 'node',
      label: machine.id,
      state: machine.down ? 'down' : 'ok',
      ref: { href: `/nodes/${machine.id}` },
      stats: { lanes, warm_lanes: machine.down ? 0 : Math.min(machine.max, Math.ceil(lanes / 3)), max_warm_lanes: machine.max, cpu_pct: machine.down ? null : Math.round(15 + random() * 55) },
    });
    links.push(link(`node:${machine.id}`, 'cloud:agentos', 'attached'));
  }

  const sites = new Map<string, number>();
  for (const business of businesses) {
    const ws = slug(business.name);
    entities.push({ id: `workspace:${ws}`, type: 'workspace', label: business.name, state: 'ok', ref: { href: `/?workspace=${ws}` }, stats: { lanes: business.sites.length, color: business.color } });
    for (const site of business.sites) {
      const id = `${ws}/${site}`;
      const roll = random();
      const health: HealthState = states[id] ?? (working.some((item) => item.lane === id) ? 'working' : roll > 0.55 ? 'ok' : 'asleep');
      const state = health === 'ok' || health === 'working' ? 'ok' : health === 'asleep' ? 'idle' : health === 'quarantined' || health === 'blocked' ? 'blocked' : 'attention';
      const known = SITES.get(site);
      entities.push({
        id: `lane:${id}`,
        type: 'lane',
        label: site,
        state,
        ref: { href: `/lanes/${id}` },
        stats: { kind: site === 'terminal' ? 'terminal' : 'browser', health, site, site_label: site === 'terminal' ? 'Computer' : (known?.name ?? site), calls: Math.floor(random() * 30), failed: 0 },
      });
      links.push(link(`lane:${id}`, `node:${business.machine}`, 'runs_on'), link(`lane:${id}`, `workspace:${ws}`, 'belongs_to'));
      if (site !== 'terminal') {
        sites.set(site, (sites.get(site) ?? 0) + 1);
        links.push(link(`lane:${id}`, `site:${site}`, 'acts_on'));
      }
    }
  }
  for (const [site, lanes] of sites) {
    const known = SITES.get(site);
    entities.push({ id: `site:${site}`, type: 'site', label: known?.name ?? site, state: 'ok', ref: null, stats: { lanes, key: site, domain: known?.domains[0] ?? null, source: 'account' } });
  }

  const agents = [
    { id: 'claude-code', brand: 'claude' },
    { id: 'chatgpt-app', brand: 'chatgpt' },
    { id: 'codex', brand: 'codex' },
  ];
  for (const agent of agents) {
    entities.push({ id: `agent:${agent.id}`, type: 'agent', label: agent.id, state: 'ok', ref: null, stats: { kind: 'agent', brand: agent.brand, calls: 12, failed: 0 } });
  }
  entities.push({ id: 'agent:routines', type: 'agent', label: 'Routines', state: 'ok', ref: { href: '/flows' }, stats: { kind: 'flow', calls: 40, failed: 1 } });
  for (const item of working) links.push(link(item.kind === 'flow' ? 'agent:routines' : `agent:${item.principal}`, `lane:${item.lane}`, 'attached'));

  const active: TopologyActive[] = working
    .filter((item) => item.kind === 'agent')
    .map((item) => ({ lane: item.lane, principal: item.principal, tool: 'browser_act', since: new Date(now - 40_000).toISOString(), run_id: null }));
  const lanes = entities.filter((entity) => entity.type === 'lane' && entity.stats.health !== 'asleep');
  const recent: TopologyRecent[] = [];
  for (let index = 0; index < Math.min(14, lanes.length * 2); index += 1) {
    const lane = lanes[Math.floor(random() * lanes.length)];
    if (!lane) continue;
    const id = lane.id.replace(/^lane:/, '');
    recent.push({ at: new Date(now - 60_000 - index * 37_000).toISOString(), lane: id, principal: `${id.replace('/', '.')}.check`, kind: 'flow', tool: 'browser_snapshot', status: 'succeeded_verified' });
  }
  const flow = working.find((item) => item.kind === 'flow');
  if (flow) recent.unshift({ at: new Date(now - 5_000).toISOString(), lane: flow.lane, principal: flow.principal, kind: 'flow', tool: 'browser_act', status: 'succeeded_unverified' });
  return { generated_at: new Date(now).toISOString(), window_seconds: windowSeconds, entities, links, active, recent };
}
