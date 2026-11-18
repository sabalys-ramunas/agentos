/**
 * The words of how an environment opened (concept): each bring-up step's name, seconds as a person reads
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

/** Where it started from, in a few words: "a warm, prepared machine", "a paused machine". */
export function startedFrom(env: Pick<EnvDetail, 'prepared' | 'resumed'>): string | null {
  if (env.resumed) return env.resumed.from === 'paused' ? 'its paused machine' : env.resumed.from === 'snapshot' ? 'its snapshot' : 'its saved files';
  const prepared = env.prepared;
  if (!prepared) return null;
  if (prepared.used && prepared.warm) return 'a warm, prepared machine';
  if (prepared.used) return 'a prepared machine';
  if (prepared.warm) return 'a warm machine';
  return null;
}

/** Steps skipped, and the seconds they would have taken. */
export function skipped(bringup: EnvBringUp): { steps: EnvBringUpStep[]; seconds: number } {
  const steps = bringup.steps.filter((step) => step.skipped);
  return { steps, seconds: steps.reduce((sum, step) => sum + step.seconds, 0) };
}

const sentence = (text: string) => {
  const trimmed = text.trim();
  if (!trimmed) return '';
  const capital = trimmed.charAt(0).toUpperCase() + trimmed.slice(1);
  return /[.!?]$/.test(capital) ? capital : `${capital}.`;
};

/** The node's own words, as a sentence: capitalised and ended. */
export const nodeSentence = sentence;

/**
 * The lead and the line under it. "Opened in 4.1 s on a warm, prepared machine." then where the time
 * went: "Setup ran when it was built 2 days ago, so here it only refreshed: 1.4 s instead of 3 min 12 s."
 */
/** The small heading over it: what is happening, or that it happened. */
export function openedEyebrow(env: Pick<EnvDetail, 'state' | 'prepared' | 'resumed'>): string {
  if (env.state !== 'starting') return 'How it opened';
  return env.resumed ? `Waking ${startedFrom(env) ?? ''}`.trim() : 'Opening now';
}

export function openedWords(env: Pick<EnvDetail, 'state' | 'prepared' | 'resumed' | 'bringup'>): { lead: string; detail: string | null } {
  const bringup = env.bringup;
  const from = startedFrom(env);
  if (env.state === 'starting' && bringup?.now) {
    return { lead: `${stepName(bringup.now.step)}: ${seconds(bringup.now.so_far_s)} so far, ${seconds(bringup.total_s)} since ${env.resumed ? 'you resumed it' : 'the open'}.`, detail: waitingWords(env) };
  }
  const total = bringup ? seconds(bringup.total_s) : null;
  const lead = total ? `Opened in ${total}${from ? ` on ${from}` : ''}.` : from ? `Opened on ${from}.` : 'Opened.';
  return { lead: env.resumed ? lead.replace('Opened', 'Woke') : lead, detail: preparedDetail(env.prepared) };
}

function waitingWords(env: Pick<EnvDetail, 'resumed' | 'prepared'>): string | null {
  if (env.resumed) return env.resumed.from === 'paused' ? 'A paused machine comes back as it was, its dev server still running. Usually under 10 s.' : 'A snapshot comes back on a new machine. Usually about a minute.';
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
