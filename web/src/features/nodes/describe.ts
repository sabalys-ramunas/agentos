/** Plain words about a node, read from its data. Pure, so it is tested without a screen. */
import type { NodeConnections, Schedule } from '@/api/types';
import type { Band } from '@/components/charts/time-chart';
import { projectFires } from '@/lib/cron';

const WEEK = 7 * 86_400_000;
import { plural } from '@/lib/format';

export function listWords(items: string[]): string {
  if (items.length <= 1) return items[0] ?? '';
  return `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`;
}

interface Facts {
  lanes: number;
  /** Workspace names, in the order the panel shows them. */
  workspaces: string[];
  brains: string[];
  /** Null when the node does not report its schedules yet. */
  schedules: Schedule[] | null;
}

/** "Runs 6 lanes for Aster Coffee and Personal, holds the claude-max brain, and fires 22 schedules a day." */
export function describeNode({ lanes, workspaces, brains, schedules }: Facts, now = Date.now()): string {
  const parts: string[] = [];
  if (lanes === 0) parts.push('Runs no accounts yet');
  else {
    const owners = workspaces.length > 3 ? `${workspaces.length} workspaces` : listWords(workspaces);
    parts.push(`Runs ${plural(lanes, 'account')}${owners ? ` for ${owners}` : ''}`);
  }
  if (brains.length) parts.push(`holds the ${listWords(brains)} ${brains.length === 1 ? 'brain' : 'brains'}`);
  if (schedules) {
    const running = schedules.filter((item) => item.enabled !== false);
    // Counted over a week, so a Monday-only schedule is not read as a daily one.
    const weekly = running.reduce((sum, item) => sum + (item.cron ? (projectFires(item.cron, now, now + WEEK, 'UTC')?.length ?? 0) : 0), 0);
    const daily = weekly / 7;
    const watches = running.filter((item) => !item.cron).length;
    const rate = daily >= 2 ? `${plural(Math.round(daily), 'time')} a day` : weekly ? `${plural(weekly, 'time')} a week` : null;
    const when = [rate, watches ? 'on a change' : null].filter(Boolean).join(' or ');
    if (running.length) parts.push(`has ${plural(running.length, 'schedule')} that ${running.length === 1 ? 'fires' : 'fire'} ${when}`);
  }
  if (parts.length === 1) return `${parts[0]}.`;
  return `${parts.slice(0, -1).join(', ')}, and ${parts[parts.length - 1]}.`;
}

/**
 * When the node was away, from its connection log: each disconnect up to the next connect.
 * One still open runs to now. These shade every chart on the page, so they all line up.
 */
export function incidentsFrom(connections: NodeConnections | null | undefined, now = Date.now()): Band[] {
  if (!connections) return [];
  const events = [...connections.events].sort((a, b) => Date.parse(a.at) - Date.parse(b.at));
  const bands: Band[] = [];
  let down: number | null = null;
  for (const event of events) {
    const at = Date.parse(event.at);
    if (event.kind === 'disconnected' && down === null) down = at;
    else if (event.kind === 'connected' && down !== null) {
      bands.push({ from: down, to: at, label: 'Machine away' });
      down = null;
    }
  }
  if (down !== null) bands.push({ from: down, to: now, label: 'Machine away' });
  return bands;
}

/** "41 min" or "2 h 10 min". */
export function span(ms: number): string {
  const minutes = Math.max(1, Math.round(ms / 60_000));
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (hours < 48) return rest ? `${hours} h ${rest} min` : `${hours} h`;
  return `${Math.round(hours / 24)} days`;
}

/** "19 days", "4 h", for an uptime in seconds. */
export function uptime(seconds: number): string {
  if (seconds >= 2 * 86_400) return `${Math.floor(seconds / 86_400)} days`;
  if (seconds >= 3_600) return `${Math.floor(seconds / 3_600)} h`;
  return `${Math.max(1, Math.floor(seconds / 60))} min`;
}
