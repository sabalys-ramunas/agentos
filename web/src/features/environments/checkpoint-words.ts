/**
 * Checkpoints as a line of work: newest first, with the working copy on top, the moment the agent went
 * back, and the checkpoints it went back past on a side line of their own. The node marks a rollback with
 * the checkpoint it makes first ("before rollback to 3"), so the line is read from the checkpoints alone.
 * Pure, and tested.
 */
import type { EnvCheckpoint } from '@/api/types';

/** Files and lines changed, from a checkpoint to the work tree now, over every repository. */
export interface Change {
  files: number;
  added: number;
  removed: number;
}

export type CheckpointRow =
  | { kind: 'now'; since: Change | null; base: { number: number; note: string | null } | null }
  | { kind: 'restore'; at: string; to: number }
  | { kind: 'checkpoint'; checkpoint: EnvCheckpoint; number: number; side: boolean; current: boolean; forks: boolean };

/** From the checkpoint to now, summed over its repositories; null when the machine did not say. */
export function sinceOf(checkpoint: EnvCheckpoint): Change | null {
  const diffs = checkpoint.repos.map((repo) => repo.diff);
  if (!diffs.length || diffs.some((diff) => !diff)) return null;
  return diffs.reduce<Change>(
    (sum, diff) => ({
      files: sum.files + (diff?.files_added ?? 0) + (diff?.files_changed ?? 0) + (diff?.files_removed ?? 0),
      added: sum.added + (diff?.lines_added ?? 0),
      removed: sum.removed + (diff?.lines_removed ?? 0),
    }),
    { files: 0, added: 0, removed: 0 },
  );
}

/** The checkpoint a rollback went back to, from the one agentos made before it ("before rollback to 3"). */
export function rollbackTarget(checkpoint: Pick<EnvCheckpoint, 'auto'>): number | null {
  const match = checkpoint.auto?.match(/rollback to (\d+)/);
  return match ? Number(match[1]) : null;
}

/** The agent's note, or the words agentos gave one it made itself. */
export const noteOf = (checkpoint: Pick<EnvCheckpoint, 'note' | 'auto'>) => checkpoint.note ?? (checkpoint.auto ? `Kept ${checkpoint.auto}` : 'No note');

/** "/work/shop" as "shop": the folder a person knows. */
export const repoDir = (dir: string) => dir.replace(/\/+$/, '').split('/').pop() || dir;

/**
 * The rows, newest first. The last rollback splits the line: the checkpoints after the one it went back to,
 * up to the one agentos made before it, are on the side line (their work is no longer in the work tree).
 * The working copy builds on the newest checkpoint still on the line.
 */
export function checkpointRows(checkpoints: EnvCheckpoint[]): CheckpointRow[] {
  const ordered = [...checkpoints].sort((a, b) => a.id - b.id);
  const auto = [...ordered].reverse().find((checkpoint) => rollbackTarget(checkpoint) !== null);
  const to = auto ? rollbackTarget(auto) : null;
  const target = to !== null ? ordered.find((checkpoint) => checkpoint.id === to) : undefined;
  const side = (checkpoint: EnvCheckpoint) => Boolean(auto && to !== null && checkpoint.id > to && checkpoint.id <= auto.id);
  const onLine = ordered.filter((checkpoint) => !side(checkpoint));
  const base = onLine[onLine.length - 1] ?? null;
  const rows: CheckpointRow[] = [...ordered].reverse().flatMap((checkpoint): CheckpointRow[] => {
    const row: CheckpointRow = { kind: 'checkpoint', checkpoint, number: checkpoint.id, side: side(checkpoint), current: checkpoint === base, forks: checkpoint === target };
    // the rollback happened just after agentos kept the work as it was
    return checkpoint === auto && to !== null && checkpoint.at ? [{ kind: 'restore', at: checkpoint.at, to }, row] : [row];
  });
  const now: CheckpointRow = { kind: 'now', since: base ? sinceOf(base) : null, base: base ? { number: base.id, note: base.note } : null };
  return [now, ...rows];
}
