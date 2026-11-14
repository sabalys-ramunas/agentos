/**
 * Machines as the table reads them: every node and every disposable VM as one row, with what it is
 * doing now, its load and its lanes, from what the node really serves. Nothing here is invented: a
 * value the node does not send stays null, and the screen draws an en dash for it.
 */
import type { FleetNode, Job, JobState, Vm } from '@/api/types';
import type { Kind } from '@/components/ui/kind';
import type { StatusLabel } from '@/lib/labels';
import { plural } from '@/lib/format';
import type { MachineSort } from './search';

/** From this much CPU a machine reads as busy (app-design.md, Machines). */
export const BUSY_CPU = 85;
/** The last hour, as the timeline and "doing now" read it. */
export const HOUR_MS = 3_600_000;

export interface MachineNow {
  /** The runs going on it now, newest first. */
  jobs: Job[];
  /** Accounts an agent is in right now. */
  agentLanes: number;
}

export interface MachineRow {
  /** The id in `/machines/<id>`. */
  id: string;
  /** What a person reads: the node's name, or the lane a VM is for. */
  name: string;
  type: 'node' | 'vm';
  /** The second line: where it is and its size, as far as the node says. */
  where: string;
  status: StatusLabel;
  /** Worst first: not answering, struggling, busy, starting, online. */
  rank: number;
  cpu: number | null;
  /** Memory used, in percent of the machine's own. */
  memory: number | null;
  memoryText: string | null;
  browsers: number | null;
  terminals: number | null;
  services: number | null;
  /** Requests waiting for a person on it, when the node says. */
  waiting: number | null;
  now: MachineNow;
  node?: FleetNode;
  vm?: Vm;
}

const GB = 1024;

export function gbShort(mb: number): string {
  const value = mb / GB;
  return `${value >= 10 || Number.isInteger(value) ? Math.round(value) : value.toFixed(1)} GB`;
}

/** The machine's state as a shape and a word. */
export function nodeStatus(node: FleetNode): { status: StatusLabel; rank: number } {
  if (node.state === 'down') return { status: { label: 'Not answering', tone: 'bad' }, rank: 0 };
  if (node.state === 'degraded' || node.degraded) return { status: { label: 'Struggling', tone: 'warn' }, rank: 1 };
  if (node.cpu >= BUSY_CPU) return { status: { label: 'Busy', tone: 'warn' }, rank: 2 };
  return { status: { label: 'Online', tone: 'ok' }, rank: 4 };
}

export function vmStatus(vm: Vm): { status: StatusLabel; rank: number } {
  switch (vm.state) {
    case 'ready':
      return { status: { label: 'Ready', tone: 'ok' }, rank: 4 };
    case 'creating':
    case 'booting':
      return { status: { label: 'Starting', tone: 'live', pulse: true }, rank: 3 };
    case 'deleting':
      return { status: { label: 'Deleting', tone: 'idle' }, rank: 3 };
    case 'deleted':
      return { status: { label: 'Deleted', tone: 'idle' }, rank: 5 };
    case 'failed':
      return { status: { label: 'Failed', tone: 'bad' }, rank: 0 };
    default:
      return { status: { label: vm.state, tone: 'neutral' }, rank: 3 };
  }
}

/** A VM still worth a row: one that is up or on its way, not one deleted long ago. */
export const vmIsCurrent = (vm: Vm) => !['deleted'].includes(vm.state);

/** "40 min left", "2 h 5 min left", or null when the node does not say. */
export function timeLeft(vm: Vm, now = Date.now()): string | null {
  const minutes = vm.minutes_left ?? (vm.expires_at ? Math.round((Date.parse(vm.expires_at) - now) / 60_000) : null);
  if (minutes === null || Number.isNaN(minutes)) return null;
  if (minutes <= 0) return 'deleting now';
  if (minutes < 60) return `${minutes} min left`;
  const rest = minutes % 60;
  return `${Math.floor(minutes / 60)} h${rest ? ` ${rest} min` : ''} left`;
}

/** Euros as the node prices a VM: four places for an hourly rate, two for a total. */
export const euros = (value: number, places = 2) => `€${value.toFixed(places)}`;

function runningOn(jobs: Job[], test: (job: Job) => boolean): Job[] {
  return jobs.filter((job) => job.state === 'running' && test(job)).sort((a, b) => b.started_at.localeCompare(a.started_at));
}

/** Every machine as one row: the nodes as they are served, then the VMs that are up or on their way. */
export function machineRows(nodes: FleetNode[], vms: Vm[], jobs: Job[]): MachineRow[] {
  const vmLanes = new Set(vms.map((vm) => vm.lane));
  const rows: MachineRow[] = nodes.map((node) => {
    const { status, rank } = nodeStatus(node);
    const away = node.state === 'down';
    const kinds = node.lanes_by_kind;
    const memory = node.mem.total_mb > 0 ? Math.round((node.mem.used_mb / node.mem.total_mb) * 100) : null;
    return {
      id: node.id,
      name: node.id,
      type: 'node',
      where: [zoneCity(node.timezone), node.mem.total_mb > 0 ? `${gbShort(node.mem.total_mb)} memory` : null].filter(Boolean).join(' · '),
      status,
      rank,
      cpu: away ? null : node.cpu,
      memory: away ? null : memory,
      memoryText: away || node.mem.total_mb <= 0 ? null : `${gbShort(node.mem.used_mb)} of ${gbShort(node.mem.total_mb)}`,
      browsers: kinds ? (kinds.browser ?? 0) + (kinds.android ?? 0) : null,
      terminals: kinds ? (kinds.terminal ?? 0) : null,
      services: kinds ? (kinds.service ?? 0) : null,
      waiting: node.approvals_waiting ?? null,
      // A VM's runs belong to the VM's row, not to the node that rents it.
      now: { jobs: away ? [] : runningOn(jobs, (job) => job.node === node.id && !vmLanes.has(job.lane)), agentLanes: away ? 0 : (node.lanes_active ?? 0) },
      node,
    };
  });
  for (const vm of vms.filter(vmIsCurrent)) {
    const { status, rank } = vmStatus(vm);
    rows.push({
      id: vm.id,
      name: vm.lane,
      type: 'vm',
      where: [vm.location, vm.server_type].filter(Boolean).join(' · '),
      status,
      rank,
      cpu: null,
      memory: null,
      memoryText: null,
      browsers: null,
      terminals: null,
      services: null,
      waiting: null,
      now: { jobs: runningOn(jobs, (job) => job.lane === vm.lane), agentLanes: 0 },
      vm,
    });
  }
  return rows;
}

/** "Europe/Vilnius" reads as "Vilnius time": the machine's clock, which is all the node says of where it is. */
export function zoneCity(zone: string | undefined | null): string | null {
  if (!zone || zone === 'UTC' || zone === 'Etc/UTC') return zone ? 'UTC clock' : null;
  const city = zone.split('/').pop()?.replaceAll('_', ' ');
  return city ? `${city} time` : null;
}

const NATURAL: Record<MachineSort, 'asc' | 'desc'> = { name: 'asc', state: 'asc', load: 'desc', memory: 'desc' };

/** The direction a column sorts in when nothing else is said. */
export const naturalDirection = (sort: MachineSort) => NATURAL[sort];

/** Sorted by a column; with no column the rows stay as served, nodes before VMs. */
export function sortRows(rows: MachineRow[], sort: MachineSort | undefined, dir?: 'asc' | 'desc'): MachineRow[] {
  if (!sort) return rows;
  const direction = (dir ?? NATURAL[sort]) === 'asc' ? 1 : -1;
  const value = (row: MachineRow): number | string => {
    if (sort === 'name') return row.id;
    if (sort === 'state') return row.rank;
    // A machine with no reading sorts last either way.
    const reading = sort === 'load' ? row.cpu : row.memory;
    return reading ?? (direction === 1 ? Infinity : -Infinity);
  };
  return [...rows].sort((a, b) => {
    const left = value(a);
    const right = value(b);
    const order = typeof left === 'string' && typeof right === 'string' ? left.localeCompare(right) : (left as number) - (right as number);
    return order * direction || a.id.localeCompare(b.id);
  });
}

/** One part of the sentence on top: plain words, a machine's name, or a number that wants a look. */
export type Part = string | { name: string } | { warn: string };

/** What matters, in one sentence: how many answer, which is busiest, and what is not answering. */
export function machinesSentence(rows: MachineRow[], now = Date.now()): Part[] {
  const nodes = rows.filter((row) => row.type === 'node');
  const vms = rows.filter((row) => row.type === 'vm');
  const away = nodes.filter((row) => row.node?.state === 'down');
  const online = nodes.filter((row) => row.node?.state !== 'down');
  const parts: Part[] = [];
  const working = (row: MachineRow) => row.now.jobs.length + row.now.agentLanes;

  if (nodes.length === 1) {
    const only = nodes[0]!;
    if (away.length) return [{ name: only.id }, ' is not answering. Its accounts are paused, and nothing was lost.'];
    parts.push({ name: only.id }, ' is your one machine');
    if (only.cpu !== null) parts.push(' at ', only.cpu >= BUSY_CPU ? { warn: `${only.cpu}%` } : `${only.cpu}%`, ' CPU');
    const job = only.now.jobs[0];
    if (job) parts.push(', running ', { name: job.command });
    else if (only.now.agentLanes) parts.push(`, with an agent in ${plural(only.now.agentLanes, 'account')}`);
    else parts.push(', and nothing is running on it now');
    parts.push('.');
  } else if (nodes.length > 1) {
    parts.push(`${online.length} online`);
    if (away.length) parts.push(', ', { warn: `${away.length} not answering` });
    parts.push('. ');
    const busiest = [...online].sort((a, b) => (b.cpu ?? 0) - (a.cpu ?? 0) || working(b) - working(a))[0];
    if (busiest && busiest.cpu !== null) {
      parts.push({ name: busiest.id }, ' is the busiest at ', busiest.cpu >= BUSY_CPU ? { warn: `${busiest.cpu}%` } : `${busiest.cpu}%`, '.');
    }
    if (away.length === 1) parts.push(' ', { name: away[0]!.id }, ' has not answered since ', lastSeen(away[0]!.node?.last_seen, now), '.');
  }
  const vm = vms.find((row) => row.vm?.state === 'ready') ?? vms[0];
  if (vm?.vm) {
    const left = timeLeft(vm.vm, now);
    const more = vms.length > 1 ? `, one of ${vms.length} rented` : '';
    parts.push(' ', { name: vm.vm.lane }, left ? ` is a disposable VM with ${left}${more}.` : ` is a disposable VM${more}.`);
  }
  return parts;
}

export function lastSeen(iso: string | undefined, now = Date.now()): string {
  if (!iso) return 'a while ago';
  const minutes = Math.round((now - Date.parse(iso)) / 60_000);
  if (minutes < 1) return 'a moment ago';
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  return hours < 48 ? `${hours} h ago` : `${Math.round(hours / 24)} days ago`;
}

/** The totals under the table. */
export function totals(rows: MachineRow[]): string {
  const sum = (pick: (row: MachineRow) => number | null) => rows.reduce((total, row) => total + (pick(row) ?? 0), 0);
  const online = rows.filter((row) => row.rank > 0 && row.status.tone !== 'idle').length;
  const parts = [plural(rows.length, 'machine'), `${online} online`, plural(sum((row) => row.browsers), 'browser'), plural(sum((row) => row.terminals), 'terminal')];
  const running = sum((row) => row.now.jobs.length);
  if (running) parts.push(`${plural(running, 'run')} going`);
  return parts.join(' · ');
}

/** The kind of work a run is, read from its command the way a person would: tests, a build, a dev server. */
export function jobKind(job: Pick<Job, 'command' | 'tests'>): Kind | null {
  if (job.tests) return 'test';
  const command = job.command.toLowerCase();
  if (/\b(pytest|vitest|jest|playwright|go test|swift test|cargo test|rspec|phpunit|mocha|e2e|test)\b/.test(command)) return 'test';
  if (/\b(dev|serve|start|preview)\b/.test(command)) return 'server';
  if (/\b(build|install|lint|typecheck|tsc|migrate|compile|ci)\b/.test(command)) return 'build';
  return null;
}

// ---------- The last hour ----------

export type BarTone = 'live' | 'ok' | 'bad' | 'idle';

const BAR_TONE: Record<JobState, BarTone> = {
  running: 'live',
  passed: 'ok',
  failed: 'bad',
  timed_out: 'bad',
  lost: 'bad',
  cancelled: 'idle',
};

export const BAR_WORDS: Record<BarTone, string> = { live: 'running', ok: 'passed', bad: 'failed', idle: 'cancelled' };

export interface Bar {
  job: Job;
  tone: BarTone;
  /** Where it starts and ends along the hour, from 0 to 1. */
  from: number;
  to: number;
}

export interface Lane {
  row: MachineRow;
  bars: Bar[];
}

/**
 * Each machine's runs in the last hour, as bars along it. Only real runs with a start (and an end, or
 * still running) are drawn; a machine with none keeps an empty line, so a quiet machine reads as quiet.
 */
export function lastHour(rows: MachineRow[], jobs: Job[], now = Date.now()): Lane[] {
  const start = now - HOUR_MS;
  const vmLanes = new Map(rows.filter((row) => row.vm).map((row) => [row.vm!.lane, row.id]));
  const at = (iso: string) => Math.min(1, Math.max(0, (Date.parse(iso) - start) / HOUR_MS));
  return rows.map((row) => ({
    row,
    bars: jobs
      .filter((job) => (vmLanes.get(job.lane) ?? job.node) === row.id)
      .filter((job) => Date.parse(job.finished_at ?? new Date(now).toISOString()) >= start && Date.parse(job.started_at) <= now)
      .map((job) => ({ job, tone: BAR_TONE[job.state], from: at(job.started_at), to: job.finished_at ? at(job.finished_at) : 1 }))
      .sort((a, b) => a.from - b.from),
  }));
}

/** True when there is anything at all in the hour: otherwise the timeline is left out, and the page says so. */
export const hourHasRuns = (lanes: Lane[]) => lanes.some((lane) => lane.bars.length > 0);

/** A run's state as a shape and a word. */
export const JOB_STATUS: Record<JobState, StatusLabel> = {
  running: { label: 'Running', tone: 'live', pulse: true },
  passed: { label: 'Passed', tone: 'ok' },
  failed: { label: 'Failed', tone: 'bad' },
  cancelled: { label: 'Cancelled', tone: 'idle' },
  timed_out: { label: 'Timed out', tone: 'bad' },
  lost: { label: 'Lost', tone: 'warn' },
};

/** "3 days", "5 h", "12 min": how long a machine has been up. */
export function upFor(seconds: number): string {
  if (seconds < 3_600) return `${Math.max(1, Math.round(seconds / 60))} min`;
  if (seconds < 172_800) return `${Math.round(seconds / 3_600)} h`;
  return `${Math.round(seconds / 86_400)} days`;
}
