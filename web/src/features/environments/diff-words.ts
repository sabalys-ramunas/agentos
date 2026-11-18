/**
 * A checkpoint's diff as the dialog shows it: each repository's files with git's status and line counts,
 * and each file's lines cut from the repository's patch. The node caps the patch, so a file can have its
 * counts and no lines. Pure, and tested.
 */
import type { EnvCheckpointDiff } from '@/api/types';
import { repoDir } from './checkpoint-words';

export interface DiffLine {
  kind: 'add' | 'del' | 'ctx' | 'hunk';
  text: string;
}

export interface DiffFile {
  /** The repository's folder ("shop"). */
  repo: string;
  path: string;
  status: 'A' | 'M' | 'D' | 'T';
  /** Null for a binary file. */
  added: number | null;
  removed: number | null;
  /** Null when the patch was cut before this file. */
  lines: DiffLine[] | null;
}

export const STATUS_WORDS: Record<DiffFile['status'], string> = { A: 'New since', M: 'Changed', D: 'Deleted since', T: 'Type changed' };

/** One unified patch split by file: the path (the b side) to its lines from the first hunk on. */
export function patchFiles(patch: string): Map<string, DiffLine[]> {
  const out = new Map<string, DiffLine[]>();
  let current: DiffLine[] | null = null;
  for (const line of patch.split('\n')) {
    const head = line.match(/^diff --git a\/(.+?) b\/(.+)$/);
    if (head) {
      current = [];
      out.set(head[2] ?? head[1] ?? '', current);
      continue;
    }
    if (!current) continue;
    if (line.startsWith('@@')) current.push({ kind: 'hunk', text: line });
    else if (!current.length) continue; // index, mode, --- and +++ lines before the first hunk
    else if (line.startsWith('+')) current.push({ kind: 'add', text: line.slice(1) });
    else if (line.startsWith('-')) current.push({ kind: 'del', text: line.slice(1) });
    else if (line.startsWith('\\')) continue; // "\ No newline at end of file"
    else current.push({ kind: 'ctx', text: line.startsWith(' ') ? line.slice(1) : line });
  }
  return out;
}

/** Every repository's files, in the order git listed them, each with its lines when the patch holds them. */
export function diffFiles(diff: EnvCheckpointDiff): DiffFile[] {
  return diff.repos.flatMap((repo) => {
    const lines = patchFiles(repo.patch);
    return repo.files.map((file) => ({
      repo: repoDir(repo.dir),
      path: file.path,
      status: file.status,
      added: file.lines_added,
      removed: file.lines_removed,
      lines: lines.get(file.path) ?? null,
    }));
  });
}

/** Files git counted beyond the ones listed, and whether the patch was cut. */
export function diffCut(diff: EnvCheckpointDiff): { more: number; truncated: boolean } {
  return {
    more: diff.repos.reduce((sum, repo) => sum + Math.max(0, repo.files_total - repo.files.length), 0),
    truncated: diff.repos.some((repo) => repo.truncated),
  };
}
