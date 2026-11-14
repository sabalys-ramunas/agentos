/**
 * The address of Runs and of one run. The runs builder owns this file: add or change a param here,
 * and the router (app/router.tsx) picks it up with no edit there.
 */
import { clean, count, flag, oneOf, text } from '@/app/search';
import type { Search } from '@/app/search';

/**
 * The states a person filters by. `stopped` gathers the runs that ended early: cancelled, timed out,
 * or lost when the machine stopped. The node has no queue, so nothing is ever "waiting".
 */
export const RUN_STATES = ['running', 'passed', 'failed', 'stopped'] as const;
/** The kind of work, read from the command (`kindOf` in kind.ts). `command` when it cannot tell. */
export const RUN_KINDS = ['test', 'build', 'server', 'command'] as const;
export const RUN_SORTS = ['started', 'duration', 'state', 'failed'] as const;

export type RunStateFilter = (typeof RUN_STATES)[number];
export type RunKind = (typeof RUN_KINDS)[number];
export type RunSort = (typeof RUN_SORTS)[number];

export interface RunsSearch {
  /** Only what is in this state. */
  state?: RunStateFilter;
  /** Only this kind of work. */
  kind?: RunKind;
  /** Only on this machine (its id). */
  machine?: string;
  /** Only in this lane (`workspace/name`). */
  lane?: string;
  /** Only what this agent or person started, as the node names them. */
  by?: string;
  /** Free text: a command, a branch, an id. */
  q?: string;
  /** How far back: `1h`, `24h`, `7d`, `30d`. */
  range?: '1h' | '24h' | '7d' | '30d';
  sort?: RunSort;
  dir?: 'asc' | 'desc';
}

export function runsSearch(search: Search): RunsSearch {
  return clean({
    state: oneOf(search.state, RUN_STATES),
    kind: oneOf(search.kind, RUN_KINDS),
    machine: text(search.machine),
    lane: text(search.lane),
    by: text(search.by),
    q: text(search.q),
    range: oneOf(search.range, ['1h', '24h', '7d', '30d'] as const),
    sort: oneOf(search.sort, RUN_SORTS),
    dir: oneOf(search.dir, ['asc', 'desc'] as const),
  });
}

/** Where to land on a run's page: `tests` scrolls to what failed, `next` to what happened. */
export const RUN_TABS = ['output', 'tests', 'next', 'details'] as const;

export interface RunSearch {
  tab?: (typeof RUN_TABS)[number];
  /** Which stream of the output. Unset shows both, the errors after the output. */
  stream?: 'stdout' | 'stderr' | 'both';
  /** Keep the output scrolled to the newest line. Unset means yes while the run is going. */
  follow?: boolean;
  /** Wrap long lines. */
  wrap?: boolean;
  /** Show only output lines with this text. */
  q?: string;
  /** A line of the output to scroll to and mark (1-based, in the stream shown first), for a link to it. */
  line?: number;
  /** The failure picked (its index, from 0): the output scrolls to its line. */
  failure?: number;
  /** A test name picked: the failure with this name. */
  test?: string;
}

export function runSearch(search: Search): RunSearch {
  return clean({
    tab: oneOf(search.tab, RUN_TABS),
    stream: oneOf(search.stream, ['stdout', 'stderr', 'both'] as const),
    follow: flag(search.follow),
    wrap: flag(search.wrap),
    q: text(search.q),
    line: count(search.line),
    failure: count(search.failure),
    test: text(search.test, 400),
  });
}
