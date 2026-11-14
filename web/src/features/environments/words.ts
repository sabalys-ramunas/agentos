/**
 * The words of Environments: each state as a status, money, time left, the machine and why one closed.
 * Everything here reads what the node serves and nothing else.
 */
import type { EnvCommand, EnvState, EnvSummary } from '@/api/types';
import type { StatusLabel } from '@/lib/labels';

/** Running is ink in Mono, so starting, working and closing pulse; idle is the hollow ring. */
export const ENV_STATUS: Record<EnvState, StatusLabel> = {
  starting: { label: 'Starting', tone: 'live', pulse: true },
  ready: { label: 'Ready', tone: 'ok' },
  working: { label: 'Working', tone: 'live', pulse: true },
  idle: { label: 'Idle', tone: 'idle' },
  closing: { label: 'Closing', tone: 'live', pulse: true },
  closed: { label: 'Closed', tone: 'neutral' },
  failed: { label: 'Failed', tone: 'bad' },
};

/** One line on what each state means, for the status's title and the page's lede. */
export const ENV_STATE_SENTENCE: Record<EnvState, string> = {
  starting: 'The machine is booting and the repository is being cloned.',
  ready: 'The machine is up and waiting for the agent.',
  working: 'The agent called it in the last 2 minutes.',
  idle: 'No call from the agent for 10 minutes or more.',
  closing: 'The machine and its browser are being deleted.',
  closed: 'The machine and its browser are deleted.',
  failed: 'The machine did not start, so it was deleted.',
};

export const isOpen = (env: Pick<EnvSummary, 'state'>) => env.state !== 'closed' && env.state !== 'failed';

/** The time left is short enough to say so in amber. */
export const SHORT_MS = 15 * 60_000;

const MINUTE = 60_000;

/**
 * Euros: two places for what it cost so far ("€0.21"), one significant figure under a cent ("€0.003",
 * "€0.0004") so a few minutes on a small machine never reads as nothing, and up to four for a price an hour ("€0.0121").
 */
export function euro(value: number, places = 2): string {
  if (!Number.isFinite(value)) return '–';
  if (places === 2 && value > 0 && value < 0.01) {
    if (value < 0.0001) return 'under €0.0001';
    return `€${value.toFixed(Math.min(4, -Math.floor(Math.log10(value))))}`;
  }
  return `€${value.toFixed(places)}`;
}

/** The price an hour, with as many places as it needs (at least two). */
export function hourly(value: number): string {
  if (value === 0) return 'Free';
  const places = value >= 0.1 ? 2 : 4;
  return `${euro(value, places)} an hour`;
}

/** "2 h 14 min", "48 min", "under a minute": a span a person reads. */
export function span(ms: number): string {
  const minutes = Math.floor(Math.max(0, ms) / MINUTE);
  if (minutes < 1) return 'under a minute';
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return minutes % 60 ? `${hours} h ${minutes % 60} min` : `${hours} h`;
  return `${Math.round(hours / 24)} days`;
}

/** How long an environment has been open, or how long it ran when it is closed. */
export function ageOf(env: EnvSummary, now: number): number {
  const end = !isOpen(env) && env.closed_at ? Date.parse(env.closed_at) : now;
  return Math.max(0, end - Date.parse(env.created_at));
}

/** Milliseconds before the node closes it, or null for one that is closed. */
export function leftOf(env: EnvSummary, now: number): number | null {
  if (!isOpen(env) || env.state === 'closing') return null;
  return Date.parse(env.expires_at) - now;
}

/** "1 h 46 min left", "Ends now". */
export function leftWords(left: number): string {
  return left <= 0 ? 'Ends now' : `${span(left)} left`;
}

/** "github.com/harbor-supply/shop" as "harbor-supply/shop": the host is nearly always the same. */
export function repoName(repo: string): string {
  return repo.replace(/^https?:\/\//, '').replace(/^github\.com\//, '').replace(/\.git$/, '');
}

/** "Claude Code in T3 Code", or just the agent when the client is the agent itself or unknown. */
export function agentWords(agent: EnvSummary['agent']): string {
  return agent.client && agent.client !== agent.name ? `${agent.name} in ${agent.client}` : agent.name;
}

/** "2 CPU, 4 GB": what the machine has. */
export function machineSize(machine: EnvSummary['machine']): string {
  return `${machine.cpu} CPU, ${machine.memory_gb} GB`;
}

const PROVIDERS: Record<string, string> = { hetzner: 'Hetzner', docker: 'Docker' };
export const providerName = (provider: string) => PROVIDERS[provider] ?? provider;

/** "cpx22 in nbg1, Hetzner", or "A Docker container on the node" for a machine with no type of its own. */
export function machineWhere(machine: EnvSummary['machine']): string {
  if (machine.provider === 'docker') return 'A Docker container on the node';
  return `${machine.server_type} in ${machine.location}, ${providerName(machine.provider)}`;
}

/**
 * Why it closed. The node may send a short code or its own sentence; a code is put in words, a sentence
 * is shown as it is, and nothing at all says so.
 */
const REASONS: Record<string, string> = {
  agent: 'The agent closed it when its work was done.',
  owner: 'You closed it.',
  person: 'A person closed it from the app.',
  idle: 'It was closed after an hour with no call from the agent.',
  expired: 'It reached its time limit, so the node closed it.',
  limit: 'It reached its time limit, so the node closed it.',
  boot_failed: 'The machine did not start in time, so the node deleted it.',
  failed: 'The machine did not start, so the node deleted it.',
  discarded: 'It was closed and its unsaved work was discarded.',
};

export function closedReason(reason: string | null | undefined, state: EnvState): string {
  if (!reason) return state === 'failed' ? 'The machine did not start. The node did not say why.' : 'The node did not say why it closed.';
  const known = REASONS[reason.trim().toLowerCase()];
  if (known) return known;
  const text = reason.trim();
  return /[.!?]$/.test(text) ? text.charAt(0).toUpperCase() + text.slice(1) : `${text.charAt(0).toUpperCase()}${text.slice(1)}.`;
}

/** A command the terminal ran is a job (it has a run page) when it carries the node's job id. */
export const isJob = (command: EnvCommand) => command.id.startsWith('job-');

/** A command's state as a status. The node uses the run states; anything else is shown as it is, quietly. */
export function commandStatus(command: EnvCommand): StatusLabel {
  switch (command.state) {
    case 'running':
      return { label: 'Running', tone: 'live', pulse: true };
    case 'passed':
    case 'ok':
    case 'done':
      return { label: command.exit_code === undefined || command.exit_code === null || command.exit_code === 0 ? 'Done' : `Exit ${command.exit_code}`, tone: 'ok' };
    case 'failed':
      return { label: command.exit_code !== undefined && command.exit_code !== null ? `Exit ${command.exit_code}` : 'Failed', tone: 'bad' };
    case 'cancelled':
      return { label: 'Cancelled', tone: 'idle' };
    case 'timed_out':
      return { label: 'Timed out', tone: 'warn' };
    case 'lost':
      return { label: 'Lost', tone: 'warn' };
    default:
      return { label: command.state.charAt(0).toUpperCase() + command.state.slice(1).replace(/_/g, ' '), tone: 'neutral' };
  }
}

/** Open first (newest first), then closed (newest closing first). */
export function splitEnvironments(environments: EnvSummary[]): { open: EnvSummary[]; closed: EnvSummary[] } {
  const open = environments.filter(isOpen).sort((a, b) => b.created_at.localeCompare(a.created_at));
  const closed = environments
    .filter((env) => !isOpen(env))
    .sort((a, b) => (b.closed_at ?? b.created_at).localeCompare(a.closed_at ?? a.created_at));
  return { open, closed };
}

/** The lede over the list: "2 working, 1 idle · €0.41 so far". */
export function openSummary(open: EnvSummary[]): string {
  if (!open.length) return 'None open right now.';
  const order: EnvState[] = ['working', 'starting', 'ready', 'idle', 'closing'];
  const parts = order
    .map((state) => [state, open.filter((env) => env.state === state).length] as const)
    .filter(([, count]) => count > 0)
    .map(([state, count]) => `${count} ${ENV_STATUS[state].label.toLowerCase()}`);
  const cost = open.reduce((sum, env) => sum + env.cost_eur, 0);
  return `${parts.join(', ')} · ${euro(cost)} so far`;
}

/** The short form of a commit: seven characters. */
export const shortCommit = (commit: string) => commit.slice(0, 7);
