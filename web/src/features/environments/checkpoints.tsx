/**
 * Checkpoints: the agent's own undo points in an environment (git's, on the machine), as a line of work.
 * The working copy on top with what changed since the checkpoint it builds on, then each checkpoint newest
 * first with its note (the agent's words, shown as text), when, what it held per repository and what
 * changed since. When the agent went back to one, the checkpoints it went back past are drawn on a dashed
 * side line that ends, and the line goes on from the one it went back to. Each opens its diff from it to
 * now. Only the agent goes back; nothing here changes a file.
 */
import { Undo2 } from 'lucide-react';
import { useState } from 'react';
import type { EnvCheckpoint, EnvDetail } from '@/api/types';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardHeader } from '@/components/ui/card';
import { cn } from '@/lib/cn';
import { dateTime, plural, timeAgo } from '@/lib/format';
import { CheckpointDiffDialog } from './checkpoint-diff';
import { checkpointRows, noteOf, repoDir, sinceOf } from './checkpoint-words';
import type { Change, CheckpointRow } from './checkpoint-words';

const MAIN = 'left-[11px]';
const SIDE = 'left-[31px]';

export function CheckpointsCard({ org, env, now }: { org: string; env: EnvDetail; now: number }) {
  const checkpoints = env.checkpoints ?? [];
  const [picked, setPicked] = useState<{ checkpoint: EnvCheckpoint; number: number } | null>(null);
  if (!checkpoints.length) return null;
  const rows = checkpointRows(checkpoints);
  const restore = rows.find((row) => row.kind === 'restore');
  return (
    <Card className="overflow-hidden">
      <CardHeader
        title="Checkpoints"
        meta={
          <>
            {plural(checkpoints.length, 'checkpoint')}
            {restore?.kind === 'restore' ? ` · went back to ${restore.to} ${timeAgo(restore.at, now).toLowerCase()}` : null}
          </>
        }
      />
      <ol aria-label="Checkpoints, newest first" className="px-5 py-3">
        {rows.map((row, index) => (
          <Row key={row.kind === 'checkpoint' ? row.checkpoint.id : row.kind} row={row} first={index === 0} last={index === rows.length - 1} sideTop={!isSide(rows[index - 1])} now={now} onOpen={setPicked} />
        ))}
      </ol>
      <p className="border-t border-line px-5 py-3 text-caption text-faint">Only the agent goes back to a checkpoint. A checkpoint is kept with the environment, and goes when it is closed for good.</p>
      <CheckpointDiffDialog org={org} env={env} picked={picked} onClose={() => setPicked(null)} />
    </Card>
  );
}

const isSide = (row: CheckpointRow | undefined) => row?.kind === 'checkpoint' && row.side;

interface RowProps {
  row: CheckpointRow;
  first: boolean;
  last: boolean;
  /** The newest checkpoint on the side line, where that line ends. */
  sideTop: boolean;
  now: number;
  onOpen: (picked: { checkpoint: EnvCheckpoint; number: number }) => void;
}

/** The two lines of the gutter: the main one always, the side one beside the checkpoints that went back. */
function Gutter({ row, first, last, sideTop }: Omit<RowProps, 'now' | 'onOpen'>) {
  const side = row.kind === 'checkpoint' && row.side;
  const forks = row.kind === 'checkpoint' && row.forks;
  return (
    <span aria-hidden="true" className="relative w-11 shrink-0 self-stretch">
      <span className={cn('absolute w-px bg-line-strong', MAIN, first ? 'top-5' : 'top-0', last ? 'h-5' : 'bottom-0')} />
      {side ? <span className={cn('absolute w-0 border-l border-dashed border-faint', SIDE, sideTop ? 'top-5' : 'top-0', 'bottom-0')} /> : null}
      {forks ? (
        <svg className="absolute top-0 left-[11px] overflow-visible text-faint" width="21" height="22" viewBox="0 0 21 22">
          <path d="M0 22 C 14 22 20 16 20 0" fill="none" stroke="currentColor" strokeWidth="1" strokeDasharray="3 3" />
        </svg>
      ) : null}
      <Dot row={row} />
    </span>
  );
}

function Dot({ row }: { row: CheckpointRow }) {
  if (row.kind === 'restore') {
    return (
      <span className="absolute top-3 left-[3px] flex size-[18px] items-center justify-center rounded-full border border-line-strong bg-card">
        <Undo2 className="size-3 text-fg" />
      </span>
    );
  }
  if (row.kind === 'now') return <span className="absolute top-4 left-[6px] size-3 rounded-full border-2 border-fg bg-card" />;
  return (
    <span
      className={cn(
        'absolute top-4 size-3 rounded-full',
        row.side ? 'left-[26px] border border-faint bg-card' : 'left-[6px] bg-fg',
        row.current && 'outline-2 outline-offset-2 outline-fg',
      )}
    />
  );
}

function Changes({ change, label }: { change: Change | null; label?: string }) {
  if (!change) return <span className="text-caption text-faint">{label ? `${label}: ` : ''}the machine did not say</span>;
  if (!change.files) return <span className="text-caption text-faint">{label ? `${label}: ` : ''}no changes</span>;
  return (
    <span className="inline-flex items-center gap-2 font-mono text-code-caption tabular">
      {label ? <span className="font-sans text-caption text-faint">{label}</span> : null}
      <span className="text-muted">{plural(change.files, 'file')}</span>
      <span className="text-diff-add-mark">+{change.added}</span>
      <span className="text-diff-del-mark">{'−'}{change.removed}</span>
    </span>
  );
}

function Row({ row, first, last, sideTop, now, onOpen }: RowProps) {
  if (row.kind === 'now') {
    return (
      <li className="flex min-h-14 gap-2">
        <Gutter row={row} first={first} last={last} sideTop={sideTop} />
        <div className="grid min-w-0 flex-1 content-start gap-1 py-3">
          <span className="text-14 font-medium text-fg">The working copy now</span>
          <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <Changes change={row.since} label={row.base ? `Since ${row.base.number}` : undefined} />
          </span>
        </div>
      </li>
    );
  }
  if (row.kind === 'restore') {
    return (
      <li className="flex min-h-12 gap-2">
        <Gutter row={row} first={first} last={last} sideTop={sideTop} />
        <p className="flex min-w-0 flex-1 flex-wrap items-baseline gap-x-2 py-3 text-secondary">
          <span className="font-medium text-fg">Went back to {row.to}</span>
          <span className="text-muted">The work after it moved to the side line, kept in a checkpoint first.</span>
          <time dateTime={row.at} title={dateTime(row.at)} className="text-caption text-faint">
            {timeAgo(row.at, now)}
          </time>
        </p>
      </li>
    );
  }
  const { checkpoint, number, side } = row;
  return (
    <li className="group flex gap-2">
      <Gutter row={row} first={first} last={last} sideTop={sideTop} />
      <div className={cn('grid min-w-0 flex-1 gap-2 rounded-lg py-3 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-start', side && 'pl-5')}>
        <div className="grid min-w-0 gap-1">
          <span className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
            <span className={cn('font-mono text-code-caption tabular', side ? 'text-faint' : 'text-muted')}>{number}</span>
            <span className={cn('min-w-0 text-14 [overflow-wrap:anywhere]', side ? 'text-muted' : 'font-medium text-fg', !checkpoint.note && 'text-faint')}>{noteOf(checkpoint)}</span>
            {row.forks ? <Badge tone="accent">Went back here</Badge> : null}
            {checkpoint.auto ? <Badge>Made by agentos</Badge> : null}
          </span>
          <span className="flex flex-wrap items-center gap-x-4 gap-y-1">
            {checkpoint.at ? (
              <time dateTime={checkpoint.at} title={dateTime(checkpoint.at)} className="text-caption text-faint">
                {timeAgo(checkpoint.at, now)}
              </time>
            ) : null}
            {checkpoint.repos.map((repo) => (
              <span key={repo.dir} className="inline-flex items-center gap-2" title={repo.head ? `${repo.dir}, on ${repo.head}` : repo.dir}>
                <span className="font-mono text-code-caption text-faint">{repoDir(repo.dir)}</span>
                <span className="font-mono text-code-caption text-muted tabular">{repo.files ? plural(repo.files, 'file') : 'clean'}</span>
              </span>
            ))}
            {side ? null : <Changes change={sinceOf(checkpoint)} label="Since then" />}
          </span>
        </div>
        <Button
          variant="ghost"
          size="sm"
          className="justify-self-start sm:justify-self-end"
          aria-label={`Show what changed since checkpoint ${number}`}
          onClick={() => onOpen({ checkpoint, number })}
        >
          Changes
        </Button>
      </div>
    </li>
  );
}
