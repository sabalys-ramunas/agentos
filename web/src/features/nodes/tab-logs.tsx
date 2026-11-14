/**
 * A live tail of the node's logs. They stream from the node, redacted on the way through, and
 * the cloud keeps none of them. Reading is never disturbed: scrolled up or paused, the view
 * holds still and new lines wait behind a pill.
 */
import { ArrowDown, Pause, Play, Search, ShieldCheck, X } from 'lucide-react';
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { useNodeLogs } from '@/api/logs';
import type { SeqLine } from '@/api/logs';
import type { LogLevel, LogSource } from '@/api/types';
import { Segmented } from '@/components/charts/small';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Select } from '@/components/ui/select';
import { StatusDot } from '@/components/ui/status';
import { cn } from '@/lib/cn';
import { clockSeconds } from '@/lib/format';
import { Panel } from './parts';

const SHOW = 1_000;
const ROW = 20;
const RANK: Record<LogLevel, number> = { debug: 0, info: 1, warn: 2, error: 3 };
const SOURCES: { value: LogSource | 'all'; label: string }[] = [
  { value: 'all', label: 'All sources' },
  { value: 'agentosd', label: 'agentosd' },
  { value: 'scheduler', label: 'Scheduler' },
  { value: 'channel', label: 'Channel' },
  { value: 'lane', label: 'Accounts' },
  { value: 'service', label: 'Services' },
];
const LEVELS: { value: LogLevel; label: string }[] = [
  { value: 'debug', label: 'Debug' },
  { value: 'info', label: 'Info' },
  { value: 'warn', label: 'Warn' },
  { value: 'error', label: 'Error' },
];


function highlight(text: string, query: string): ReactNode {
  if (!query) return text;
  const lower = text.toLowerCase();
  const needle = query.toLowerCase();
  const parts: ReactNode[] = [];
  let from = 0;
  let index = lower.indexOf(needle);
  while (index >= 0 && parts.length < 40) {
    if (index > from) parts.push(text.slice(from, index));
    parts.push(
      <mark key={index} className="rounded-[2px] bg-fill-3 text-fg">
        {text.slice(index, index + needle.length)}
      </mark>,
    );
    from = index + needle.length;
    index = lower.indexOf(needle, from);
  }
  parts.push(text.slice(from));
  return parts;
}

function Line({ line, query }: { line: SeqLine; query: string }) {
  return (
    <div className="grid h-5 grid-cols-[64px_40px_72px_minmax(0,1fr)] items-center gap-3 px-4 whitespace-nowrap hover:bg-fill-1 max-sm:grid-cols-[56px_36px_minmax(0,1fr)] sm:px-6">
      <span className="text-faint">{clockSeconds(line.at)}</span>
      <span className={cn(line.level === 'error' ? 'text-red' : line.level === 'warn' ? 'text-amber' : 'text-faint')}>{line.level}</span>
      <span className="truncate text-muted max-sm:hidden">{line.source}</span>
      <span className="truncate text-fg">
        {line.lane ? <span className="text-faint">{line.lane} </span> : null}
        {highlight(line.message, query)}
      </span>
    </div>
  );
}

interface LogsProps {
  org: string;
  node: string;
  away: boolean;
  allowed: boolean;
  source: string | undefined;
  level: LogLevel;
  q: string;
  onFilters: (next: { source?: string; level?: LogLevel; q?: string }) => void;
}

export function LogsTab({ org, node, away, allowed, source, level, q, onFilters }: LogsProps) {
  const { lines, state } = useNodeLogs(org, node, allowed && !away);
  const [paused, setPaused] = useState(false);
  const [follow, setFollow] = useState(true);
  // The newest line in view while reading. Null follows the stream.
  const [held, setHeld] = useState<number | null>(null);
  const [query, setQuery] = useState(q);
  const box = useRef<HTMLDivElement>(null);

  // Back and Forward change the URL, and the box follows.
  useEffect(() => setQuery(q), [q]);

  // Typing filters at once. The URL catches up a moment later, so Back is not one step per key.
  useEffect(() => {
    const timer = setTimeout(() => {
      if (query !== q) onFilters({ q: query || undefined });
    }, 300);
    return () => clearTimeout(timer);
  }, [query, q, onFilters]);

  const frozen = paused || !follow;
  const lastSeq = lines[lines.length - 1]?.seq ?? 0;
  useEffect(() => {
    if (frozen && held === null) setHeld(lastSeq);
    if (!frozen && held !== null) setHeld(null);
  }, [frozen, held, lastSeq]);

  const matches = (line: SeqLine) =>
    RANK[line.level] >= RANK[level] &&
    (!source || source === 'all' || line.source === source) &&
    (!query || `${line.message} ${line.lane ?? ''} ${line.source}`.toLowerCase().includes(query.toLowerCase()));

  const visible = useMemo(() => {
    const cut = held ?? Number.POSITIVE_INFINITY;
    return lines.filter((line) => line.seq <= cut && matches(line));
    // eslint-disable-next-line react-hooks/exhaustive-deps -- matches reads only these
  }, [lines, held, level, source, query]);
  const shown = visible.slice(-SHOW);
  const waiting = held === null ? 0 : lines.filter((line) => line.seq > held && matches(line)).length;

  // Following: stay pinned to the bottom as lines arrive.
  useLayoutEffect(() => {
    const element = box.current;
    if (element && follow && !paused) element.scrollTop = element.scrollHeight;
  }, [shown.length, follow, paused, lastSeq]);

  const jump = () => {
    setPaused(false);
    setFollow(true);
    requestAnimationFrame(() => {
      const element = box.current;
      if (element) element.scrollTop = element.scrollHeight;
    });
  };

  if (!allowed) {
    return (
      <Card className="p-4 sm:p-6">
        <p className="text-14 font-medium">Only admins can read this machine's logs</p>
        <p className="mt-1 text-13 text-muted">Logs can hold more than a summary shows, so reading them needs the admin role.</p>
      </Card>
    );
  }
  if (away) {
    return (
      <Card className="p-4 sm:p-6">
        <p className="text-14 font-medium">
          <span className="mono">{node}</span> is away, so there is no log stream
        </p>
        <p className="mt-1 max-w-prose text-13 text-muted">
          Logs live on the machine and are never kept, so there is nothing older to show here. The tail starts again when the machine is back.
        </p>
      </Card>
    );
  }

  const status =
    state === 'refused'
      ? { tone: 'bad' as const, word: 'Not streaming' }
      : state === 'reconnecting'
        ? { tone: 'warn' as const, word: 'Reconnecting' }
        : state === 'connecting'
          ? { tone: 'neutral' as const, word: 'Connecting' }
          : paused
            ? { tone: 'idle' as const, word: 'Paused' }
            : { tone: 'ok' as const, word: 'Live' };

  return (
    <Panel
      title="Logs"
      description={
        <span className="inline-flex items-center gap-2">
          <ShieldCheck className="size-4 shrink-0" />
          Streamed from the machine and redacted on the way. Nothing is stored.
        </span>
      }
    >
      <Card className="overflow-hidden">
        <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-2 sm:px-6">
          <label className="relative flex min-w-48 flex-1 items-center sm:max-w-72">
            <Search className="pointer-events-none absolute left-2 size-4 text-faint" />
            <input
              type="search"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Escape') setQuery('');
              }}
              aria-label="Search the logs"
              placeholder="Search the logs"
              className="h-7 w-full rounded-md border border-line bg-transparent pr-7 pl-8 text-13 outline-none placeholder:text-faint hover:border-line-strong focus-visible:border-line-strong [&::-webkit-search-cancel-button]:hidden"
            />
            {query ? (
              <button type="button" aria-label="Clear the search" onClick={() => setQuery('')} className="absolute right-1 inline-flex size-5 items-center justify-center rounded-sm text-faint hover:text-fg">
                <X className="size-4" />
              </button>
            ) : null}
          </label>
          <Select size="sm" aria-label="Source" value={source ?? 'all'} onValueChange={(next) => onFilters({ source: next === 'all' ? undefined : next })} options={SOURCES} className="w-36" />
          <Segmented label="Lowest level shown" value={level} onChange={(next) => onFilters({ level: next })} options={LEVELS} />
          <span className="ml-auto inline-flex items-center gap-3">
            <span className="inline-flex w-28 items-center justify-end gap-2 text-12 text-muted" aria-live="polite">
              <StatusDot tone={status.tone} pulse={status.word === 'Live'} />
              {status.word}
            </span>
            <Button size="sm" variant="secondary" icon={paused ? <Play className="size-4" /> : <Pause className="size-4" />} onClick={() => (paused ? jump() : setPaused(true))} className="w-24">
              {paused ? 'Resume' : 'Pause'}
            </Button>
          </span>
        </div>
        <div className="relative">
          {/* eslint-disable-next-line jsx-a11y/no-noninteractive-element-interactions -- End and Space drive the tail */}
          <div
            ref={box}
            role="log"
            aria-label={`Logs from ${node}`}
            aria-live="off"
            // A scrollable region has to be reachable by keyboard to be read, and End and Space work there.
            // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
            tabIndex={0}
            onScroll={(event) => {
              const element = event.currentTarget;
              const atBottom = element.scrollHeight - element.scrollTop - element.clientHeight < ROW * 1.5;
              if (atBottom !== follow) setFollow(atBottom);
            }}
            onKeyDown={(event) => {
              if (event.key === 'End') jump();
              else if (event.key === ' ') {
                event.preventDefault();
                if (paused) jump();
                else setPaused(true);
              }
            }}
            className="focus-inset mono h-[min(60vh,560px)] min-h-80 overflow-y-auto py-2 text-12 leading-5 [overflow-anchor:none]"
          >
            {visible.length > SHOW ? (
              <p className="px-4 pb-2 text-faint sm:px-6">Showing the latest {SHOW.toLocaleString('en')} of {visible.length.toLocaleString('en')} matching lines. Search to narrow it.</p>
            ) : null}
            {shown.length ? (
              shown.map((line) => <Line key={line.seq} line={line} query={query} />)
            ) : (
              <p className="px-4 py-8 text-center font-sans text-13 text-muted sm:px-6">
                {state === 'refused'
                  ? 'This machine did not open its log stream. It may be on an older version that does not send logs yet.'
                  : lines.length
                    ? 'No line matches these filters yet. New lines appear as they arrive.'
                    : 'Waiting for the first line from this machine.'}
              </p>
            )}
          </div>
          {waiting > 0 || (!follow && !paused) ? (
            <div className="pointer-events-none absolute inset-x-0 bottom-3 flex justify-center">
              <button
                type="button"
                onClick={jump}
                className="pointer-events-auto inline-flex h-7 animate-rise items-center gap-2 rounded-full border border-line-strong bg-raised px-3 text-13 text-fg shadow-popover transition-colors duration-(--duration-fast) ease-standard hover:bg-fill-1"
              >
                <ArrowDown className="size-4" />
                <span className="tabular">{waiting ? `${waiting.toLocaleString('en')} new` : 'Jump to latest'}</span>
              </button>
            </div>
          ) : null}
        </div>
      </Card>
    </Panel>
  );
}
