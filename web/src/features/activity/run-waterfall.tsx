import { useState } from 'react';
import type { Run, RunStatus, RunStep } from '@/api/types';
import { StatusDot } from '@/components/ui/status';
import { clock, elapsed } from '@/lib/format';
import type { Tone } from '@/lib/labels';
import { useNow } from '@/lib/use-now';

const OPEN: ReadonlySet<RunStatus> = new Set<RunStatus>(['queued', 'running', 'waiting_approval']);

export const isOpenRun = (run: Pick<Run, 'status'>) => OPEN.has(run.status);

export interface Span {
  step: RunStep;
  start: number;
  end: number;
  /** Still going, so its end is the clock and not a recorded time. */
  live: boolean;
}

/**
 * A step's `at` is when it finished, so its bar starts where the step before it ended. A step
 * still going has only a start, so there `at` opens the bar. Clocks on a node can disagree, and
 * a bar never runs backwards: anything out of order collapses to zero length.
 */
export function spansOf(run: Run, now: number): { spans: Span[]; start: number; end: number } {
  const start = new Date(run.started_at).getTime();
  const finished = run.finished_at ? new Date(run.finished_at).getTime() : null;
  const stop = finished ?? now;
  const spans: Span[] = [];
  let cursor = start;
  for (const step of run.steps ?? []) {
    const at = Math.max(new Date(step.at).getTime(), cursor);
    if (OPEN.has(step.status)) {
      const end = Math.max(stop, at);
      spans.push({ step, start: at, end, live: finished === null });
      cursor = end;
    } else {
      spans.push({ step, start: cursor, end: at, live: false });
      cursor = at;
    }
  }
  const end = Math.max(finished ?? (isOpenRun(run) ? now : cursor), cursor);
  return { spans, start, end };
}

/**
 * Only an exception takes colour. A step that ended well is not "verified": that word is about
 * the run's proof, and it is said once, at the top.
 */
function look(status: RunStatus): { tone: Tone; color: string; word: string | null } {
  switch (status) {
    case 'failed':
      return { tone: 'bad', color: 'var(--mark-failed)', word: 'Failed' };
    case 'partial':
      return { tone: 'warn', color: 'var(--mark-partial)', word: 'Partial' };
    case 'cancelled':
      return { tone: 'idle', color: 'var(--fill-3)', word: 'Cancelled' };
    case 'waiting_approval':
      return { tone: 'warn', color: 'var(--mark-partial)', word: 'Waiting' };
    case 'queued':
    case 'running':
      return { tone: 'info', color: 'var(--foreground)', word: 'Running' };
    default:
      return { tone: 'neutral', color: 'var(--foreground-subtle)', word: null };
  }
}

const sameWords = (a: string, b: string) => a.replace(/[^a-z0-9]/gi, '').toLowerCase() === b.replace(/[^a-z0-9]/gi, '').toLowerCase();

function Row({ span, from, total, slowest, open, onToggle }: { span: Span; from: number; total: number; slowest: boolean; open: boolean; onToggle: () => void }) {
  const { step } = span;
  const { tone, color, word } = look(step.status);
  const length = span.end - span.start;
  const left = ((span.start - from) / total) * 100;
  const width = (length / total) * 100;
  return (
    <li>
      <button
        type="button"
        aria-expanded={open}
        onClick={onToggle}
        className="grid w-full gap-1 rounded-md px-2 py-2 text-left transition-colors duration-(--duration-fast) ease-standard hover:bg-hover"
      >
        <span className="flex items-baseline gap-2 text-secondary">
          <StatusDot tone={tone} pulse={span.live} className="translate-y-px" />
          <span className="min-w-0 flex-1 truncate">{step.summary}</span>
          {word ? <span className="text-caption text-muted">{word}</span> : slowest ? <span className="text-caption text-faint">Slowest</span> : null}
          <span className="mono tabular text-caption text-muted">{elapsed(length)}</span>
        </span>
        <span aria-hidden="true" className="relative block h-1.5 overflow-hidden rounded-full bg-hover">
          <span
            className="absolute inset-y-0 rounded-full"
            style={{ left: `${left}%`, width: `max(${width}%, 3px)`, backgroundColor: color }}
          />
        </span>
      </button>
      {open ? (
        <dl className="tabular grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 px-2 pt-1 pb-2 pl-6 text-caption text-muted">
          {sameWords(step.tool, step.summary) ? null : (
            <>
              <dt className="text-faint">Tool</dt>
              <dd className="mono min-w-0 break-all">{step.tool}</dd>
            </>
          )}
          <dt className="text-faint">Began</dt>
          <dd>+{elapsed(span.start - from)} into the run</dd>
          <dt className="text-faint">{span.live ? 'Still going' : 'Finished'}</dt>
          <dd>{span.live ? `${Math.round((length / total) * 100)}% of the run so far` : `${clock(step.at)}, ${Math.round((length / total) * 100)}% of the run`}</dd>
        </dl>
      ) : null}
    </li>
  );
}

/** Every step on one clock, the way a request trace reads. */
export function Waterfall({ run }: { run: Run }) {
  const live = isOpenRun(run) && !run.finished_at;
  const now = useNow(live);
  const [open, setOpen] = useState<string | null>(null);
  const { spans, start, end } = spansOf(run, now);
  const total = Math.max(end - start, 1);
  const longest = spans.length > 1 ? Math.max(...spans.map((span) => span.end - span.start)) : Infinity;
  return (
    <div>
      <div aria-hidden="true" className="mono tabular flex justify-between px-2 pb-1 text-caption text-faint">
        <span>0s</span>
        <span>{elapsed(total / 2)}</span>
        <span>{elapsed(total)}</span>
      </div>
      <ol className="grid gap-px" aria-label="Steps, in order">
        {spans.map((span) => (
          <Row
            key={span.step.id}
            span={span}
            from={start}
            total={total}
            slowest={span.end - span.start === longest}
            open={open === span.step.id}
            onToggle={() => setOpen(open === span.step.id ? null : span.step.id)}
          />
        ))}
      </ol>
    </div>
  );
}
