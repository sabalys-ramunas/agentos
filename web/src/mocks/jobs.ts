/**
 * The mock of Runs, the commands started with `agentos run` (`/jobs`, agentos.api.web.jobs): a run in every
 * state, its output read from offsets, and cancel. The running one prints more on every read and ends after
 * a few, so a page that follows it sees it finish. A viewer reads; a cancel needs the owner, an admin or a
 * member who operates, as the node asks for `operate`.
 */
import { http, HttpResponse } from 'msw';
import type { Job, JobDetail, JobState, JobTests } from '@/api/types';
import type { Helpers } from './s3';
import type { OrgData } from './seed';

const MINUTE = 60_000;
const READ_BYTES = 65_536;
/** Reads of the running run before it ends. */
const READS_TO_FINISH = 6;

export interface MockJob {
  job: Job;
  stdout: string;
  stderr: string;
  /** For the running one: everything it will print, and how much of it is out so far. */
  plan?: { stdout: string; stderr: string; reads: number; end: { state: JobState; exit_code: number; tests?: JobTests } };
}

const ago = (now: number, minutes: number) => new Date(now - minutes * MINUTE).toISOString();

function vitestOutput(failed: number, passed: number): string {
  const lines = [
    ' RUN  v2.1.8 /home/agent/src/harbor-shop',
    '',
    ' ✓ src/cart/total.test.ts (14 tests) 41ms',
    ' ✓ src/cart/discount.test.ts (9 tests) 18ms',
    ' ✓ src/checkout/address.test.ts (22 tests) 120ms',
    ...(failed
      ? [
          ' ❯ src/checkout/payment.test.ts (12 tests | 2 failed) 340ms',
          '   × rounds the total to cents before it charges 12ms',
          '   × shows the card error next to the field 30ms',
        ]
      : [' ✓ src/checkout/payment.test.ts (12 tests) 300ms']),
    ' ✓ src/catalog/search.test.ts (61 tests) 410ms',
    '',
    ` Test Files  ${failed ? '1 failed | 4 passed' : '5 passed'} (5)`,
    `      Tests  ${failed ? `${failed} failed | ` : ''}${passed} passed (${failed + passed})`,
    '   Start at  09:12:04',
    '   Duration  4.31s',
    '',
  ];
  return lines.join('\n');
}

const PYTEST_OUT = [
  '============================= test session starts ==============================',
  'platform linux -- Python 3.12.7, pytest-8.3.3',
  'rootdir: /home/agent/src/harbor-api',
  'collected 214 items',
  '',
  'tests/test_orders.py ........................................            [ 18%]',
  'tests/test_stock.py ..........................................s......     [ 41%]',
  'tests/test_shipping.py ...................................................[ 65%]',
  'tests/test_invoices.py ..........................................ss..... [100%]',
  '',
  '======================= 211 passed, 3 skipped in 48.20s ========================',
  '',
].join('\n');

function base(now: number, over: Partial<Job> & Pick<Job, 'id' | 'lane' | 'command' | 'state'>): Job {
  const [workspace = '', lane_name = ''] = over.lane.split('/');
  return {
    workspace,
    lane_name,
    node: 'hetzner-1',
    cwd: null,
    exit_code: null,
    signal: null,
    oom_killed: false,
    started_at: ago(now, 5),
    finished_at: null,
    duration_s: null,
    timeout_s: 3_600,
    started_by: 'claude-code',
    stdout_bytes: 0,
    stderr_bytes: 0,
    dropped_bytes: 0,
    sync: { dest: 'src/harbor-shop', files: 1_284, changed: 12, deleted: 1, bytes: 18_400_000 },
    note: null,
    ...over,
  };
}

function finished(now: number, job: Job, startedMinutesAgo: number, durationS: number): Job {
  return {
    ...job,
    started_at: ago(now, startedMinutesAgo),
    finished_at: new Date(now - startedMinutesAgo * MINUTE + durationS * 1_000).toISOString(),
    duration_s: durationS,
  };
}

const sized = (item: MockJob): MockJob => ({
  ...item,
  job: { ...item.job, stdout_bytes: new Blob([item.stdout]).size, stderr_bytes: new Blob([item.stderr]).size },
});

/** One run in each state, in the northwind-group org: a web shop's tests, an API's tests, a build, a deploy. */
export function seedJobs(now = Date.now()): MockJob[] {
  const commit = '7c1e9a4b2d8f3e6a0b5c9d1f4e7a2b8c6d0e3f5a';
  const running: MockJob = {
    job: base(now, {
      id: 'job-1a2b3c4d5e',
      lane: 'harbor/run-shop',
      command: 'pnpm vitest run',
      cwd: 'src/harbor-shop',
      state: 'running',
      started_at: ago(now, 1),
      source: { branch: 'feature/cart-rounding', commit, dirty: true, told_by: 'client' },
    }),
    stdout: '',
    stderr: '',
    plan: {
      stdout: vitestOutput(0, 118),
      stderr: '',
      reads: 0,
      end: { state: 'passed', exit_code: 0, tests: { runner: 'vitest', passed: 118, failed: 0, skipped: 0, total: 118, source: 'output' } },
    },
  };
  const jobs: MockJob[] = [
    running,
    {
      job: finished(now, base(now, {
        id: 'job-2b3c4d5e6f', lane: 'harbor/run-api', command: 'uv run pytest -q', cwd: 'src/harbor-api', state: 'passed',
        exit_code: 0, started_by: 'The owner',
        source: { branch: 'main', commit: 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4', dirty: false, told_by: 'client' },
        tests: { runner: 'pytest', passed: 211, failed: 0, skipped: 3, total: 214, source: 'output' },
      }), 42, 51.4),
      stdout: PYTEST_OUT,
      stderr: '',
    },
    {
      job: finished(now, base(now, {
        id: 'job-3c4d5e6f7a', lane: 'harbor/run-shop', command: 'pnpm vitest run', cwd: 'src/harbor-shop', state: 'failed',
        exit_code: 1,
        source: { branch: 'feature/cart-rounding', commit: '9f86d081884c7d659a2feaa0c55ad015a3bf4f1b', dirty: false, told_by: 'client' },
        tests: { runner: 'vitest', passed: 116, failed: 2, skipped: 0, total: 118, source: 'output' },
      }), 95, 6.2),
      stdout: vitestOutput(2, 116),
      stderr: 'AssertionError: expected 41.999999 to be 42\n    at src/checkout/payment.test.ts:88:31\n',
    },
    {
      job: finished(now, base(now, {
        id: 'job-4d5e6f7a8b', lane: 'harbor/run-shop', command: 'pnpm build', cwd: 'src/harbor-shop', state: 'cancelled',
        started_by: 'The owner', note: 'cancelled by the owner',
      }), 180, 74),
      stdout: 'vite v6.0.3 building for production...\ntransforming (812) src/catalog/grid.tsx\n',
      stderr: '',
    },
    {
      job: finished(now, base(now, {
        id: 'job-5e6f7a8b9c', lane: 'meridian/run-site', command: 'npm run e2e', cwd: 'src/meridian-site', state: 'timed_out',
        node: 'hetzner-1', timeout_s: 1_800, note: 'stopped after 1800 s, its time limit', signal: 'SIGKILL',
        sync: { dest: 'src/meridian-site', files: 642, changed: 3, deleted: 0, bytes: 9_200_000 },
      }), 300, 1_800),
      stdout: 'Running 48 tests using 4 workers\n  ✓ 1 [chromium] › booking.spec.ts:12 › finds a flight (8.1s)\n',
      stderr: 'Timed out waiting for http://localhost:4173 to answer\n',
    },
    {
      job: finished(now, base(now, {
        id: 'job-6f7a8b9c0d', lane: 'harbor/run-api', command: 'uv run pytest tests/test_invoices.py', cwd: 'src/harbor-api', state: 'lost',
        note: 'the node stopped while it ran', source: { branch: 'main', commit: 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4', dirty: false, told_by: 'client' },
      }), 1_500, 12),
      stdout: 'collected 46 items\n\ntests/test_invoices.py ..........',
      stderr: '',
    },
    {
      // From an older agentos: no branch or commit was sent, and the command prints no test summary.
      job: finished(now, base(now, {
        id: 'job-7a8b9c0d1e', lane: 'personal/shell', command: './scripts/backup-photos.sh --dry-run', state: 'passed',
        node: 'home-mini', exit_code: 0, started_by: 'The owner', sync: null,
      }), 2_900, 3.1),
      stdout: 'Would copy 1,204 files (3.2 GB) to /backups/photos\nNothing was copied: dry run.\n',
      stderr: '',
    },
  ];
  return jobs.map(sized);
}

/**
 * Holds the org's running runs, so a test decides when (and whether) they end: they keep printing up to
 * their last part and stay running. The flag lives on the org's record, so the db reset before each test
 * clears it with everything else.
 */
export function holdRunningJobs(record: OrgData, hold = true) {
  record.jobsHeld = hold;
}

/** Makes the running run print its next part, and end once it has printed it all (unless the org holds its runs). */
function advance(item: MockJob, now: number, held = false) {
  const plan = item.plan;
  if (!plan || item.job.state !== 'running') return;
  plan.reads += 1;
  const share = Math.min(held ? (READS_TO_FINISH - 1) / READS_TO_FINISH : 1, plan.reads / READS_TO_FINISH);
  item.stdout = plan.stdout.slice(0, Math.floor(plan.stdout.length * share));
  item.stderr = plan.stderr.slice(0, Math.floor(plan.stderr.length * share));
  if (share >= 1) {
    const started = Date.parse(item.job.started_at);
    item.job = {
      ...item.job,
      state: plan.end.state,
      exit_code: plan.end.exit_code,
      finished_at: new Date(now).toISOString(),
      duration_s: Math.round((now - started) / 100) / 10,
      ...(plan.end.tests ? { tests: plan.end.tests } : {}),
    };
    delete item.plan;
  }
  Object.assign(item, sized(item));
}

const encoder = new TextEncoder();
const decoder = new TextDecoder();

function slice(text: string, offset: number): { text: string; next: number; more: boolean } {
  const bytes = encoder.encode(text);
  const part = bytes.slice(offset, offset + READ_BYTES);
  return { text: decoder.decode(part), next: offset + part.length, more: offset + part.length < bytes.length };
}

export function detailOf(item: MockJob, stdoutOffset: number, stderrOffset: number): JobDetail {
  const out = slice(item.stdout, stdoutOffset);
  const err = slice(item.stderr, stderrOffset);
  return {
    ...item.job,
    output: {
      stdout: out.text,
      stderr: err.text,
      stdout_offset: stdoutOffset,
      stderr_offset: stderrOffset,
      next_stdout_offset: out.next,
      next_stderr_offset: err.next,
      more: out.more || err.more,
    },
  };
}

const OPERATE = new Set(['owner', 'admin', 'member']);

/** The org's runs, seeded on first use: northwind-group has one in each state, any other org none. */
export function mockJobs(record: OrgData): MockJob[] {
  record.jobs ??= record.org.slug === 'northwind-group' ? seedJobs() : [];
  return record.jobs;
}

export function jobHandlers({ org, failure }: Pick<Helpers, 'org' | 'failure'>) {
  const jobsOf = mockJobs;
  const notFound = () => failure(404, 'not_found', 'No such run on this computer.');
  const int = (raw: string | null) => (raw && /^\d+$/.test(raw) ? Number(raw) : 0);

  return [
    http.get(
      '/api/orgs/:org/jobs',
      org((record, { request }) => {
        const query = new URL(request.url).searchParams;
        const lane = query.get('lane');
        const state = query.get('state');
        const limit = Math.min(int(query.get('limit')) || 50, 200);
        const items = jobsOf(record);
        items.forEach((item) => advance(item, Date.now(), record.jobsHeld));
        const jobs = items
          .map((item) => item.job)
          .filter((job) => (!lane || job.lane === lane) && (!state || job.state === state))
          .sort((a, b) => b.started_at.localeCompare(a.started_at))
          .slice(0, limit);
        return HttpResponse.json({ available: true, jobs });
      }),
    ),
    http.get(
      '/api/orgs/:org/jobs/:id',
      org((record, { params, request }) => {
        const item = jobsOf(record).find((entry) => entry.job.id === params.id);
        if (!item) return notFound();
        advance(item, Date.now(), record.jobsHeld);
        const query = new URL(request.url).searchParams;
        return HttpResponse.json(detailOf(item, int(query.get('stdout_offset')), int(query.get('stderr_offset'))));
      }),
    ),
    http.post(
      '/api/orgs/:org/jobs/:id/cancel',
      org((record, { params }) => {
        if (!OPERATE.has(record.role)) return failure(403, 'denied', 'Your role does not let you stop runs here.');
        const item = jobsOf(record).find((entry) => entry.job.id === params.id);
        if (!item) return notFound();
        if (item.job.state === 'running') {
          const now = Date.now();
          item.job = {
            ...item.job,
            state: 'cancelled',
            finished_at: new Date(now).toISOString(),
            duration_s: Math.round((now - Date.parse(item.job.started_at)) / 100) / 10,
            note: 'cancelled from the panel',
          };
          delete item.plan;
        }
        return HttpResponse.json(item.job);
      }),
    ),
  ];
}
