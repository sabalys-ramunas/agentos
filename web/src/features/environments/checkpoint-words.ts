/**
 * Checkpoints as a line of work (concept): newest first, with the working copy on top, the moment the
 * agent went back, and the checkpoints that went back left on a side line of their own. Pure, and tested.
 */
import type { EnvCheckpoint } from '@/api/types';

export type CheckpointRow =
  | { kind: 'now'; since: EnvCheckpoint['since']; base: { number: number; note: string } | null }
  | { kind: 'restore'; at: string; to: number }
  | { kind: 'checkpoint'; checkpoint: EnvCheckpoint; number: number; side: boolean; current: boolean; forks: boolean };

/**
 * The rows, newest first. A checkpoint made after the one the agent went back to, and before it went back,
 * is on the side line (its work is no longer in the working copy). The one it went back to is the fork.
 */
export function checkpointRows(checkpoints: EnvCheckpoint[]): CheckpointRow[] {
  const ordered = [...checkpoints].sort((a, b) => a.at.localeCompare(b.at));
  const numbered = ordered.map((checkpoint, index) => ({ checkpoint, number: index + 1 }));
  const target = [...numbered].reverse().find((item) => item.checkpoint.restored_at);
  const restoredAt = target?.checkpoint.restored_at ?? null;
  const side = (item: (typeof numbered)[number]) => Boolean(target && restoredAt && item.checkpoint.at > target.checkpoint.at && item.checkpoint.at <= restoredAt);
  const onLine = numbered.filter((item) => !side(item));
  const base = target ?? onLine[onLine.length - 1] ?? null;
  const rows: { at: string; row: CheckpointRow }[] = numbered.map((item) => ({
    at: item.checkpoint.at,
    row: { kind: 'checkpoint', checkpoint: item.checkpoint, number: item.number, side: side(item), current: item === base, forks: item === target },
  }));
  if (target && restoredAt) rows.push({ at: restoredAt, row: { kind: 'restore', at: restoredAt, to: target.number } });
  rows.sort((a, b) => b.at.localeCompare(a.at) || (a.row.kind === 'restore' ? -1 : b.row.kind === 'restore' ? 1 : 0));
  const now: CheckpointRow = { kind: 'now', since: base?.checkpoint.since ?? { files: 0, added: 0, removed: 0 }, base: base ? { number: base.number, note: base.checkpoint.note } : null };
  return [now, ...rows.map((item) => item.row)];
}
