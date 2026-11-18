/**
 * The words of how an environment opened: each bring-up step's name, seconds as a person reads
 * them, the one sentence about where it started from, and the app's state. Pure, and tested.
 */
import type { EnvApp, EnvBringUp, EnvBringUpStep, EnvDetail, EnvPrepared } from '@/api/types';
import type { StatusLabel } from '@/lib/labels';

/** bringup.STEP_WORDS, as short names for a row. */
export const STEP_NAMES: Record<string, string> = {
  'VM create': 'Rent the machine',
  'VM ready': 'Boot',
  'setup check': 'Check its tools',
  'apt ready': 'Package manager',
  'tools top-up': 'Install tools',
  'tunnels and git': 'Connect',
  network: 'Set up the network',
  browser: 'Start the browser',
  clone: 'Clone the repository',
  restore: 'Restore saved work',
  ssh: 'Set up ssh',
  checkout: 'Check out the branch',
  refresh: 'Refresh',
  setup: 'Run setup',
  app: 'Start the app',
  resume: 'Wake the machine',
  swap: 'Turn on swap',
};

export const stepName = (step: string) => STEP_NAMES[step] ?? step.charAt(0).toUpperCase() + step.slice(1);

/** "0.6 s", "52 s", "3 min 12 s": tenths under ten seconds, whole seconds under a minute. */
export function seconds(value: number): string {
  if (!Number.isFinite(value) || value < 0) return '–';
  if (value < 10) return `${(Math.round(value * 10) / 10).toFixed(1)} s`;
  if (value < 60) return `${Math.round(value)} s`;
  const minutes = Math.floor(value / 60);
  const rest = Math.round(value - minutes * 60);
  return rest ? `${minutes} min ${rest} s` : `${minutes} min`;
}

type Opened = Pick<EnvDetail, 'state' | 'prepared' | 'resumed' | 'resumed_by' | 'bringup' | 'repo'> & { machine?: Pick<EnvDetail['machine'], 'provider'> };

/** What a resume wakes from: the record of the resume, else what the bring-up says it restores. */
export function wakesFrom(env: Pick<EnvDetail, 'resumed' | 'bringup'>): 'snapshot' | 'paused' | 'files' | null {
  return env.resumed?.from ?? env.bringup?.restore_from ?? null;
}

/** Where it started from, in a few words: "a warm, prepared machine", "its paused machine". */
export function startedFrom(env: Pick<EnvDetail, 'prepared' | 'resumed' | 'bringup'>): string | null {
  const from = wakesFrom(env);
  if (from) return from === 'paused' ? 'its paused machine' : from === 'snapshot' ? 'its snapshot' : 'its saved files';
  const prepared = env.prepared;
  if (!prepared) return null;
  if (prepared.used && prepared.warm) return 'a warm, prepared machine';
  if (prepared.used) return 'a prepared machine';
  if (prepared.warm) return 'a warm machine';
  return null;
}

/** A step as a row shows it: done, running now (its seconds so far), or still to come. */
export interface StepView extends EnvBringUpStep {
  running?: boolean;
  pending?: boolean;
}

/** The steps every bring-up of this kind goes through, so a starting one can show what is still to come. */
function usualSteps(env: Opened): string[] {
  const from = wakesFrom(env);
  const work = from ? 'restore' : env.repo ? 'clone' : null;
  return ['VM create', 'VM ready', 'tunnels and git', 'browser', ...(work ? [work] : [])];
}

/** The rows: the steps done, the ones running now, and (while it starts) the usual ones still to come. */
export function stepViews(env: Opened): StepView[] {
  const bringup = env.bringup;
  if (!bringup) return [];
  const done: StepView[] = [...bringup.steps];
  const running: StepView[] = (bringup.running ?? []).map((step) => ({ step: step.step, at_s: step.at_s, seconds: step.so_far_s, running: true }));
  const seen = new Set([...done, ...running].map((step) => step.step));
  const pending: StepView[] = env.state === 'starting' ? usualSteps(env).filter((step) => !seen.has(step)).map((step) => ({ step, at_s: 0, seconds: 0, pending: true })) : [];
  return [...done, ...running, ...pending];
}

/** Steps skipped, and the seconds they took the last time they ran (when the node knows). */
export function skipped(bringup: EnvBringUp): { steps: EnvBringUpStep[]; seconds: number } {
  const steps = bringup.steps.filter((step) => step.skipped);
  return { steps, seconds: steps.reduce((sum, step) => sum + (step.saved_s ?? step.seconds), 0) };
}

/** About what the same open takes without what it skipped: this one plus the skipped steps' last seconds. Null when nothing measured was skipped. */
export function withoutSkips(bringup: EnvBringUp): number | null {
  const saved = skipped(bringup).seconds;
  return bringup.total_s !== null && saved > 0 ? bringup.total_s + saved : null;
}

const sentence = (text: string) => {
  const trimmed = text.trim();
  if (!trimmed) return '';
  const capital = trimmed.charAt(0).toUpperCase() + trimmed.slice(1);
  return /[.!?]$/.test(capital) ? capital : `${capital}.`;
};

/** The node's own words, as a sentence: capitalised and ended. */
export const nodeSentence = sentence;

/** The small heading over it: what is happening, or that it happened. */
export function openedEyebrow(env: Opened): string {
  if (env.state !== 'starting') return env.state === 'failed' ? 'How it failed to open' : 'How it opened';
  return wakesFrom(env) ? `Waking ${startedFrom(env) ?? ''}`.trim() : 'Opening now';
}

/**
 * The lead and the line under it. "Opened in 4.1 s on a warm, prepared machine." then where the time
 * went: "Setup ran when it was built 2 days ago, so here it only refreshed: 1.4 s instead of 3 min 12 s."
 * While it starts: the steps running now and how long since the open.
 */
export function openedWords(env: Opened): { lead: string; detail: string | null } {
  const bringup = env.bringup;
  const from = startedFrom(env);
  const woke = Boolean(wakesFrom(env));
  if (env.state === 'starting') {
    const since = bringup?.total_s !== null && bringup?.total_s !== undefined ? `, ${seconds(bringup.total_s)} since ${env.resumed_by === 'owner' ? 'you resumed it' : woke ? 'the resume' : 'the open'}` : '';
    const now = bringup?.running ?? [];
    const lead = now.length
      ? `${now.map((step) => `${stepName(step.step)}: ${seconds(step.so_far_s)} so far`).join(', ')}${since}.`
      : `Starting${since}.`;
    return { lead, detail: waitingWords(env) };
  }
  if (env.state === 'failed') {
    const failed = bringup?.steps.find((step) => step.ok === false);
    return { lead: failed ? `${stepName(failed.step)} failed after ${seconds(failed.seconds)}.` : 'It did not come up.', detail: null };
  }
  const total = bringup?.total_s !== null && bringup?.total_s !== undefined ? seconds(bringup.total_s) : null;
  const verb = woke ? 'Woke' : 'Opened';
  const lead = total ? `${verb} in ${total}${from ? ` from ${from}` : ''}.` : from ? `${verb} from ${from}.` : `${verb}.`;
  return { lead: woke ? lead : lead.replace(' from ', ' on '), detail: preparedDetail(env.prepared) };
}

function waitingWords(env: Opened): string | null {
  const from = wakesFrom(env);
  if (from === 'paused') return 'A paused machine comes back as it was, its dev server still running. Usually under 10 s.';
  if (from === 'snapshot' && env.machine?.provider === 'docker') return 'A snapshot comes back from its image on this computer. Usually a few seconds.';
  if (from === 'snapshot') return 'A snapshot comes back on a new machine. Usually about a minute.';
  if (from === 'files') return 'Its files come back on a fresh machine. A few minutes.';
  return env.prepared?.why ? sentence(env.prepared.why) : null;
}

function preparedDetail(prepared: EnvPrepared | undefined): string | null {
  if (!prepared) return null;
  if (prepared.used && prepared.setup_s) {
    const age = prepared.from?.age ? ` ${prepared.from.age} ago` : '';
    return `Setup ran when it was built${age}, so here it only refreshed: ${seconds(prepared.setup_s.here)} instead of ${seconds(prepared.setup_s.build)}.`;
  }
  if (prepared.used) return 'It started from a prepared machine, so nothing was set up here.';
  const parts = [prepared.why ? sentence(prepared.why) : null];
  if (prepared.setup) parts.push(prepared.setup.ok ? `Setup took ${seconds(prepared.setup.seconds)}.` : `Setup failed after ${seconds(prepared.setup.seconds)}.`);
  if (prepared.build?.started) parts.push('A prepared machine is being built from it, so the next open skips setup.');
  else if (prepared.build?.why) parts.push(sentence(`No prepared machine was built: ${prepared.build.why}`));
  return parts.filter(Boolean).join(' ') || null;
}

/** The app's state as a status: listening, not listening, or not started. */
export function appStatus(app: EnvApp): StatusLabel {
  if (app.listening) return { label: 'Listening', tone: 'ok' };
  if (app.why?.startsWith('it did not start')) return { label: 'Did not start', tone: 'bad' };
  return { label: 'Not listening', tone: 'warn' };
}

/** When the app listens on another port: what that probably means, and the one line that fixes it. */
export function otherPortWords(app: EnvApp): { sentence: string; fix: string } | null {
  const other = app.listening_instead?.filter((port) => port !== app.port) ?? [];
  if (app.listening || other.length !== 1) return null;
  return {
    sentence: `Port ${other[0]} listens instead. If that is the app, set its port in the repository's environment file.`,
    fix: `port = ${other[0]}`,
  };
}

/** The scale of the steps chart: its end, rounded up to a tidy number of seconds. */
export function scaleEnd(total: number): number {
  const steps = [1, 2, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240, 300, 600];
  return steps.find((step) => step >= total) ?? Math.ceil(total / 60) * 60;
}
