import type { FleetNode, Job, Vm } from '@/api/types';
import { jobKind, lastHour, machineRows, machinesSentence, sortRows, timeLeft, totals } from './model';
import type { Part } from './model';

const NOW = Date.parse('2026-11-13T12:00:00.000Z');
const ago = (minutes: number) => new Date(NOW - minutes * 60_000).toISOString();

const node = (id: string, over: Partial<FleetNode> = {}): FleetNode => ({
  id,
  state: 'up',
  capabilities: ['browser', 'terminal'],
  cpu: 20,
  mem: { used_mb: 8_192, total_mb: 16_384 },
  warm_lanes: 1,
  max_warm_lanes: 4,
  last_seen: ago(0),
  version: '0.1.0',
  degraded: false,
  timezone: 'Europe/Vilnius',
  lanes_by_kind: { browser: 3, terminal: 2 },
  lanes_active: 0,
  approvals_waiting: 1,
  ...over,
});

const job = (id: string, over: Partial<Job> = {}): Job => ({
  id,
  lane: 'harbor/run-shop',
  workspace: 'harbor',
  lane_name: 'run-shop',
  node: 'hel-1',
  command: 'pnpm vitest run',
  cwd: null,
  state: 'running',
  exit_code: null,
  signal: null,
  oom_killed: false,
  started_at: ago(5),
  finished_at: null,
  duration_s: null,
  timeout_s: 3_600,
  started_by: 'Claude Code',
  stdout_bytes: 0,
  stderr_bytes: 0,
  dropped_bytes: 0,
  sync: null,
  note: null,
  ...over,
});

const vm: Vm = {
  id: 'vm_7f3a9c21',
  lane: 'harbor/ci-box',
  state: 'ready',
  profile: 'small',
  server_type: 'cpx21',
  location: 'hel1',
  created_at: ago(20),
  ready_at: ago(19),
  expires_at: new Date(NOW + 40 * 60_000).toISOString(),
  minutes_left: 40,
  price_hourly_eur: 0.0119,
  cost_eur_so_far: 0.01,
  purpose: 'Run the shop tests on a clean machine',
  started_by: 'An agent',
};

const words = (parts: Part[]) => parts.map((part) => (typeof part === 'string' ? part : 'name' in part ? part.name : part.warn)).join('');

describe('the machines table, read from what the node serves', () => {
  it('makes a row per node and per VM that is up, with no load invented for a VM', () => {
    const rows = machineRows([node('hel-1'), node('office')], [vm, { ...vm, id: 'vm_old', state: 'deleted' }], []);
    expect(rows.map((row) => row.id)).toEqual(['hel-1', 'office', 'vm_7f3a9c21']);
    const row = rows[2]!;
    expect(row.type).toBe('vm');
    expect([row.cpu, row.memory, row.browsers, row.terminals]).toEqual([null, null, null, null]);
    expect(rows[0]!).toMatchObject({ cpu: 20, memory: 50, browsers: 3, terminals: 2, waiting: 1, where: 'Vilnius time · 16 GB memory' });
  });

  it('says busy, struggling and not answering as a shape and a word, and shows no load for a machine away', () => {
    const [busy, struggling, away] = machineRows([node('a', { cpu: 91 }), node('b', { state: 'degraded' }), node('c', { state: 'down' })], [], []);
    expect(busy!.status).toEqual({ label: 'Busy', tone: 'warn' });
    expect(struggling!.status).toEqual({ label: 'Struggling', tone: 'warn' });
    expect(away!.status).toEqual({ label: 'Not answering', tone: 'bad' });
    expect(away!.cpu).toBeNull();
  });

  it('puts a run on the machine it runs on, and a VM lane run on the VM', () => {
    const rows = machineRows([node('hel-1')], [vm], [job('a'), job('b', { lane: 'harbor/ci-box' }), job('c', { state: 'passed', finished_at: ago(1) })]);
    expect(rows[0]!.now.jobs.map((item) => item.id)).toEqual(['a']);
    expect(rows[1]!.now.jobs.map((item) => item.id)).toEqual(['b']);
  });

  it('sorts by a column in its natural direction, and the other way when asked', () => {
    const rows = machineRows([node('b', { cpu: 10 }), node('a', { cpu: 70 }), node('c', { state: 'down' })], [], []);
    expect(sortRows(rows, 'load').map((row) => row.id)).toEqual(['a', 'b', 'c']);
    expect(sortRows(rows, 'load', 'asc').map((row) => row.id)).toEqual(['b', 'a', 'c']);
    expect(sortRows(rows, 'name').map((row) => row.id)).toEqual(['a', 'b', 'c']);
    expect(sortRows(rows, 'state').map((row) => row.id)[0]).toBe('c');
    expect(sortRows(rows, undefined)).toBe(rows);
  });
});

describe('the sentence on top', () => {
  it('names how many answer, the busiest, what is not answering and a VM with its time left', () => {
    const rows = machineRows([node('hel-1', { cpu: 88 }), node('office', { cpu: 30 }), node('shop', { state: 'down', last_seen: ago(40) })], [vm], []);
    expect(words(machinesSentence(rows, NOW))).toBe(
      '2 online, 1 not answering. hel-1 is the busiest at 88%. shop has not answered since 40 min ago. harbor/ci-box is an environment with 40 min left.',
    );
  });

  it('makes one machine read as useful, with what is running on it', () => {
    expect(words(machinesSentence(machineRows([node('home-mini')], [], [job('a', { node: 'home-mini' })]), NOW))).toBe(
      'home-mini is your one machine at 20% CPU, running pnpm vitest run.',
    );
    expect(words(machinesSentence(machineRows([node('home-mini')], [], []), NOW))).toBe('home-mini is your one machine at 20% CPU, and nothing is running on it now.');
  });

  it('counts the totals under the table', () => {
    expect(totals(machineRows([node('a'), node('b', { state: 'down' })], [vm], [job('x', { node: 'a' })]))).toBe('3 machines · 2 online · 6 browsers · 4 terminals · 1 run going');
  });
});

describe('the last hour', () => {
  it('draws only real runs, where they started and ended in the hour', () => {
    const rows = machineRows([node('hel-1'), node('quiet')], [], []);
    const lanes = lastHour(rows, [job('now', { started_at: ago(6) }), job('done', { state: 'failed', started_at: ago(30), finished_at: ago(15) }), job('old', { state: 'passed', started_at: ago(200), finished_at: ago(190) })], NOW);
    expect(lanes[0]!.bars.map((bar) => [bar.job.id, bar.tone, Number(bar.from.toFixed(2)), Number(bar.to.toFixed(2))])).toEqual([
      ['done', 'bad', 0.5, 0.75],
      ['now', 'live', 0.9, 1],
    ]);
    expect(lanes[1]!.bars).toEqual([]);
  });
});

describe('small words', () => {
  it('reads a run as tests, a build or a dev server from its command', () => {
    expect(jobKind({ command: 'uv run pytest -q' })).toBe('test');
    expect(jobKind({ command: 'pnpm build' })).toBe('build');
    expect(jobKind({ command: 'pnpm dev --port 3000' })).toBe('server');
    expect(jobKind({ command: './scripts/backup.sh' })).toBeNull();
  });

  it('says a VM time left the way a person would', () => {
    expect(timeLeft(vm, NOW)).toBe('40 min left');
    expect(timeLeft({ ...vm, minutes_left: 125 }, NOW)).toBe('2 h 5 min left');
    expect(timeLeft({ ...vm, minutes_left: null, expires_at: new Date(NOW - 1000).toISOString() }, NOW)).toBe('deleting now');
  });
});
