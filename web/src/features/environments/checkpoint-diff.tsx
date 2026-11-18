/**
 * One checkpoint's diff, large: from it to the work tree now, the way a rollback to it would undo. The
 * files on the left with git's status and their counts, the picked file's lines on the right. Added and
 * removed lines take the diff tokens, the only colour here, because they are what changed. The node caps
 * the patch, and the dialog says when it did.
 */
import { useState } from 'react';
import { useCheckpointDiff } from '@/api/hooks';
import { ApiError } from '@/api/http';
import type { EnvCheckpoint, EnvDetail } from '@/api/types';
import { Dialog } from '@/components/ui/dialog';
import { Skeleton } from '@/components/ui/states';
import { cn } from '@/lib/cn';
import { dateTime, plural } from '@/lib/format';
import { noteOf } from './checkpoint-words';
import { STATUS_WORDS, diffCut, diffFiles } from './diff-words';
import type { DiffFile } from './diff-words';

export function CheckpointDiffDialog({ org, env, picked, onClose }: { org: string; env: EnvDetail; picked: { checkpoint: EnvCheckpoint; number: number } | null; onClose: () => void }) {
  return (
    <Dialog
      open={picked !== null}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      title={picked ? `Since checkpoint ${picked.number}: ${noteOf(picked.checkpoint)}` : 'Checkpoint'}
      description={picked ? `What changed from it to the work tree now, which a rollback to it would undo.${picked.checkpoint.at ? ` Made ${dateTime(picked.checkpoint.at)}.` : ''} The note is the agent's own.` : undefined}
      className="sm:top-[6vh] sm:max-w-[min(1120px,calc(100vw-32px))]"
    >
      {picked ? <DiffBody key={picked.checkpoint.id} org={org} env={env.id} checkpoint={picked.checkpoint.id} /> : null}
    </Dialog>
  );
}

function DiffBody({ org, env, checkpoint }: { org: string; env: string; checkpoint: number }) {
  const diff = useCheckpointDiff(org, env, checkpoint);
  const [pickedPath, setPickedPath] = useState<string | null>(null);
  if (diff.isPending) return <Skeleton className="h-80 w-full rounded-lg" />;
  if (!diff.data) {
    const why =
      diff.error instanceof ApiError && diff.error.status === 503
        ? 'The machine is not up now, so git cannot be asked. Nothing was changed.'
        : diff.error instanceof ApiError && diff.error.status === 403
          ? 'Only the owner reads what changed since a checkpoint.'
          : diff.error instanceof ApiError
            ? `${diff.error.message} Nothing was changed.`
            : 'The diff could not be read. Nothing was changed.';
    return <p className="rounded-lg border border-line px-4 py-6 text-center text-14 text-muted">{why}</p>;
  }
  const files = diffFiles(diff.data);
  const cut = diffCut(diff.data);
  if (!files.length) return <p className="rounded-lg border border-line px-4 py-6 text-center text-14 text-muted">Nothing changed since this checkpoint.</p>;
  const keyOf = (entry: DiffFile) => `${entry.repo}/${entry.path}`;
  const file = files.find((entry) => keyOf(entry) === pickedPath) ?? files[0];
  const added = files.reduce((sum, entry) => sum + (entry.added ?? 0), 0);
  const removed = files.reduce((sum, entry) => sum + (entry.removed ?? 0), 0);
  const repos = new Set(files.map((entry) => entry.repo)).size;
  return (
    <div className="grid min-h-0 gap-4 md:grid-cols-[280px_minmax(0,1fr)]">
      <nav aria-label="Changed files" className="grid max-h-[60dvh] content-start gap-1 overflow-y-auto max-md:max-h-56">
        <p className="px-2 pb-1 font-mono text-code-caption text-muted tabular">
          {plural(files.length + cut.more, 'file')} <span className="text-diff-add-mark">+{added}</span> <span className="text-diff-del-mark">{'−'}{removed}</span>
        </p>
        {files.map((entry) => {
          const id = keyOf(entry);
          const active = entry === file;
          const folder = entry.path.split('/').slice(0, -1).join('/');
          return (
            <button
              key={id}
              type="button"
              aria-current={active || undefined}
              onClick={() => setPickedPath(id)}
              className={cn('grid min-w-0 gap-px rounded-md px-2 py-2 text-left transition-colors duration-(--duration-fast) hover:bg-hover', active && 'bg-pressed')}
            >
              <span className="flex min-w-0 items-center gap-2">
                <StatusMark status={entry.status} />
                <span className="min-w-0 truncate font-mono text-code-caption text-fg" title={id}>
                  {entry.path.split('/').pop()}
                </span>
              </span>
              <span className="flex min-w-0 items-center gap-2 pl-6 font-mono text-code-caption tabular">
                <span className="min-w-0 truncate text-faint">{repos > 1 ? `${entry.repo}/${folder}` : folder || entry.repo}</span>
                {entry.added === null ? (
                  <span className="ml-auto shrink-0 font-sans text-caption text-faint">binary</span>
                ) : (
                  <>
                    <span className="ml-auto shrink-0 text-diff-add-mark">+{entry.added}</span>
                    <span className="shrink-0 text-diff-del-mark">{'−'}{entry.removed ?? 0}</span>
                  </>
                )}
              </span>
            </button>
          );
        })}
        {cut.more || cut.truncated ? (
          <p className="px-2 pt-2 text-caption text-faint">
            {cut.more ? `${plural(cut.more, 'more file')} not listed. ` : ''}
            {cut.truncated ? 'The patch is cut at 200 KB, so some files show counts only. ' : ''}
            On the machine, <code className="font-mono text-code-caption">git diff refs/agentos/checkpoints/{diff.data.checkpoint}</code> shows all of it.
          </p>
        ) : null}
      </nav>
      {file ? <FileDiff file={file} /> : null}
    </div>
  );
}

/** git's letter as a small square mark: a file made since, changed, or deleted since. */
function StatusMark({ status }: { status: DiffFile['status'] }) {
  return (
    <span
      title={STATUS_WORDS[status]}
      className={cn(
        'flex size-4 shrink-0 items-center justify-center rounded-sm border font-mono text-micro',
        status === 'A' ? 'border-diff-add-mark text-diff-add-mark' : status === 'D' ? 'border-diff-del-mark text-diff-del-mark' : 'border-line-strong text-muted',
      )}
    >
      <span aria-hidden="true">{status}</span>
      <span className="sr-only">{STATUS_WORDS[status]}</span>
    </span>
  );
}

function FileDiff({ file }: { file: DiffFile }) {
  return (
    <section aria-label={`${file.repo}/${file.path}`} className="min-w-0 overflow-hidden rounded-lg border border-line">
      <header className="flex min-w-0 items-center gap-3 border-b border-line px-4 py-2">
        <span className="min-w-0 truncate font-mono text-code text-fg" title={file.path}>
          {file.path}
        </span>
        <span className="ml-auto shrink-0 text-caption text-faint">{STATUS_WORDS[file.status]}</span>
      </header>
      {file.lines?.length ? (
        <pre className="max-h-[56dvh] overflow-auto py-2 font-mono text-code-caption">
          {file.lines.map((line, index) => (
            <div
              key={index}
              className={cn(
                'grid grid-cols-[24px_minmax(0,1fr)] px-2',
                line.kind === 'add' && 'bg-diff-add',
                line.kind === 'del' && 'bg-diff-del',
                line.kind === 'hunk' && 'mt-2 text-faint first:mt-0',
              )}
            >
              <span aria-hidden="true" className={cn('select-none text-center', line.kind === 'add' ? 'text-diff-add-mark' : line.kind === 'del' ? 'text-diff-del-mark' : 'text-faint')}>
                {line.kind === 'add' ? '+' : line.kind === 'del' ? '−' : ''}
              </span>
              <span className={cn('whitespace-pre', line.kind === 'ctx' ? 'text-muted' : 'text-fg')}>
                <span className="sr-only">{line.kind === 'add' ? 'Added: ' : line.kind === 'del' ? 'Removed: ' : ''}</span>
                {line.text || ' '}
              </span>
            </div>
          ))}
        </pre>
      ) : (
        <p className="px-4 py-6 text-14 text-muted">{file.added === null ? 'A binary file: git counts no lines in it.' : file.lines ? 'Only its mode changed.' : 'Its lines are past the cut of the patch.'}</p>
      )}
    </section>
  );
}
