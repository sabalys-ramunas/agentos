/**
 * What runs now, for Home's sentence and its slider (Mono): `openajar run` jobs, design checks that just
 * ended, routine and agent runs, and accounts an agent is acting in. Pure, so the rules are tested
 * (running.test.ts): what counts as running, which failures still stand, and which run again.
 */
import type { HistoryStory, Job, Lane, StudioRunRow, Workspace } from '@/api/types';
import type { Kind, NeutralKind } from '@/components/ui/kind';
import type { Failure } from './sentence';
import { accountCondition } from './words';

/** A design check is shown on Home this long after it ends. Studio keeps no "running" state. */
export const CHECK_FRESH_MS = 30 * 60_000;

export const TEST_COMMAND = /\b(test|tests|vitest|pytest|jest|mocha|playwright|cypress|go test|swift test|cargo test|rspec|phpunit)\b/i;
const BUILD_COMMAND = /\b(build|install|ci|compile|tsc|lint|typecheck|migrate|bundle|make)\b/i;
const SERVER_COMMAND = /\b(dev|serve|server|start|preview|watch)\b/i;

/** The kind of work a run is, read from what it is: tests when it printed a test summary or says test. */
export function jobKind(job: Pick<Job, 'command' | 'tests'>): Kind | 'lane' {
  if (job.tests || TEST_COMMAND.test(job.command)) return 'test';
  if (SERVER_COMMAND.test(job.command) && !BUILD_COMMAND.test(job.command)) return 'server';
  if (BUILD_COMMAND.test(job.command)) return 'build';
  return 'lane';
}

export const ENDED_BADLY: ReadonlySet<Job['state']> = new Set(['failed', 'timed_out', 'lost']);

export type RunningItem =
  | { type: 'job'; id: string; kind: Kind | 'lane'; at: string; failing: boolean; machine: string; job: Job }
  | { type: 'check'; id: string; kind: 'design'; at: string; failing: boolean; machine: null; run: StudioRunRow }
  | { type: 'story'; id: string; kind: NeutralKind; at: string; failing: false; machine: string | null; story: HistoryStory }
  | { type: 'lane'; id: string; kind: NeutralKind; at: string; failing: false; machine: string; lane: Lane; workspace: Workspace };

export interface RunningInput {
  jobs: Job[];
  checks: StudioRunRow[];
  stories: HistoryStory[];
  workspaces: Workspace[];
  /** One workspace, or every one. */
  scope: string;
  now: number;
}

const inScope = (scope: string, workspace: string | null | undefined) => !scope || workspace === scope;

/** Every running thing, the ones failing first, then the newest. */
export function runningItems({ jobs, checks, stories, workspaces, scope, now }: RunningInput): RunningItem[] {
  const lanes = new Map(workspaces.flatMap((workspace) => workspace.lanes.map((lane) => [lane.id, { lane, workspace }] as const)));
  const items: RunningItem[] = [];
  for (const job of jobs) {
    if (job.state !== 'running' || !inScope(scope, job.workspace)) continue;
    items.push({ type: 'job', id: job.id, kind: jobKind(job), at: job.started_at, failing: (job.tests?.failed ?? 0) > 0, machine: job.node, job });
  }
  for (const run of checks) {
    if (now - Date.parse(run.created_at) > CHECK_FRESH_MS) continue;
    // A check with no profile is the owner's, and belongs to no workspace.
    if (scope && !(run.profile ?? '').startsWith(`${scope}/`)) continue;
    items.push({ type: 'check', id: run.id, kind: 'design', at: run.created_at, failing: run.counts.blocker > 0, machine: null, run });
  }
  const storyLanes = new Set<string>();
  for (const story of stories) {
    if (!inScope(scope, story.workspace)) continue;
    storyLanes.add(story.lane);
    const kind: NeutralKind = story.why.trigger === 'schedule' || story.technical.flow ? 'routine' : 'account';
    items.push({ type: 'story', id: story.id, kind, at: story.started_at, failing: false, machine: lanes.get(story.lane)?.lane.node ?? null, story });
  }
  for (const { lane, workspace } of lanes.values()) {
    if (!inScope(scope, workspace.id) || storyLanes.has(lane.id)) continue;
    if (accountCondition(lane).state !== 'working') continue;
    items.push({ type: 'lane', id: lane.id, kind: 'account', at: lane.last_used ?? new Date(now).toISOString(), failing: false, machine: lane.node, lane, workspace });
  }
  // Failing first; then runs and checks, which have a picture, before routines and accounts.
  const order = { job: 0, check: 1, story: 2, lane: 3 } as const;
  return items.sort((a, b) => Number(b.failing) - Number(a.failing) || order[a.type] - order[b.type] || b.at.localeCompare(a.at));
}

/** What is going now: checks only show that they just ended, so they are not counted. */
export const isGoing = (item: RunningItem) => item.type !== 'check';

/** The machines the going things run on, by name; empty unless every one of them says where. */
export function machinesOf(items: RunningItem[]): string[] {
  const going = items.filter(isGoing);
  if (going.some((item) => !item.machine)) return [];
  return [...new Set(going.map((item) => item.machine as string))].sort();
}

const sameWork = (a: Job, b: Job) => a.lane === b.lane && a.command === b.command;

/**
 * Runs that failed since `since` and have not passed since: each with whether the same command runs again
 * in the same lane now, and who started that. A failure later run again and passed is over.
 */
export function standingFailures(jobs: Job[], scope: string, since: number): { job: Job; failure: Failure }[] {
  const mine = jobs.filter((job) => inScope(scope, job.workspace));
  const out: { job: Job; failure: Failure }[] = [];
  const seen = new Set<string>();
  for (const job of [...mine].sort((a, b) => b.started_at.localeCompare(a.started_at))) {
    if (!ENDED_BADLY.has(job.state) || Date.parse(job.started_at) < since) continue;
    const key = `${job.lane}\u0000${job.command}`;
    // The newest failure of a command speaks for it.
    if (seen.has(key)) continue;
    seen.add(key);
    const later = mine.filter((other) => other.id !== job.id && sameWork(other, job) && other.started_at > job.started_at);
    if (later.some((other) => other.state === 'passed')) continue;
    const again = later.find((other) => other.state === 'running') ?? null;
    out.push({
      job,
      failure: { what: job.tests || TEST_COMMAND.test(job.command) ? 'tests' : 'run', again: Boolean(again), againBy: again?.started_by ?? null },
    });
  }
  return out;
}

/** The newest check of each page that has blockers, unless a newer check of that page is clean of them. */
export function blockedChecks(checks: StudioRunRow[], scope: string, since: number): StudioRunRow[] {
  const newest = new Map<string, StudioRunRow>();
  for (const run of checks) {
    if (run.kind !== 'check' || Date.parse(run.created_at) < since) continue;
    if (scope && !(run.profile ?? '').startsWith(`${scope}/`)) continue;
    const held = newest.get(run.url);
    if (!held || run.created_at > held.created_at) newest.set(run.url, run);
  }
  return [...newest.values()].filter((run) => run.counts.blocker > 0);
}

/** "shop.harborsupply.example/products": a page's address without the scheme. */
export function pageName(url: string): string {
  try {
    const parsed = new URL(url);
    const path = parsed.pathname === '/' ? '' : parsed.pathname;
    return `${parsed.host}${path}`;
  } catch {
    return url;
  }
}
