/**
 * The mock of Environments (`/environments`, docs/design/environments.md "Web routes"): the cloud computers
 * agents rent for one chat. northwind-group has three open (one working on a dev server with its browser on
 * localhost:3000, one starting, one idle with unsaved work) and six closed; any other org has none, and so
 * does `?mock=no-envs`. The starting one is ready after a little while (never under `still`). A close with
 * unsaved work answers 409 `unsaved_work` unless it says `discard`; a close takes a moment, then it is closed.
 *
 * Each open one has its browser lane (`env/<id>-browser`) among the org's lanes, so the live view and take
 * over work as on any account. The jobs its terminal ran answer `/jobs/{id}` here, so a command links to a
 * real run page; any other run falls through to the Runs mock.
 */
import { http, HttpResponse } from 'msw';
import type { EnvCall, EnvCommand, EnvDetail, EnvMachine, EnvState, EnvSummary, Job, JobDetail, Lane } from '@/api/types';
import { idleControl } from '@/lib/lanes';
import { scenario } from './scenario';
import type { Helpers } from './s3';
import type { MockDb, OrgData } from './seed';

const SECOND = 1_000;
const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
/** How long the starting one takes to boot, from when the mock was made. */
const BOOT_MS = 25 * SECOND;
/** How long a close takes before the machine is gone. */
const CLOSE_MS = 2_500;
const OPERATE = new Set(['owner', 'admin', 'member']);
const encoder = new TextEncoder();
const decoder = new TextDecoder();

export interface MockEnv {
  detail: EnvDetail;
  /** When a starting one is ready, or a closing one is closed (ms since the epoch). */
  until?: number;
  /** The jobs its terminal ran, with their output, for `/jobs/{id}`. */
  jobs: { job: Job; stdout: string }[];
}

const SMALL: EnvMachine = { provider: 'hetzner', server_type: 'cpx22', cpu: 2, memory_gb: 4, location: 'nbg1' };
const MEDIUM: EnvMachine = { provider: 'hetzner', server_type: 'cpx32', cpu: 4, memory_gb: 8, location: 'hel1' };
const PRICE: Record<string, number> = { cpx22: 0.0121, cpx32: 0.0213 };

const at = (now: number, ms: number) => new Date(now + ms).toISOString();
const costOf = (machine: EnvMachine, minutes: number) => Math.round(((PRICE[machine.server_type] ?? 0.0121) * minutes) / 60 * 10_000) / 10_000;

function call(now: number, secondsAgo: number, tool: string, summary: string, ok = true): EnvCall {
  return { at: at(now, -secondsAgo * SECOND), tool, summary, ok };
}

function command(now: number, id: string, cmd: string, state: string, minutesAgo: number, exit_code: number | null = null): EnvCommand {
  return { id, cmd, state, started_at: at(now, -minutesAgo * MINUTE), exit_code };
}

interface Seed {
  id: string;
  title: string;
  state: EnvState;
  repo: string | null;
  branch: string | null;
  /** Minutes since it opened, and how long it ran when it is closed. */
  age: number;
  ran?: number;
  lastCall: number | null;
  /** Minutes left before its time limit, for an open one. */
  left: number;
  machine: EnvMachine;
  agent: EnvSummary['agent'];
}

function summaryOf(now: number, seed: Seed): EnvSummary {
  const open = seed.state !== 'closed' && seed.state !== 'failed';
  const ran = open ? seed.age : (seed.ran ?? seed.age);
  const created = now - seed.age * MINUTE;
  return {
    id: seed.id,
    title: seed.title,
    state: seed.state,
    repo: seed.repo,
    branch: seed.branch,
    created_at: new Date(created).toISOString(),
    last_call_at: seed.lastCall === null ? null : at(now, -seed.lastCall * SECOND),
    closed_at: open ? null : new Date(created + ran * MINUTE).toISOString(),
    expires_at: open ? at(now, seed.left * MINUTE) : new Date(created + 4 * HOUR).toISOString(),
    machine: seed.machine,
    price_hourly_eur: PRICE[seed.machine.server_type] ?? 0.0121,
    cost_eur: costOf(seed.machine, ran),
    agent: seed.agent,
    lanes: { terminal: `env/${seed.id}`, browser: `env/${seed.id}-browser` },
  };
}

const NEXT_DEV = [
  '> shop@0.1.0 dev /work/shop',
  '> next dev --port 3000',
  '',
  '   ▲ Next.js 15.1.4',
  '   - Local:        http://localhost:3000',
  '   - Network:      http://10.0.0.4:3000',
  '',
  ' ✓ Starting...',
  ' ✓ Ready in 1.8s',
  ' ○ Compiling /offer ...',
  ' ✓ Compiled /offer in 912ms (1204 modules)',
  ' GET /offer 200 in 1043ms',
  ' ✓ Compiled in 188ms (1190 modules)',
  ' GET /offer 200 in 64ms',
  '',
].join('\n');

const INSTALL = [
  'Lockfile is up to date, resolution step is skipped',
  'Packages: +1032',
  '++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++++',
  'Progress: resolved 1032, reused 0, downloaded 1032, added 1032, done',
  '',
  'dependencies:',
  '+ next 15.1.4',
  '+ react 19.0.0',
  '+ react-dom 19.0.0',
  '',
  'Done in 21.4s',
  '',
].join('\n');

const VITEST = [
  ' RUN  v2.1.8 /work/back-office',
  '',
  ' ✓ src/invoices/csv.test.ts (18 tests) 64ms',
  ' ✓ src/invoices/totals.test.ts (9 tests) 22ms',
  '',
  ' Test Files  2 passed (2)',
  '      Tests  27 passed (27)',
  '   Duration  1.92s',
  '',
].join('\n');

function job(now: number, env: string, id: string, cmd: string, cwd: string, state: Job['state'], minutesAgo: number, took: number | null, stdout: string): { job: Job; stdout: string } {
  const started = now - minutesAgo * MINUTE;
  return {
    job: {
      id,
      lane: `env/${env}`,
      workspace: 'env',
      lane_name: env,
      node: `env-${env}`,
      command: cmd,
      cwd,
      state,
      exit_code: state === 'running' ? null : state === 'passed' ? 0 : 1,
      signal: null,
      oom_killed: false,
      started_at: new Date(started).toISOString(),
      finished_at: took === null ? null : new Date(started + took * SECOND).toISOString(),
      duration_s: took,
      timeout_s: 14_400,
      started_by: 'Claude Code',
      stdout_bytes: encoder.encode(stdout).length,
      stderr_bytes: 0,
      dropped_bytes: 0,
      sync: null,
      note: null,
    },
    stdout,
  };
}

/** The org's environments: three open and six closed in northwind-group. */
export function seedEnvironments(now = Date.now()): MockEnv[] {
  const claude = { name: 'Claude Code', client: 'T3 Code' };
  const offer: Seed = {
    id: 'e7k2', title: 'Black Friday offer page', state: 'working', repo: 'github.com/harbor-supply/shop', branch: 'feat/black-friday-offer',
    age: 14, lastCall: 25, left: 106, machine: SMALL, agent: claude,
  };
  const flaky: Seed = {
    id: 'q3mn', title: 'Fix the flaky checkout test', state: 'starting', repo: 'github.com/harbor-supply/shop', branch: 'main',
    age: 1, lastCall: 40, left: 239, machine: MEDIUM, agent: { name: 'Codex', client: null },
  };
  const invoices: Seed = {
    id: 'w8tp', title: 'Invoice export to CSV', state: 'idle', repo: 'github.com/meridian-travel/back-office', branch: 'fix/invoice-csv-export',
    age: 72, lastCall: 23 * 60, left: 48, machine: { ...SMALL, location: 'fsn1' }, agent: { name: 'Claude Code', client: null },
  };
  const devJob = job(now, 'e7k2', 'job-8c1f0e2a3b', 'pnpm dev', '/work/shop', 'running', 11, null, NEXT_DEV);
  const installJob = job(now, 'e7k2', 'job-7b0e9d1c2a', 'pnpm install', '/work/shop', 'passed', 13, 21.4, INSTALL);
  const testJob = job(now, 'w8tp', 'job-5d2c8b7a6f', 'pnpm vitest run src/invoices', '/work/back-office', 'passed', 30, 4.1, VITEST);

  const open: MockEnv[] = [
    {
      jobs: [devJob, installJob],
      detail: {
        ...summaryOf(now, offer),
        ports: [
          { port: 3000, process: 'next-server', link: '/api/orgs/northwind-group/environments/e7k2/ports/3000/' },
          { port: 9229, process: 'node', link: null },
        ],
        git: {
          branch: 'feat/black-friday-offer',
          ahead: 0,
          dirty: 0,
          pushed: [
            { branch: 'feat/black-friday-offer', commit: 'a41c9e2b7d03f5e8c1a9b6d2e4f70c3a8b5d1e96', at: at(now, -2 * MINUTE) },
            { branch: 'feat/black-friday-offer', commit: '6e0d3b8a2c71f4e9d5b0a3c8e1f62d7b4a9c0e35', at: at(now, -7 * MINUTE) },
          ],
        },
        calls: [
          call(now, 25, 'ui_check', 'http://localhost:3000/offer at 360, 768 and 1280: 1 minor'),
          call(now, 70, 'lane_exec', 'git push origin feat/black-friday-offer'),
          call(now, 95, 'lane_exec', 'git commit -m "Offer: the yearly plans show the old price struck through"'),
          call(now, 160, 'browser_act', 'click "Claim the deal"'),
          call(now, 190, 'browser_snapshot', 'localhost:3000/offer, 42 refs'),
          call(now, 230, 'ui_check', 'http://localhost:3000/offer at 360: 1 major, the price wraps under the badge', false),
          call(now, 300, 'browser_navigate', 'http://localhost:3000/offer'),
          call(now, 420, 'lane_exec', 'git push origin feat/black-friday-offer'),
          call(now, 660, 'lane_job_start', 'pnpm dev'),
          call(now, 780, 'lane_job_start', 'pnpm install'),
          call(now, 800, 'lane_exec', 'git switch -c feat/black-friday-offer'),
          call(now, 830, 'environment_open', 'Black Friday offer page, github.com/harbor-supply/shop'),
        ],
        commands: [
          command(now, devJob.job.id, 'pnpm dev', 'running', 11),
          command(now, 'exec-31f0a9', 'git push origin feat/black-friday-offer', 'passed', 1, 0),
          command(now, 'exec-2c7e44', 'git commit -m "Offer: the yearly plans show the old price struck through"', 'passed', 2, 0),
          command(now, 'exec-1b5d02', 'git push origin feat/black-friday-offer', 'passed', 7, 0),
          command(now, installJob.job.id, 'pnpm install', 'passed', 13, 0),
          command(now, 'exec-0a4c91', 'git switch -c feat/black-friday-offer', 'passed', 13, 0),
        ],
        viewer: { path: '/api/orgs/northwind-group/lanes/env/e7k2-browser/vnc' },
        closed_reason: null,
        unsaved: null,
      },
    },
    {
      jobs: [],
      until: scenario.has('still') ? undefined : now + BOOT_MS,
      detail: {
        ...summaryOf(now, flaky),
        ports: [],
        git: { branch: 'main', ahead: 0, dirty: 0, pushed: [] },
        calls: [call(now, 40, 'environment_open', 'Fix the flaky checkout test, github.com/harbor-supply/shop')],
        commands: [],
        viewer: null,
        closed_reason: null,
        unsaved: null,
      },
    },
    {
      jobs: [testJob],
      detail: {
        ...summaryOf(now, invoices),
        ports: [{ port: 5173, process: 'vite', link: '/api/orgs/northwind-group/environments/w8tp/ports/5173/' }],
        git: { branch: 'fix/invoice-csv-export', ahead: 2, dirty: 3, pushed: [] },
        calls: [
          call(now, 23 * 60, 'lane_exec', 'git commit -m "Invoices: quote fields that hold a comma"'),
          call(now, 24 * 60, 'lane_exec', 'git commit -m "Invoices: one row per line item, with the VAT rate"'),
          call(now, 26 * 60, 'browser_snapshot', 'localhost:5173/invoices, 88 refs'),
          call(now, 29 * 60, 'lane_job_start', 'pnpm vitest run src/invoices'),
          call(now, 41 * 60, 'browser_navigate', 'http://localhost:5173/invoices'),
          call(now, 44 * 60, 'lane_job_start', 'pnpm dev --port 5173'),
          call(now, 70 * 60, 'environment_open', 'Invoice export to CSV, github.com/meridian-travel/back-office'),
        ],
        commands: [
          command(now, 'exec-9e1d77', 'git commit -m "Invoices: quote fields that hold a comma"', 'passed', 23, 0),
          command(now, 'exec-8d0c66', 'git commit -m "Invoices: one row per line item, with the VAT rate"', 'passed', 24, 0),
          command(now, testJob.job.id, 'pnpm vitest run src/invoices', 'passed', 30, 0),
          command(now, 'exec-7cfb55', 'pnpm dev --port 5173', 'running', 44),
        ],
        viewer: { path: '/api/orgs/northwind-group/lanes/env/w8tp-browser/vnc' },
        closed_reason: null,
        unsaved: {
          dirty_files: ['src/invoices/csv.ts', 'src/invoices/csv.test.ts', 'src/invoices/export-button.tsx'],
          unpushed: ['4b8e2d1 Invoices: quote fields that hold a comma', '9a3c7f0 Invoices: one row per line item, with the VAT rate'],
        },
      },
    },
  ];

  const closed: [Seed, string, number][] = [
    [{ id: 'p2lc', title: 'Pricing page copy', state: 'closed', repo: 'github.com/harbor-supply/shop', branch: 'copy/pricing-page', age: 190, ran: 38, lastCall: 0, left: 0, machine: SMALL, agent: claude }, 'agent', 1],
    [{ id: 'v6up', title: 'Upgrade Vite to 6', state: 'closed', repo: 'github.com/meridian-travel/back-office', branch: 'chore/vite-6', age: 26 * 60, ran: 131, lastCall: 0, left: 0, machine: SMALL, agent: { name: 'Codex', client: null } }, 'idle', 2],
    [{ id: 'f9sr', title: 'Search results pagination', state: 'failed', repo: 'github.com/harbor-supply/shop', branch: 'main', age: 29 * 60, ran: 4, lastCall: 0, left: 0, machine: MEDIUM, agent: claude }, 'boot_failed', 0],
    [{ id: 'o4em', title: 'Onboarding email preview', state: 'closed', repo: 'github.com/solstice-studio/mailers', branch: 'feat/welcome-preview', age: 2 * 24 * 60, ran: 52, lastCall: 0, left: 0, machine: SMALL, agent: { name: 'Claude Code', client: 'Claude Code' } }, 'owner', 0],
    [{ id: 'd1dm', title: 'Dark mode for the dashboard', state: 'closed', repo: 'github.com/harbor-supply/admin', branch: 'feat/dark-mode', age: 3 * 24 * 60, ran: 240, lastCall: 0, left: 0, machine: MEDIUM, agent: claude }, 'expired', 3],
    [{ id: 'a8fx', title: 'Accessibility pass on the booking forms, labels, focus order and the error messages read aloud', state: 'closed', repo: 'github.com/meridian-travel/booking', branch: 'fix/a11y-booking-forms-labels-and-focus', age: 6 * 24 * 60, ran: 77, lastCall: 0, left: 0, machine: SMALL, agent: claude }, 'agent', 1],
  ];
  const ended: MockEnv[] = closed.map(([seed, reason, pushes]) => {
    const summary = summaryOf(now, seed);
    const end = Date.parse(summary.closed_at!);
    return {
      jobs: [],
      detail: {
        ...summary,
        last_call_at: seed.state === 'failed' ? null : new Date(end - (reason === 'idle' ? 60 : 1) * MINUTE).toISOString(),
        ports: [],
        git: seed.state === 'failed' ? null : { branch: seed.branch, ahead: 0, dirty: 0, pushed: Array.from({ length: pushes }, (_, index) => ({ branch: seed.branch!, commit: `${seed.id}${index}c4e1b9a07d3f62e85c1b4a9d0e7f3a26b8c`.slice(0, 40), at: new Date(end - (index * 9 + 2) * MINUTE).toISOString() })) },
        calls:
          seed.state === 'failed'
            ? [call(end, 0, 'environment_open', `${seed.title}: the machine did not boot`, false)]
            : [
                call(end, 30, 'environment_close', reason === 'agent' ? 'closed, the work is pushed' : 'closed'),
                call(end, 120, 'lane_exec', `git push origin ${seed.branch}`),
                call(end, 600, 'ui_check', 'http://localhost:3000 at 360 and 1280: clean'),
              ],
        commands: seed.state === 'failed' ? [] : [command(end, `exec-${seed.id}`, `git push origin ${seed.branch}`, 'passed', 2, 0)],
        viewer: null,
        closed_reason: reason,
        unsaved: null,
      },
    };
  });
  return [...open, ...ended];
}

/** An environment's browser, as a lane on the node: no account, no proxy of its own, nothing kept. */
function browserLane(sibling: Lane | undefined, env: EnvDetail, now: number): Lane {
  const name = `${env.id}-browser`;
  return {
    ...(sibling as Lane),
    id: `env/${name}`,
    workspace: 'env',
    name,
    kind: 'browser',
    engine: 'chromium',
    preset: 'environment',
    state: env.state === 'starting' ? 'warming' : 'warm',
    signal: 'ok',
    proxy: null,
    exit_ip: null,
    last_used: env.last_call_at ?? new Date(now).toISOString(),
    node: sibling?.node ?? 'hetzner-1',
    quarantine_reason: null,
    logged_in_hint: 'unknown',
    viewer: env.state === 'starting' ? null : { url: `/api/orgs/northwind-group/lanes/env/${name}/vnc` },
    control: { ...idleControl, state: 'agent' },
    hold: null,
    hold_until: null,
    signal_detail: null,
    account: null,
    readiness: 'ready',
    readiness_note: null,
    login_url: null,
  };
}

/** Seeds the environments of northwind-group and puts each open one's browser among its lanes. */
export function withEnvironments(db: MockDb, now = Date.now()) {
  const data = db.orgs.get('northwind-group');
  if (!data || scenario.has('no-envs')) return;
  data.environments = seedEnvironments(now);
  const sibling = data.lanes.find((lane) => lane.kind === 'browser');
  for (const item of data.environments) {
    if (item.detail.state === 'closed' || item.detail.state === 'failed') continue;
    data.lanes.push(browserLane(sibling, item.detail, now));
  }
}

function strip(detail: EnvDetail): EnvSummary {
  const { ports: _ports, git: _git, calls: _calls, commands: _commands, viewer: _viewer, closed_reason: _reason, unsaved: _unsaved, ...summary } = detail;
  return summary;
}

/** What time has done since the last read: a boot ends, a close ends, the cost goes up, a quiet one goes idle. */
function advance(record: OrgData, item: MockEnv, now: number) {
  const env = item.detail;
  if (env.state === 'starting' && item.until !== undefined && now >= item.until) {
    item.until = undefined;
    env.state = 'ready';
    env.viewer = { path: `/api/orgs/${record.org.slug}/lanes/env/${env.id}-browser/vnc` };
    env.calls = [{ at: new Date(now).toISOString(), tool: 'environment', summary: 'ready: 4 CPU, 8 GB in hel1, /work/shop cloned', ok: true }, ...env.calls];
    const lane = record.lanes.find((entry) => entry.id === env.lanes.browser);
    if (lane) Object.assign(lane, { state: 'warm', viewer: { url: `/api/orgs/${record.org.slug}/lanes/${env.lanes.browser}/vnc` } });
  }
  if (env.state === 'closing' && item.until !== undefined && now >= item.until) {
    item.until = undefined;
    env.state = 'closed';
    env.closed_at = new Date(now).toISOString();
    env.viewer = null;
    env.ports = [];
    record.lanes = record.lanes.filter((entry) => entry.id !== env.lanes.browser);
  }
  if (env.state !== 'closed' && env.state !== 'failed' && !scenario.has('still')) {
    const minutes = (now - Date.parse(env.created_at)) / MINUTE;
    env.cost_eur = Math.round((env.price_hourly_eur * minutes) / 60 * 10_000) / 10_000;
  }
}

/** The org's environments, newest first. */
export function mockEnvironments(record: OrgData): MockEnv[] {
  record.environments ??= [];
  return record.environments;
}

export function environmentHandlers({ org, failure, data }: Pick<Helpers, 'org' | 'failure'> & { data: () => MockDb }) {
  const missing = () => failure(404, 'not_found', 'No environment with that id on this computer.');
  const find = (record: OrgData, id: unknown) => mockEnvironments(record).find((item) => item.detail.id === id);
  const envJob = (orgSlug: unknown, id: unknown) => {
    const record = typeof orgSlug === 'string' ? data().orgs.get(orgSlug) : undefined;
    for (const item of record ? mockEnvironments(record) : []) {
      const found = item.jobs.find((entry) => entry.job.id === id);
      if (found) return found;
    }
    return undefined;
  };

  return [
    http.get(
      '/api/orgs/:org/environments',
      org((record, { request }) => {
        const state = new URL(request.url).searchParams.get('state');
        const now = Date.now();
        const items = mockEnvironments(record);
        items.forEach((item) => advance(record, item, now));
        const environments = items
          .map((item) => strip(item.detail))
          .filter((env) => state !== 'open' || (env.state !== 'closed' && env.state !== 'failed'))
          .sort((a, b) => b.created_at.localeCompare(a.created_at));
        return HttpResponse.json({ environments });
      }),
    ),
    http.get(
      '/api/orgs/:org/environments/:id',
      org((record, { params }) => {
        const item = find(record, params.id);
        if (!item) return missing();
        advance(record, item, Date.now());
        return HttpResponse.json(item.detail);
      }),
    ),
    http.post(
      '/api/orgs/:org/environments/:id/close',
      org(async (record, { params, request }) => {
        if (!OPERATE.has(record.role)) return failure(403, 'denied', 'Your role does not let you close environments here.');
        const item = find(record, params.id);
        if (!item) return missing();
        const env = item.detail;
        if (env.state === 'closed' || env.state === 'failed' || env.state === 'closing') return HttpResponse.json(strip(env));
        const body = (await request.clone().json().catch(() => ({}))) as { discard?: boolean };
        if (!body.discard && env.unsaved && (env.unsaved.dirty_files.length || env.unsaved.unpushed.length)) {
          // as the node answers (agentos.api.web.environments): the unsaved work inside the error
          return HttpResponse.json(
            {
              error: {
                code: 'unsaved_work',
                message: 'This environment has work that is not pushed. Commit and push it, or close with discard.',
                unsaved: { dirty_files: env.unsaved.dirty_files, unpushed: env.unsaved.unpushed },
              },
            },
            { status: 409 },
          );
        }
        const now = Date.now();
        env.state = 'closing';
        env.closed_reason = 'owner';
        env.unsaved = null;
        item.until = now + CLOSE_MS;
        return HttpResponse.json(strip(env));
      }),
    ),
    http.post(
      '/api/orgs/:org/environments/:id/extend',
      org(async (record, { params, request }) => {
        if (!OPERATE.has(record.role)) return failure(403, 'denied', 'Your role does not let you extend environments here.');
        const item = find(record, params.id);
        if (!item) return missing();
        const env = item.detail;
        if (env.state === 'closed' || env.state === 'failed' || env.state === 'closing') {
          return failure(409, 'conflict', 'This environment is closed, so it cannot be extended.');
        }
        const body = (await request.clone().json().catch(() => ({}))) as { minutes?: number };
        const minutes = Math.max(1, Math.min(Number(body.minutes) || 60, 8 * 60));
        env.expires_at = new Date(Math.max(Date.parse(env.expires_at), Date.now()) + minutes * MINUTE).toISOString();
        return HttpResponse.json(strip(env));
      }),
    ),
    // The runs an environment's terminal started. Any other run falls through to the Runs mock (mocks/jobs.ts).
    http.get('/api/orgs/:org/jobs/:id', ({ params, request }) => {
      const found = envJob(params.org, params.id);
      if (!found) return undefined;
      const bytes = encoder.encode(found.stdout);
      const raw = new URL(request.url).searchParams.get('stdout_offset');
      const offset = raw && /^\d+$/.test(raw) ? Math.min(Number(raw), bytes.length) : 0;
      const detail: JobDetail = {
        ...found.job,
        output: {
          stdout: decoder.decode(bytes.slice(offset)),
          stderr: '',
          stdout_offset: offset,
          stderr_offset: 0,
          next_stdout_offset: bytes.length,
          next_stderr_offset: 0,
          more: false,
        },
      };
      return HttpResponse.json(detail);
    }),
  ];
}
