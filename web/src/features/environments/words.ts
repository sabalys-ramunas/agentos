/**
 * The words of Environments: each state as a status, money, time left, the machine and why one closed.
 * Everything here reads what the node serves and nothing else.
 */
import { agentSaid, agentVia } from '@/lib/agent-names';
import type { EnvCommand, EnvKeep, EnvSave, EnvState, EnvSummary } from '@/api/types';
import { timeAgo } from '@/lib/format';
import type { StatusLabel } from '@/lib/labels';

/** Running is ink in Mono, so starting, working and closing pulse; idle is the hollow ring. */
export const ENV_STATUS: Record<EnvState, StatusLabel> = {
  starting: { label: 'Starting', tone: 'live', starting: true },
  ready: { label: 'Ready', tone: 'ok' },
  working: { label: 'Working', tone: 'live', pulse: true },
  idle: { label: 'Idle', tone: 'idle' },
  gone: { label: 'Machine stopped', tone: 'bad' },
  closing: { label: 'Closing', tone: 'live', pulse: true },
  closed: { label: 'Closed', tone: 'neutral' },
  failed: { label: 'Did not start', tone: 'bad' },
};

/** One line on what each state means, for the status's title and the page's lede. */
export const ENV_STATE_SENTENCE: Record<EnvState, string> = {
  starting: 'The machine is booting and the repository is being cloned.',
  ready: 'The machine is up and waiting for the agent.',
  working: 'The agent called it in the last 2 minutes.',
  idle: 'No call from the agent for 10 minutes or more.',
  gone: 'Its machine stopped and will not come back. Close it, or ask the agent to open it again on a new machine.',
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
  if (!isOpen(env) || env.state === 'closing' || env.state === 'gone') return null;
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

/**
 * "Claude Code in T3 Code", or just "Claude Code" when it came in through itself: the names the node sends
 * (openajar.core.agent_names), never the raw subject or the client's version.
 */
export function agentWords(agent: EnvSummary['agent']): string {
  const via = agentVia(agent);
  return via ? `${agentSaid(agent)} in ${via}` : agentSaid(agent);
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

/** Open first (one whose machine stopped leads, then newest first), then closed (newest closing first). */
export function splitEnvironments(environments: EnvSummary[]): { open: EnvSummary[]; closed: EnvSummary[] } {
  const open = environments
    .filter(isOpen)
    .sort((a, b) => Number(b.state === 'gone') - Number(a.state === 'gone') || b.created_at.localeCompare(a.created_at));
  const closed = environments
    .filter((env) => !isOpen(env))
    .sort((a, b) => (b.closed_at ?? b.created_at).localeCompare(a.closed_at ?? a.created_at));
  return { open, closed };
}

/** The lede over the list: "2 working, 1 idle · €0.41 so far". */
export function openSummary(open: EnvSummary[]): string {
  if (!open.length) return 'None open right now.';
  const order: EnvState[] = ['gone', 'working', 'starting', 'ready', 'idle', 'closing'];
  const parts = order
    .map((state) => [state, open.filter((env) => env.state === state).length] as const)
    .filter(([, count]) => count > 0)
    .map(([state, count]) => `${count} ${ENV_STATUS[state].label.toLowerCase()}`);
  const cost = open.reduce((sum, env) => sum + env.cost_eur, 0);
  return `${parts.join(', ')} · ${euro(cost)} so far`;
}

export const shortCommit = (commit: string) => commit.slice(0, 7);

/**
 * GitHub's page that opens a pull request for a pushed branch, or null when there is none to open: the
 * repository is not on GitHub, or the branch is the default one (nothing to compare it with).
 */
export function pullRequestLink(repo: string | null | undefined, branch: string, defaultBranch = 'main'): string | null {
  const match = repo?.replace(/^https?:\/\//, '').replace(/\.git$/, '').match(/^github\.com\/([\w.-]+)\/([\w.-]+)$/);
  if (!match || !branch || branch === defaultBranch || branch === 'master') return null;
  const path = branch.split('/').map(encodeURIComponent).join('/');
  return `https://github.com/${match[1]}/${match[2]}/compare/${path}?expand=1`;
}

/** How each one stays, in the words the app uses everywhere, and one line on what that means. */
export const KEEP: Record<EnvKeep, { label: string; sentence: string }> = {
  none: { label: 'Single use', sentence: 'The machine is deleted when the work is done. Nothing is kept.' },
  live: { label: 'Kept running', sentence: 'The machine stays on between sessions, and every hour is billed.' },
  save: { label: 'Saves when idle', sentence: 'When idle, the whole machine is saved and the VM is deleted. An agent brings it back by opening it again.' },
};

/** A node that does not say is a node with single use only. */
export const keepOf = (env: Pick<EnvSummary, 'keep'>): EnvKeep => env.keep ?? 'none';

/** Hours in an average month, as providers bill them. */
const MONTH_HOURS = 730;

/** "€7.80 a month", "€0.05 a month", "Free". */
export function monthly(value: number): string {
  if (value === 0) return 'Free';
  return `${euro(value)} a month`;
}

/**
 * What it costs while nobody uses it: nothing once a single-use one is deleted, every hour for one kept
 * running, and only its save for one that saves when idle (the save's own price when the node has one).
 */
export function idleCost(env: Pick<EnvSummary, 'keep' | 'price_hourly_eur'>, save?: Pick<EnvSave, 'cost_eur_month'> | null): string {
  switch (keepOf(env)) {
    case 'live':
      return env.price_hourly_eur === 0 ? 'Nothing while unused' : `${monthly(env.price_hourly_eur * MONTH_HOURS)} while unused`;
    case 'save':
      return save ? `${monthly(save.cost_eur_month)} while saved` : 'Cents a month while saved';
    default:
      return 'Nothing after it is deleted';
  }
}

/** The short form for a list: null for single use, "€8.83 a month" kept running, "cents a month" saved. */
export function idleShort(env: Pick<EnvSummary, 'keep' | 'price_hourly_eur'>): string | null {
  const keep = keepOf(env);
  if (keep === 'live') return env.price_hourly_eur === 0 ? null : monthly(env.price_hourly_eur * MONTH_HOURS);
  return keep === 'save' ? 'cents a month' : null;
}

/** "412 MB", "1.2 GB": what the files of a save take. */
export function size(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  if (bytes < 1024 ** 3) return `${Math.round(bytes / 1024 ** 2)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
}

/** "12 GB snapshot" or "Files only, 412 MB": what a save holds. */
export function saveHolds(save: Pick<EnvSave, 'snapshot_gb' | 'files_bytes' | 'paused'>): string {
  if (save.paused) return 'Paused machine';
  if (save.snapshot_gb) return `${Number.isInteger(save.snapshot_gb) ? save.snapshot_gb : save.snapshot_gb.toFixed(1)} GB snapshot`;
  return save.files_bytes ? `Files only, ${size(save.files_bytes)}` : 'Files only';
}

/** A save the node deletes within this long says so. */
export const PRUNE_SOON_MS = 7 * 24 * 60 * MINUTE;

/** "Deleted in 3 days unless an agent opens it again", or null when the node keeps it for longer or does not say. */
export function pruneWords(save: Pick<EnvSave, 'prune_at'>, now: number): string | null {
  if (!save.prune_at) return null;
  const left = Date.parse(save.prune_at) - now;
  if (!Number.isFinite(left) || left > PRUNE_SOON_MS) return null;
  if (left <= 0) return 'Deleted soon unless an agent opens it again';
  return `Deleted in ${span(left)} unless an agent opens it again`;
}

/** What a person says to an agent to bring one back. */
export const continueLine = (title: string) => `continue ${title} in openajar`;

/**
 * The stage's line for an environment whose machine stopped: when, why in the node's words, and what is left
 * (close it, or the agent opens the same title on a new machine, from its save when it has one).
 */
export function goneWords(env: Pick<EnvSummary, 'title' | 'machine_gone' | 'last_save'>, now?: number): { lead: string; why: string | null; then: string } {
  const at = env.machine_gone?.at;
  const lead = at ? `Its machine stopped ${timeAgo(at, now).toLowerCase()}.` : 'Its machine stopped.';
  const reason = env.machine_gone?.why?.trim();
  const why = reason ? `${reason.charAt(0).toUpperCase()}${reason.slice(1)}${/[.!?]$/.test(reason) ? '' : '.'}` : null;
  const from = env.last_save ? ' from its last save' : '';
  return { lead, why, then: `It will not come back. Close it, or say "${continueLine(env.title)}" and the agent starts a new machine${from}.` };
}

/** What a call did, in the past tense a person uses ("Clicked on a page"), and what it was about. */
export interface CallWords {
  title: string;
  /** The call's own summary, without the scheme of an address and without the verb the title already says. */
  detail: string;
  /** Which picture goes beside it: a page, a command, a check of a design, a file, the screen, the machine. */
  kind: 'page' | 'command' | 'check' | 'file' | 'screen' | 'machine';
}

const CALL_TITLES: Record<string, [string, CallWords['kind']]> = {
  browser_navigate: ['Opened a page', 'page'],
  browser_snapshot: ['Read a page', 'page'],
  browser_screenshot: ['Took a picture of a page', 'page'],
  browser_wait: ['Waited for a page', 'page'],
  browser_tabs: ['Looked at its tabs', 'page'],
  browser_upload: ['Uploaded a file to a page', 'page'],
  browser_dialog_answer: ['Answered a page', 'page'],
  ui_check: ["Checked a page's design", 'check'],
  ui_diff: ["Compared a page's design", 'check'],
  lane_exec: ['Ran a command', 'command'],
  lane_job_start: ['Started a command', 'command'],
  lane_job_read: ["Read a command's output", 'command'],
  lane_job_cancel: ['Stopped a command', 'command'],
  lane_file_read: ['Read a file', 'file'],
  lane_file_write: ['Wrote a file', 'file'],
  lane_file_edit: ['Changed a file', 'file'],
  files_put: ['Put a file on the machine', 'file'],
  environment_upload: ['Brought files in', 'file'],
  environment_download: ['Gave you a file', 'file'],
  screen_snapshot: ['Looked at its screen', 'screen'],
  screen_open: ['Opened an app', 'screen'],
  environment_open: ['Opened its computer', 'machine'],
  environment: ['Looked at its computer', 'machine'],
  environment_save: ['Saved its computer', 'machine'],
  environment_close: ['Closed its computer', 'machine'],
};

export function callWords(tool: string, summary: string, fallback: (tool: string) => string): CallWords {
  const detail = summary.replace(/\bhttps?:\/\//g, '');
  if (tool === 'browser_act') {
    const typed = /^type\b/i.test(summary);
    return { title: `${typed ? 'Typed' : 'Clicked'} on a page`, detail: detail.replace(/^(click|type|press|select|hover)\s+/i, ''), kind: 'page' };
  }
  if (tool === 'screen_act') return screenActWords(detail);
  const known = CALL_TITLES[tool];
  return { title: known ? known[0] : fallback(tool), detail, kind: known ? known[1] : 'machine' };
}

/**
 * screen_act's summary ("press ctrl+s in window 3 on the screen of e7k2", "bring window 3 to the front ...") in
 * words: what the hands did, and the rest of the line without the machine's name, which the page already says.
 */
const SCREEN_ACTS: Array<[RegExp, string]> = [
  [/^type\s+/i, 'Typed on its screen'],
  [/^press\s+/i, 'Pressed keys on its screen'],
  [/^bring\s+/i, 'Brought a window to the front'],
  [/^drag\s*/i, 'Dragged on its screen'],
  [/^scroll\s*/i, 'Scrolled its screen'],
  [/^move\s*/i, 'Moved the pointer'],
  [/^double click\s*/i, 'Double-clicked on its screen'],
  [/^right click\s*/i, 'Right-clicked on its screen'],
  [/^click\s*/i, 'Clicked on its screen'],
];

function screenActWords(detail: string): CallWords {
  const rest = detail.replace(/\s*on the screen of \S+$/, '');
  for (const [verb, title] of SCREEN_ACTS) {
    if (verb.test(rest)) {
      return { title, detail: rest.replace(verb, '').replace(/\s*to the front$/, ''), kind: 'screen' };
    }
  }
  return { title: 'Used its screen', detail: rest, kind: 'screen' };
}

/** Where a machine is, as a person names it: "Nuremberg" for nbg1. An unknown code is shown as it is. */
const PLACES_BY_CODE: Record<string, string> = {
  nbg1: 'Nuremberg',
  fsn1: 'Falkenstein',
  hel1: 'Helsinki',
  ash: 'Ashburn',
  hil: 'Hillsboro',
  sin: 'Singapore',
};
export const placeOf = (location: string) => PLACES_BY_CODE[location] ?? location;

/** "2 CPU, 4 GB, in Nuremberg", or "2 CPU, 4 GB, on this computer" for a container. */
export function machineWords(machine: EnvSummary['machine']): string {
  return machine.provider === 'docker' ? `${machineSize(machine)}, on this computer` : `${machineSize(machine)}, in ${placeOf(machine.location)}`;
}
