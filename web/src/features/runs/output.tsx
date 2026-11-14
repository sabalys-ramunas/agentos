/**
 * A run's output, drawn like the Terminal primitive (`.ui-terminal`, dark in both themes, ANSI colours
 * as text spans) but windowed: only the lines near the scroll position are in the page, so 2 MB of
 * output scrolls, filters and follows as fast as 20 lines. Lines are cut into chunks; a chunk out of
 * view is a spacer of its measured (or estimated) height.
 */
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { parseAnsi } from '@/components/ui/terminal';
import type { Segment } from '@/components/ui/terminal';
import { cn } from '@/lib/cn';
import { isFailedLine, plain } from './failures';
import type { Stream } from './failures';

export interface OutputRow {
  stream: Stream;
  /** 1-based, within its stream. */
  no: number;
  text: string;
  bad: boolean;
}

export type OutputItem = OutputRow | { divider: Stream; count: number };

export const isRow = (item: OutputItem): item is OutputRow => !('divider' in item);

/** The rows to draw, from both streams' lines: stdout, then stderr after a divider when both show. */
export function outputItems(stdout: string[], stderr: string[], stream: 'stdout' | 'stderr' | 'both'): OutputItem[] {
  const rows = (lines: string[], name: Stream): OutputRow[] => lines.map((text, index) => ({ stream: name, no: index + 1, text, bad: isFailedLine(text) }));
  if (stream === 'stdout') return rows(stdout, 'stdout');
  if (stream === 'stderr') return rows(stderr, 'stderr');
  const out: OutputItem[] = rows(stdout, 'stdout');
  if (stderr.length) {
    if (stdout.length) out.push({ divider: 'stderr', count: stderr.length });
    out.push(...rows(stderr, 'stderr'));
  }
  return out;
}

/** Only the rows with this text (colour codes ignored, any case). Dividers stay when rows follow them. */
export function filterItems(items: OutputItem[], query: string): OutputItem[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return items;
  const out: OutputItem[] = [];
  let divider: OutputItem | undefined;
  for (const item of items) {
    if (!isRow(item)) {
      divider = item;
      continue;
    }
    if (!plain(item.text).toLowerCase().includes(needle)) continue;
    if (divider) {
      out.push(divider);
      divider = undefined;
    }
    out.push(item);
  }
  return out;
}

const CHUNK = 100;
/** The code role's line height (`--type-code-line-height`): the estimate before a chunk is measured. */
const LINE = 20;

function marked(segments: Segment[], needle: string): ReactNode[] {
  return segments.map((seg, index) => {
    const className = cn(seg.color && ANSI[seg.color], seg.bold && 'font-semibold');
    if (!needle) return <span key={index} className={className}>{seg.text}</span>;
    const parts: ReactNode[] = [];
    const lower = seg.text.toLowerCase();
    let from = 0;
    for (let at = lower.indexOf(needle); at !== -1; at = lower.indexOf(needle, from)) {
      if (at > from) parts.push(seg.text.slice(from, at));
      parts.push(
        <mark key={at} className="rounded-xs bg-ansi-yellow/35 text-inherit">
          {seg.text.slice(at, at + needle.length)}
        </mark>,
      );
      from = at + needle.length;
    }
    if (from < seg.text.length) parts.push(seg.text.slice(from));
    return <span key={index} className={className}>{parts}</span>;
  });
}

const ANSI: Record<NonNullable<Segment['color']>, string> = {
  red: 'text-ansi-red',
  green: 'text-ansi-green',
  yellow: 'text-ansi-yellow',
  blue: 'text-ansi-blue',
  magenta: 'text-ansi-magenta',
  cyan: 'text-ansi-cyan',
  dim: 'text-terminal-dim',
};

export const rowKey = (row: Pick<OutputRow, 'stream' | 'no'>) => `${row.stream}:${row.no}`;

export interface OutputViewProps {
  items: OutputItem[];
  label: string;
  wrap: boolean;
  follow: boolean;
  /** The reader scrolled away from the end (false) or back to it (true). */
  onFollowChange: (follow: boolean) => void;
  /** Words to mark in each line. */
  query?: string;
  /** The row to bring into view and mark; `nonce` changes to jump again to the same row. */
  target?: { stream: Stream; no: number; nonce: number };
  /** Shown when there are no rows. */
  empty?: ReactNode;
  className?: string;
}

export function OutputView({ items, label, wrap, follow, onFollowChange, query = '', target, empty, className }: OutputViewProps) {
  const box = useRef<HTMLDivElement>(null);
  const heights = useRef(new Map<number, number>());
  const chunkEls = useRef(new Map<number, HTMLDivElement>());
  const [view, setView] = useState({ top: 0, height: 600 });
  const [, setMeasured] = useState(0);
  const needle = query.trim().toLowerCase();
  const chunks = Math.ceil(items.length / CHUNK);

  // Widths and wrapping change every chunk's height: measure again from scratch.
  useLayoutEffect(() => {
    heights.current.clear();
  }, [wrap, items]);

  const heightOf = useCallback((chunk: number) => heights.current.get(chunk) ?? Math.min(CHUNK, items.length - chunk * CHUNK) * LINE, [items.length]);

  const offsets: number[] = [];
  let total = 0;
  for (let chunk = 0; chunk < chunks; chunk += 1) {
    offsets.push(total);
    total += heightOf(chunk);
  }

  const overscan = Math.max(view.height, 400);
  let first = 0;
  while (first < chunks - 1 && offsets[first]! + heightOf(first) < view.top - overscan) first += 1;
  let last = first;
  while (last < chunks - 1 && offsets[last + 1]! < view.top + view.height + overscan) last += 1;

  const read = useCallback(() => {
    const el = box.current;
    if (!el) return;
    setView((current) => {
      const next = { top: el.scrollTop, height: el.clientHeight || 600 };
      return current.top === next.top && current.height === next.height ? current : next;
    });
  }, []);

  // Measure the chunks in the page; a change draws again with the real heights. It runs after every draw
  // on purpose, and sets state only when a height really changed, so it settles.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useLayoutEffect(() => {
    let changed = false;
    for (const [chunk, el] of chunkEls.current) {
      const height = el.offsetHeight;
      if (height > 0 && Math.abs((heights.current.get(chunk) ?? -1) - height) > 0.5) {
        heights.current.set(chunk, height);
        changed = true;
      }
    }
    if (changed) setMeasured((value) => value + 1);
  });

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const onScroll = () => {
      read();
      const atEnd = el.scrollHeight - el.scrollTop - el.clientHeight < 8;
      onFollowChange(atEnd);
    };
    el.addEventListener('scroll', onScroll, { passive: true });
    const observer = typeof ResizeObserver === 'undefined' ? undefined : new ResizeObserver(read);
    observer?.observe(el);
    read();
    return () => {
      el.removeEventListener('scroll', onScroll);
      observer?.disconnect();
    };
  }, [read, onFollowChange]);

  // Follow: the newest line stays in view as lines arrive.
  useLayoutEffect(() => {
    const el = box.current;
    if (follow && el) {
      el.scrollTop = el.scrollHeight;
      read();
    }
  }, [follow, items, total, read]);

  // A jump: scroll to the estimate at once, then to the drawn line once it is in the page.
  const [pendingJump, setPendingJump] = useState<string | null>(null);
  useLayoutEffect(() => {
    if (!target) return;
    const index = items.findIndex((item) => isRow(item) && item.stream === target.stream && item.no === target.no);
    const el = box.current;
    if (index < 0 || !el) return;
    const chunk = Math.floor(index / CHUNK);
    el.scrollTop = Math.max(0, (offsets[chunk] ?? 0) + (index % CHUNK) * LINE - el.clientHeight / 3);
    read();
    setPendingJump(rowKey(target));
    // `offsets` is read once for the jump; following it would jump again on every measure.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target?.stream, target?.no, target?.nonce, items]);
  // After every draw until the picked line is in the page; it clears itself once it is.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useLayoutEffect(() => {
    if (!pendingJump) return;
    const line = box.current?.querySelector<HTMLElement>(`[data-row="${pendingJump}"]`);
    if (!line) return;
    line.scrollIntoView?.({ block: 'center' });
    setPendingJump(null);
  });

  const picked = target ? rowKey(target) : undefined;
  const drawn: ReactNode[] = [];
  if (chunks) {
    drawn.push(<div key="top" style={{ height: offsets[first] }} aria-hidden="true" />);
    for (let chunk = first; chunk <= last; chunk += 1) {
      const slice = items.slice(chunk * CHUNK, (chunk + 1) * CHUNK);
      drawn.push(
        <div
          key={chunk}
          ref={(el) => {
            if (el) chunkEls.current.set(chunk, el);
            else chunkEls.current.delete(chunk);
          }}
        >
          {slice.map((item) => {
            if (!isRow(item)) {
              return (
                <div key={`div-${item.divider}`} data-line="" className="mt-2 border-t border-white/10 pt-2">
                  <span data-line-no="" />
                  <span data-line-text="" className="text-terminal-dim">
                    Errors (stderr), {item.count} {item.count === 1 ? 'line' : 'lines'}
                  </span>
                </div>
              );
            }
            const key = rowKey(item);
            return (
              <div key={key} data-line="" data-row={key} data-highlight={key === picked ? 'warn' : item.bad ? 'bad' : undefined}>
                <span data-line-no="">{item.no}</span>
                <span data-line-text="" title={wrap ? undefined : plain(item.text) || undefined}>
                  {item.text ? marked(parseAnsi(item.text), needle) : ' '}
                </span>
              </div>
            );
          })}
        </div>,
      );
    }
    drawn.push(<div key="bottom" style={{ height: Math.max(0, total - (offsets[last] ?? 0) - heightOf(last)) }} aria-hidden="true" />);
  }

  return (
    <div
      ref={box}
      role="log"
      aria-label={label}
      aria-live="off"
      // A log that scrolls needs to be reachable from the keyboard.
      // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
      tabIndex={0}
      data-wrap={wrap ? '' : undefined}
      className={cn('ui-terminal overflow-auto py-3 focus-inset', className)}
    >
      {chunks ? drawn : empty}
    </div>
  );
}
