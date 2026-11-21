/** What Runs says about a run, in plain words. Every word here is read from what the node serves. */
import type { Job, JobState } from '@/api/types';
import { elapsed, plural } from '@/lib/format';
import type { StatusLabel } from '@/lib/labels';
import { KIND_WORD, kindOf } from './kind';
import type { RunKind, RunStateFilter } from './search';

export const JOB_STATUS: Record<JobState, StatusLabel> = {
  running: { tone: 'live', label: 'Running', pulse: true },
  passed: { tone: 'ok', label: 'Passed' },
  failed: { tone: 'bad', label: 'Failed' },
  cancelled: { tone: 'idle', label: 'Cancelled' },
  timed_out: { tone: 'warn', label: 'Timed out' },
  lost: { tone: 'warn', label: 'Lost' },
};

/** One sentence for each way a run can end, when the node gives no note of its own. */
export const STATE_SENTENCE: Record<JobState, string> = {
  running: 'It is running now.',
  passed: 'It ended with exit code 0.',
  failed: 'It ended with an error.',
  cancelled: 'It was cancelled before it ended.',
  timed_out: 'It ran past its time limit and was stopped.',
  lost: 'The machine stopped while it ran, so how it ended is not known.',
};

export const STOPPED: JobState[] = ['cancelled', 'timed_out', 'lost'];

export function matchesState(job: Job, filter: RunStateFilter): boolean {
  return filter === 'stopped' ? STOPPED.includes(job.state) : job.state === filter;
}

export const runKind = (job: Pick<Job, 'command'>): RunKind => kindOf(job.command);

const lastPart = (path: string | null | undefined) => path?.replace(/\/+$/, '').split('/').filter(Boolean).at(-1);

/** The folder it ran in, or the lane's own name: `harbor-shop`, `shell`. */
export function placeOf(job: Pick<Job, 'cwd' | 'sync' | 'lane_name' | 'lane'>): string {
  return lastPart(job.cwd) ?? lastPart(job.sync?.dest) ?? (job.lane_name || job.lane);
}

/** A run's title: its kind and where it ran. "Tests in harbor-shop", "Command in shell". */
export function titleOf(job: Pick<Job, 'command' | 'cwd' | 'sync' | 'lane_name' | 'lane'>): string {
  return `${KIND_WORD[runKind(job)]} in ${placeOf(job)}`;
}

/** How long it took, or has been going: "51.4s", "30m 0s". */
export function durationOf(job: Pick<Job, 'duration_s' | 'started_at' | 'state'>, now = Date.now()): string | undefined {
  if (job.duration_s !== null && job.duration_s !== undefined) return elapsed(job.duration_s * 1_000);
  if (job.state === 'running') return elapsed(now - Date.parse(job.started_at));
  return undefined;
}

export function durationMs(job: Pick<Job, 'duration_s' | 'started_at' | 'state'>, now = Date.now()): number {
  if (job.duration_s !== null && job.duration_s !== undefined) return job.duration_s * 1_000;
  return job.state === 'running' ? now - Date.parse(job.started_at) : -1;
}

/** The status with what the tests said, for the run's own page: "Failed: 2 of 118 tests". */
export function statusOf(job: Job): StatusLabel {
  const base = JOB_STATUS[job.state];
  const tests = job.tests;
  if (job.state === 'failed' && tests && tests.failed > 0) return { ...base, label: `Failed: ${tests.failed} of ${tests.total} tests` };
  if (job.state === 'failed' && job.exit_code !== null) return { ...base, label: `Failed with exit code ${job.exit_code}` };
  if (job.state === 'failed' && job.signal) return { ...base, label: `Failed: stopped by ${job.signal}` };
  return base;
}

/** The command the person copies to follow it from their own terminal. */
export const followCommand = (job: Pick<Job, 'id'>) => `openajar run --follow ${job.id}`;
export const logsCommand = (job: Pick<Job, 'id'>) => `openajar run --logs ${job.id}`;

const DAY = 24 * 60 * 60 * 1_000;

/**
 * The sentence over the table: what is going and how the last day went. Only what the node serves:
 * test totals are the ones read from each run's own output.
 */
export function summaryOf(jobs: Job[], now = Date.now()): string {
  if (!jobs.length) return 'Nothing has run yet.';
  const running = jobs.filter((job) => job.state === 'running').length;
  const recent = jobs.filter((job) => job.state !== 'running' && now - Date.parse(job.finished_at ?? job.started_at) < DAY);
  const parts: string[] = [];
  parts.push(running ? `${plural(running, 'run')} going now.` : 'Nothing is running now.');
  if (!recent.length) {
    parts.push('Nothing finished in the last 24 hours.');
    return parts.join(' ');
  }
  const passed = recent.filter((job) => job.state === 'passed').length;
  const failed = recent.filter((job) => job.state === 'failed').length;
  const stopped = recent.filter((job) => STOPPED.includes(job.state)).length;
  const how = [passed ? `${passed} passed` : '', failed ? `${failed} failed` : '', stopped ? `${stopped} stopped early` : ''].filter(Boolean);
  parts.push(`In the last 24 hours ${recent.length} finished: ${joinWords(how)}.`);
  const tested = recent.filter((job) => job.tests);
  if (tested.length) {
    const testsPassed = tested.reduce((sum, job) => sum + (job.tests?.passed ?? 0), 0);
    const testsFailed = tested.reduce((sum, job) => sum + (job.tests?.failed ?? 0), 0);
    parts.push(`Their tests: ${testsPassed.toLocaleString('en')} passed and ${testsFailed.toLocaleString('en')} failed.`);
  }
  return parts.join(' ');
}

export function joinWords(items: string[]): string {
  if (items.length <= 1) return items.join('');
  return `${items.slice(0, -1).join(', ')} and ${items.at(-1)}`;
}

/** "1.2 MB", "640 KB", "12 B". */
export function bytes(value: number): string {
  if (value < 1_000) return `${value} B`;
  if (value < 1_000_000) return `${Math.round(value / 1_000)} KB`;
  if (value < 1_000_000_000) return `${(value / 1_000_000).toFixed(1)} MB`;
  return `${(value / 1_000_000_000).toFixed(1)} GB`;
}
