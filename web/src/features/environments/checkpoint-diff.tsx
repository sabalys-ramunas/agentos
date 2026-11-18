/**
 * One checkpoint's diff (concept), large: what it changed against the one before it, or from it to the
 * working copy now. The files on the left with their counts, the picked file's lines on the right. Added and
 * removed lines take the diff tokens, the only colour here, because they are what changed.
 */
import { useState } from 'react';
import type { EnvCheckpoint, EnvDetail, EnvDiffFile } from '@/api/types';
import { useCheckpointDiff } from '@/api/hooks';
import { Dialog } from '@/components/ui/dialog';
import { Skeleton } from '@/components/ui/states';
import { SegmentedControl } from '@/components/ui/tabs';
import { cn } from '@/lib/cn';
import { dateTime } from '@/lib/format';

type Against = 'previous' | 'now';

export function CheckpointDiffDialog({ org, env, picked, onClose }: { org: string; env: EnvDetail; picked: { checkpoint: EnvCheckpoint; number: number } | null; onClose: () => void }) {
  const [against, setAgainst] = useState<Against>('previous');
  return (
    <Dialog
      open={picked !== null}
      onOpenChange={(open) => {
        if (!open) {
          onClose();
          setAgainst('previous');
        }
      }}
      title={picked ? `Checkpoint ${picked.number}: ${picked.checkpoint.note}` : 'Checkpoint'}
      description={picked ? `Made ${dateTime(picked.checkpoint.at)}. The note is the agent's own.` : undefined}
      className="sm:top-[6vh] sm:max-w-[min(1120px,calc(100vw-32px))]"
    >
      {picked ? (
        <div className="grid gap-4">
          <SegmentedControl<Against>
            label="Compare"
            value={against}
            onValueChange={setAgainst}
            options={[
              { value: 'previous', label: picked.number === 1 ? 'What it holds' : `What it changed after ${picked.number - 1}` },
              { value: 'now', label: 'From it to now' },
            ]}
            className="justify-self-start"
          />
          <DiffBody key={`${picked.checkpoint.id}:${against}`} org={org} env={env.id} checkpoint={picked.checkpoint.id} against={against} />
        </div>
      ) : null}
    </Dialog>
  );
}

function DiffBody({ org, env, checkpoint, against }: { org: string; env: string; checkpoint: string; against: Against }) {
  const diff = useCheckpointDiff(org, env, checkpoint, against);
  const [pickedPath, setPickedPath] = useState<string | null>(null);
  if (diff.isPending) return <Skeleton className="h-80 w-full rounded-lg" />;
  if (!diff.data) return <p className="text-14 text-muted">The diff could not be read. Nothing was changed.</p>;
  const files = diff.data.files;
  if (!files.length) return <p className="rounded-lg border border-line px-4 py-6 text-center text-14 text-muted">{against === 'now' ? 'Nothing changed since this checkpoint.' : 'This checkpoint changed nothing.'}</p>;
  const file = files.find((entry) => `${entry.repo}/${entry.path}` === pickedPath) ?? files[0];
  const added = files.reduce((sum, entry) => sum + entry.added, 0);
  const removed = files.reduce((sum, entry) => sum + entry.removed, 0);
  return (
    <div className="grid min-h-0 gap-4 md:grid-cols-[280px_minmax(0,1fr)]">
      <nav aria-label="Changed files" className="grid content-start gap-1">
        <p className="px-2 pb-1 font-mono text-code-caption text-muted tabular">
          {files.length} {files.length === 1 ? 'file' : 'files'} <span className="text-diff-add-mark">+{added}</span> <span className="text-diff-del-mark">{'−'}{removed}</span>
        </p>
        {files.map((entry) => {
          const id = `${entry.repo}/${entry.path}`;
          const active = entry === file;
          return (
            <button
              key={id}
              type="button"
              aria-current={active || undefined}
              onClick={() => setPickedPath(id)}
              className={cn('grid min-w-0 gap-px rounded-md px-2 py-2 text-left transition-colors duration-(--duration-fast) hover:bg-hover', active && 'bg-pressed')}
            >
              <span className="truncate font-mono text-code-caption text-fg" title={id}>
                {entry.path.split('/').pop()}
              </span>
              <span className="flex min-w-0 items-center gap-2 font-mono text-code-caption tabular">
                <span className="min-w-0 truncate text-faint">
                  {entry.repo}/{entry.path.split('/').slice(0, -1).join('/')}
                </span>
                <span className="ml-auto shrink-0 text-diff-add-mark">+{entry.added}</span>
                <span className="shrink-0 text-diff-del-mark">{'−'}{entry.removed}</span>
              </span>
            </button>
          );
        })}
      </nav>
      {file ? <FileDiff file={file} /> : null}
    </div>
  );
}

const STATUS_WORDS: Record<EnvDiffFile['status'], string> = { added: 'New file', modified: 'Changed', deleted: 'Deleted' };

function FileDiff({ file }: { file: EnvDiffFile }) {
  return (
    <section aria-label={`${file.repo}/${file.path}`} className="min-w-0 overflow-hidden rounded-lg border border-line">
      <header className="flex min-w-0 items-center gap-3 border-b border-line px-4 py-2">
        <span className="min-w-0 truncate font-mono text-code text-fg">{file.path}</span>
        <span className="ml-auto shrink-0 text-caption text-faint">{STATUS_WORDS[file.status]}</span>
      </header>
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
    </section>
  );
}
