/**
 * Home's sentence, from what the node says and nothing else (docs/design/app-design.md, Home): how many
 * jobs run on how many machines, what failed and whether it runs again, what waits for you and for how
 * long. Pure, so every combination is tested (sentence.test.ts).
 *
 * The rules that keep it honest:
 * - A number is said only when it is known: no machines are named when no job says where it runs, and
 *   "nothing has failed" is said only when the runs could be read.
 * - "Is running again" is said only when a later run of the same command in the same lane is going. Who
 *   started it is named only when the node kept it. Nobody is ever said to be "fixing" anything.
 * - "Nothing needs you" is said only once every list behind the count has answered.
 */
import { plural } from '@/lib/format';

/** A run that failed today and has not passed since. */
export interface Failure {
  /** `tests` when the run printed a test summary, `run` for any other command. */
  what: 'tests' | 'run';
  /** A later run of the same command in the same lane is going now. */
  again: boolean;
  /** Who started that later run, when the node kept it. */
  againBy: string | null;
}

/** Something wrong that is not a failed run: a page with a blocker, a machine not answering, a stuck account. */
export interface Trouble {
  /** Its own short sentence, with no full stop: "The shop's product page has a blocker". */
  text: string;
  /** Where its link goes. */
  to: 'check' | 'machine' | 'account';
  id: string;
}

export interface SentenceInput {
  /** Jobs going now, of every kind. */
  running: number;
  /** The machines they run on, by name. Empty when no running job says where it runs. */
  machines: string[];
  /** Whether the runs could be read at all: if not, failures are not spoken of. */
  knowsRuns: boolean;
  failures: Failure[];
  troubles: Trouble[];
  /** What needs you, the same number as the rail; null while the lists are still answering. */
  waiting: number | null;
  /** When the oldest of them arrived. */
  oldestAt: string | null;
  now: number;
}

export type LinePart =
  | { kind: 'trouble'; text: string; to: Trouble['to']; id: string }
  | { kind: 'more'; text: string }
  | { kind: 'needs'; text: string; rest: string }
  | { kind: 'plain'; text: string };

export interface Sentence {
  /** The display line. */
  lead: string;
  /** The line under it, in parts, so the links can be links. Each part is its own sentence. */
  line: LinePart[];
}

/** "a moment", "12 min", "2 h", "3 days": how long something has waited. */
export function waitedFor(iso: string, now: number): string {
  const minutes = Math.floor((now - Date.parse(iso)) / 60_000);
  if (!Number.isFinite(minutes) || minutes < 1) return 'a moment';
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} h`;
  return plural(Math.floor(hours / 24), 'day');
}

function runningWords(running: number, machines: string[]): string {
  if (running === 0) return 'Nothing is running right now.';
  const on = machines.length === 0 ? '' : machines.length === 1 ? ` on ${machines[0]}` : ` on ${machines.length} machines`;
  if (running === 1) return `One job is running${on}.`;
  return `${running} jobs are running${on}.`;
}

function failureWords(failures: Failure[]): string {
  const [first] = failures;
  if (!first) return '';
  if (failures.length === 1) {
    const what = first.what === 'tests' ? 'A test run' : 'A run';
    if (first.again) return `${what} failed, and ${first.againBy ? `${first.againBy} is running it again` : 'it is running again'}.`;
    return `${what} failed, and it has not run again since.`;
  }
  const allTests = failures.every((item) => item.what === 'tests');
  const what = `${failures.length} ${allTests ? 'test runs' : 'runs'} failed`;
  const again = failures.filter((item) => item.again).length;
  if (again === failures.length) return `${what}, and all of them are running again.`;
  if (again === 0) return `${what}, and none has run again since.`;
  return `${what}; ${again} ${again === 1 ? 'is' : 'are'} running again.`;
}

function needsPart(waiting: number, oldestAt: string | null, now: number): LinePart {
  const text = waiting === 1 ? 'One thing needs you' : `${waiting} things need you`;
  const rest = oldestAt ? (waiting === 1 ? `, for ${waitedFor(oldestAt, now)}` : `, the oldest for ${waitedFor(oldestAt, now)}`) : '';
  return { kind: 'needs', text, rest };
}

/** The most troubles named in the line; the rest are counted. */
const SHOWN = 2;

export function homeSentence(input: SentenceInput): Sentence {
  const { running, machines, knowsRuns, failures, troubles, waiting, oldestAt, now } = input;
  const failed = knowsRuns ? failures : [];
  const line: LinePart[] = troubles.slice(0, SHOWN).map((item) => ({ kind: 'trouble', text: item.text, to: item.to, id: item.id }));
  if (troubles.length > SHOWN) line.push({ kind: 'more', text: plural(troubles.length - SHOWN, 'more problem') });

  // Only waiting: what waits leads, and the line says nothing runs.
  if (running === 0 && failed.length === 0 && waiting && waiting > 0) {
    const needs = needsPart(waiting, oldestAt, now) as Extract<LinePart, { kind: 'needs' }>;
    return { lead: `${needs.text}${needs.rest}.`, line: [...line, { kind: 'plain', text: 'Nothing is running right now' }] };
  }

  // Everything quiet: said only when the runs and the waiting list both answered.
  if (running === 0 && failed.length === 0 && waiting === 0 && knowsRuns && troubles.length === 0) {
    return { lead: 'All quiet. Nothing is running, and nothing needs you.', line: [] };
  }

  let lead = runningWords(running, machines);
  if (failed.length) lead = `${lead} ${failureWords(failed)}`;
  else if (running > 0 && knowsRuns) lead = `${lead} Nothing has failed.`;

  if (waiting === null) return { lead, line };
  line.push(waiting > 0 ? needsPart(waiting, oldestAt, now) : { kind: 'plain', text: 'Nothing needs you' });
  return { lead, line };
}
