/**
 * What this computer's ops pages show on the mock: its health, backups, version, environments, Web Bot
 * identity and settings. Built once per org on first use, so a change made here sticks until the page
 * is reloaded. Nothing here holds a secret: a backup's passphrase is typed into the node's own form, and
 * the mock has no node to serve one, so a mock backup starts with no form and simply runs.
 */
import { delay, http, HttpResponse } from 'msw';
import type {
  BackupItem,
  BackupJob,
  IdentityAccount,
  NodeBackups,
  ServiceView,
  SettingRow,
} from '@/api/types';
import type { Helpers } from './s3';
import type { NodeRecord, OrgData } from './seed';
import { nodeHealth, reportsDepth } from './fleet';

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
const iso = (at: number) => new Date(at).toISOString();
const KEEP = 7;
const MIB = 1024 * 1024;

interface OpsState {
  backups: BackupItem[];
  job: (BackupJob & { finishes_at: number }) | null;
  services: Record<string, ServiceView & { logs: string[] }>;
  identity: Record<string, IdentityAccount>;
}

const states = new Map<string, OpsState>();

function stateOf(data: OrgData, node: NodeRecord): OpsState {
  const key = `${data.org.slug}/${node.id}`;
  const known = states.get(key);
  if (known) return known;
  const now = Date.now();
  const lanes = data.lanes.filter((lane) => lane.node === node.id);
  const workspace = data.workspaces[0]?.id ?? lanes[0]?.workspace ?? 'biz-a';
  const made: OpsState = {
    backups: [
      {
        name: `agentos-${node.id}-${stamp(now - DAY)}.tar.gz.age`,
        created_at: iso(now - DAY),
        size: 412 * MIB,
        check: { state: 'verified', at: iso(now - DAY + 3 * MINUTE), error: null },
      },
      {
        name: `agentos-${node.id}-${stamp(now - 8 * DAY)}.tar.gz.age`,
        created_at: iso(now - 8 * DAY),
        size: 398 * MIB,
        check: { state: 'not_checked', at: null, error: null },
      },
    ],
    job: null,
    services: {
      [`${workspace}/portal-api`]: {
        lane: `${workspace}/portal-api`,
        desired: 'running',
        state: 'running',
        health: 'healthy',
        restarts: 1,
        last_exit: 1,
        started_at: iso(now - 19 * HOUR),
        logs: ['listening on :8080', 'GET /health 200', 'GET /orders 200', 'GET /orders 200'],
      },
      [`${workspace}/price-feed`]: {
        lane: `${workspace}/price-feed`,
        desired: 'stopped',
        state: 'stopped',
        health: 'none',
        restarts: 0,
        last_exit: null,
        started_at: null,
        logs: ['price feed stopped'],
      },
    },
    identity: Object.fromEntries(
      lanes.map((lane, index) => [
        lane.id,
        {
          lane: lane.id,
          state: index === 0 ? 'signing' : 'none',
          keyid: index === 0 ? 'jkt_6f1c2a9d' : null,
          signature_agent: index === 0 ? 'https://agents.example' : null,
          hosts:
            index === 0
              ? [{ origin: 'https://www.example.com', reason: 'read-only research', added_by: 'owner:panel', added_at: iso(now - 3 * DAY) }]
              : [],
        } satisfies IdentityAccount,
      ]),
    ),
  };
  states.set(key, made);
  return made;
}

function stamp(at: number): string {
  return new Date(at).toISOString().replace(/[-:]/g, '').replace(/\.\d+Z$/, 'Z');
}

/** A job ends on its own once its time has come, so the list reads the same without any timer. */
function settle(state: OpsState): void {
  const job = state.job;
  if (!job || job.state !== 'running' || Date.now() < job.finishes_at) return;
  const at = iso(job.finishes_at);
  if (job.kind === 'backup') {
    const name = `agentos-${job.name ?? 'node'}-${stamp(job.finishes_at)}.tar.gz.age`;
    state.backups = [{ name, created_at: at, size: 420 * MIB, check: { state: 'verified', at, error: null } }, ...state.backups];
    state.backups = state.backups.slice(0, KEEP);
    job.message = `Written and checked: ${name}.`;
  } else {
    state.backups = state.backups.map((item) =>
      item.name === job.name ? { ...item, check: { state: 'verified', at, error: null } } : item,
    );
    job.message = `${job.name} checks out.`;
  }
  job.state = 'done';
}

function backupsOf(state: OpsState, node: NodeRecord): NodeBackups {
  settle(state);
  const job = state.job ? { ...state.job } : null;
  return {
    folder: `/var/lib/agentos/${node.id}/backups`,
    keep: KEEP,
    items: state.backups,
    job: job && { id: job.id, kind: job.kind, name: job.name, state: job.state, message: job.message, started_at: job.started_at, finished_at: job.finished_at },
  };
}

const SETTINGS = (node: NodeRecord): SettingRow[] => [
  { name: 'name', value: node.id, source: 'node.toml', description: "The node's name, shown by whoami and in the panel." },
  { name: 'home', value: `/var/lib/agentos/${node.id}`, source: 'default', description: 'Where the node keeps its data (AGENTOS_HOME wins over this).' },
  { name: 'max_warm_lanes', value: node.max_warm_lanes, source: 'node.toml', description: 'How many accounts may be open at once.' },
  { name: 'mcp_port', value: 7400, source: 'default', description: 'The port the node listens on for agents.' },
  { name: 'mcp_token', value: '(set, hidden)', source: 'node.toml', description: 'The token agents use to reach this node. Never shown.' },
  { name: 'timezone', value: node.timezone ?? 'UTC', source: 'node.toml', description: 'The clock schedules and quiet hours use.' },
];

export function opsHandlers({ org, failure }: Pick<Helpers, 'org' | 'failure'>) {
  const onNode = (data: OrgData, id: string) => {
    const node = data.nodes.find((item) => item.id === id);
    return node && reportsDepth(node) ? node : null;
  };
  const missing = () => failure(404, 'not_found', 'Not found.');
  return [
    http.post(
      '/api/orgs/:org/nodes/:id/health/run',
      org(async (data, { params }) => {
        const node = onNode(data, String(params.id));
        if (!node) return missing();
        await delay(900);
        const at = iso(Date.now());
        return HttpResponse.json({ ran_at: at, checks: nodeHealth(data, node).map((check) => ({ ...check, at })) });
      }),
    ),
    http.get(
      '/api/orgs/:org/nodes/:id/health/rounds',
      org((data, { params }) => {
        const node = onNode(data, String(params.id));
        if (!node) return missing();
        const at = iso(Date.now() - 2 * MINUTE);
        if (node.id === 'hetzner-1') {
          return HttpResponse.json({
            at,
            level: 'warn',
            findings: [{ key: 'disk', kind: 'disk', level: 'warn', title: 'Disk space', detail: 'Under 100 GB free' }],
          });
        }
        return HttpResponse.json({ at, level: 'ok', findings: [] });
      }),
    ),
    http.get(
      '/api/orgs/:org/nodes/:id/backups',
      org((data, { params }) => {
        const node = onNode(data, String(params.id));
        if (!node) return missing();
        return HttpResponse.json(backupsOf(stateOf(data, node), node));
      }),
    ),
    http.post(
      '/api/orgs/:org/nodes/:id/backups',
      org((data, { params }) => {
        const node = onNode(data, String(params.id));
        if (!node) return missing();
        const state = stateOf(data, node);
        settle(state);
        if (state.job?.state === 'running') return failure(409, 'busy', 'A backup is already running. It takes a few minutes, and the Backups tab says when it is done.');
        const now = Date.now();
        state.job = { id: `job_${now}`, kind: 'backup', name: node.id, state: 'running', message: '', started_at: iso(now), finished_at: null, finishes_at: now + 6_000 };
        return HttpResponse.json({ id: `trip_${now}`, kind: 'backup', name: null, stage: 'running', form_url: null });
      }),
    ),
    http.post(
      '/api/orgs/:org/nodes/:id/backups/check',
      org(async (data, { params, request }) => {
        const node = onNode(data, String(params.id));
        if (!node) return missing();
        const state = stateOf(data, node);
        settle(state);
        const { name } = (await request.json()) as { name: string };
        if (!state.backups.some((item) => item.name === name)) return missing();
        if (state.job?.state === 'running') return failure(409, 'busy', 'A backup is already running. Check this one when it is done.');
        const now = Date.now();
        state.job = { id: `job_${now}`, kind: 'check', name, state: 'running', message: '', started_at: iso(now), finished_at: null, finishes_at: now + 3_000 };
        return HttpResponse.json({ id: `trip_${now}`, kind: 'check', name, stage: 'running', form_url: null });
      }),
    ),
    http.get(
      '/api/orgs/:org/nodes/:id/upgrade',
      org((data, { params }) => {
        const node = onNode(data, String(params.id));
        if (!node) return missing();
        return HttpResponse.json({ running: node.version, newer: null, command: 'agentos upgrade --source <your agentos checkout>', python: '3.12.4' });
      }),
    ),
    http.get(
      '/api/orgs/:org/nodes/:id/environments',
      org((data, { params }) => {
        const node = onNode(data, String(params.id));
        if (!node) return missing();
        const state = stateOf(data, node);
        const lanes = data.lanes.filter((lane) => lane.node === node.id).slice(0, 2);
        return HttpResponse.json({
          lanes: lanes.map((lane) => ({
            id: `${lane.workspace}/${lane.name}-shell`,
            kind: 'terminal',
            preset: 'sandbox',
            state: 'cold',
            isolation: 'Its own throwaway filesystem, with the network off',
            network: 'off',
            base_image: 'dev-box@1',
            last_used: null,
            quarantine_reason: null,
          })),
          services: Object.values(state.services).map(({ logs: _logs, ...row }) => ({
            lane: row.lane,
            name: row.lane.split('/')[1] ?? row.lane,
            state: row.state,
            desired: row.desired,
            health: row.health,
            restarts: row.restarts,
            last_exit: row.last_exit,
            started_at: row.started_at,
          })),
          images: {
            templates: [{ name: 'dev-box', description: 'Shell and toolchains: git, Python and Node.', placeholder: false }],
            built: [{ ref: 'dev-box@1', name: 'dev-box', version: 1, built_at: iso(Date.now() - 4 * DAY), size_bytes: 1_240 * MIB }],
          },
        });
      }),
    ),
    http.post(
      '/api/orgs/:org/nodes/:id/services/:ws/:name/:action',
      org((data, { params }) => {
        const node = onNode(data, String(params.id));
        if (!node) return missing();
        const state = stateOf(data, node);
        const lane = `${params.ws}/${params.name}`;
        const row = state.services[lane];
        if (!row) return failure(404, 'lane_not_found', 'There is no such service on this computer.');
        const now = iso(Date.now());
        if (params.action === 'start') {
          Object.assign(row, { desired: 'running', state: 'running', health: 'healthy', started_at: now });
          row.logs = [...row.logs, 'started'];
        } else {
          Object.assign(row, { desired: 'stopped', state: 'stopped', health: 'none', started_at: null });
          row.logs = [...row.logs, 'stopped'];
        }
        const { logs: _logs, ...view } = row;
        return HttpResponse.json(view);
      }),
    ),
    http.get(
      '/api/orgs/:org/nodes/:id/services/:ws/:name/logs',
      org((data, { params, request }) => {
        const node = onNode(data, String(params.id));
        if (!node) return missing();
        const row = stateOf(data, node).services[`${params.ws}/${params.name}`];
        if (!row) return failure(404, 'lane_not_found', 'There is no such service on this computer.');
        const lines = Number(new URL(request.url).searchParams.get('lines') ?? 100);
        return HttpResponse.json({ lane: row.lane, log: row.logs.slice(-Math.max(1, lines)).join('\n') });
      }),
    ),
    http.get(
      '/api/orgs/:org/nodes/:id/identity',
      org((data, { params }) => {
        const node = onNode(data, String(params.id));
        if (!node) return missing();
        return HttpResponse.json({ accounts: Object.values(stateOf(data, node).identity) });
      }),
    ),
    http.post(
      '/api/orgs/:org/nodes/:id/identity/:ws/:name/sites',
      org(async (data, { params, request }) => {
        const node = onNode(data, String(params.id));
        if (!node) return missing();
        const account = stateOf(data, node).identity[`${params.ws}/${params.name}`];
        if (!account) return missing();
        if (!account.signature_agent) {
          return failure(400, 'invalid', `${account.lane} has no identity yet. Make one on the command line: agentos identity init ${account.lane} --agent <https URL>.`);
        }
        const body = (await request.json()) as { origin: string; reason: string };
        const origin = body.origin.startsWith('http') ? body.origin : `https://${body.origin}`;
        const hosts = account.hosts.filter((host) => host.origin !== origin);
        account.hosts = [...hosts, { origin, reason: body.reason, added_by: 'owner:panel', added_at: iso(Date.now()) }];
        account.state = 'signing';
        return HttpResponse.json(account);
      }),
    ),
    http.delete(
      '/api/orgs/:org/nodes/:id/identity/:ws/:name/sites',
      org((data, { params, request }) => {
        const node = onNode(data, String(params.id));
        if (!node) return missing();
        const account = stateOf(data, node).identity[`${params.ws}/${params.name}`];
        if (!account) return missing();
        const origin = new URL(request.url).searchParams.get('origin') ?? '';
        if (!account.hosts.some((host) => host.origin === origin)) return failure(404, 'not_found', `${account.lane} does not sign for ${origin}.`);
        account.hosts = account.hosts.filter((host) => host.origin !== origin);
        account.state = account.hosts.length ? 'signing' : 'off';
        return HttpResponse.json(account);
      }),
    ),
    http.get(
      '/api/orgs/:org/nodes/:id/settings',
      org((data, { params }) => {
        const node = onNode(data, String(params.id));
        if (!node) return missing();
        return HttpResponse.json(SETTINGS(node));
      }),
    ),
  ];
}

