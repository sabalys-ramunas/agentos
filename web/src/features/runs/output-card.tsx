/** The Output card of a run: the windowed output, its filter, the stream, wrap, follow and the jump to a failure. */
import { useNavigate, useSearch } from '@tanstack/react-router';
import { ArrowDownToLine, CircleAlert, Search, WrapText } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { Job } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Card, CardHeader } from '@/components/ui/card';
import { Input } from '@/components/ui/field';
import { Kbd } from '@/components/ui/misc';
import { SegmentedControl } from '@/components/ui/tabs';
import { useKeys } from '@/lib/keyboard';
import { StatusDot } from '@/components/ui/status';
import { parseFailures, splitLines } from './failures';
import type { Failure, Stream } from './failures';
import { OutputView, filterItems, isRow, outputItems } from './output';
import { bytes, logsCommand } from './words';

export interface RunText {
  stdout: string[];
  stderr: string[];
  /** The failed tests read from the output; null when the run has no test summary to read them by. */
  failures: Failure[] | null;
  /** Where to jump for "the first failure": the first parsed failure, else the first failed line. */
  first?: { stream: Stream; line: number };
}

/** The output cut into lines once, and the failures read from it, for every card on the page. */
export function useRunText(stdout: string, stderr: string, job: Job): RunText {
  const runner = job.tests?.runner;
  const hasFailures = !!job.tests && job.tests.failed > 0;
  return useMemo(() => {
    const out = splitLines(stdout);
    const err = splitLines(stderr);
    const failures = runner ? parseFailures(runner, out, err) : null;
    let first = failures?.[0]?.at;
    if (!first) {
      // Without a summary only a run that ended badly has a "first failure" to look for.
      const items = hasFailures || !runner ? outputItems(out, err, 'both') : [];
      const bad = items.find((item) => isRow(item) && item.bad);
      if (bad && isRow(bad)) first = { stream: bad.stream, line: bad.no - 1 };
    }
    return { stdout: out, stderr: err, failures, first };
  }, [stdout, stderr, runner, hasFailures]);
}

interface OutputCardProps {
  job: Job;
  text: RunText;
  done: boolean;
  trimmed: boolean;
  jump?: { stream: Stream; no: number; nonce: number };
  onJump: (stream: Stream, line: number) => void;
}

const STREAMS = [
  { value: 'both', label: 'Both' },
  { value: 'stdout', label: 'Output' },
  { value: 'stderr', label: 'Errors' },
] as const;

export function OutputCard({ job, text, done, trimmed, jump, onJump }: OutputCardProps) {
  const search = useSearch({ from: '/$org/runs/$id' });
  const navigate = useNavigate({ from: '/$org/runs/$id' });
  const running = job.state === 'running';
  const stream = search.stream ?? 'both';
  const wrap = search.wrap ?? false;
  const [query, setQuery] = useState(search.q ?? '');
  const [follow, setFollow] = useState(search.follow ?? (running && search.line === undefined));
  const filterRef = useRef<HTMLInputElement>(null);

  // Typing filters at once; the address follows, so a filtered view can be linked.
  useEffect(() => setQuery(search.q ?? ''), [search.q]);
  const setSearch = (patch: Partial<typeof search>) => void navigate({ search: (prev) => ({ ...prev, ...patch }), replace: true });

  const items = useMemo(() => outputItems(text.stdout, text.stderr, stream), [text.stdout, text.stderr, stream]);
  const shown = useMemo(() => filterItems(items, query), [items, query]);
  const lineCount = text.stdout.length + text.stderr.length;

  // A link to a line (`?line=12`) lands on it once.
  const [linked] = useState(() =>
    search.line !== undefined ? { stream: (search.stream === 'stderr' ? 'stderr' : 'stdout') as Stream, no: search.line, nonce: 0 } : undefined,
  );
  const target = jump ?? linked;

  const onFollowChange = useCallback((atEnd: boolean) => setFollow((current) => (current === atEnd ? current : atEnd)), []);

  useKeys({
    '/': () => filterRef.current?.focus(),
    w: () => setSearch({ wrap: wrap ? undefined : true }),
    f: () => setFollow((value) => !value),
  });

  const sentence = running
    ? `Following ${job.node} as it runs`
    : done
      ? `Ended${job.exit_code !== null ? ` with exit code ${job.exit_code}` : ''}`
      : 'Reading the rest of the output';

  const toggle = 'aria-pressed:bg-pressed aria-pressed:text-fg';

  return (
    <Card className="overflow-hidden">
      <CardHeader
        title="Output"
        meta={<span className="max-md:hidden">What the machine printed</span>}
        action={
          <SegmentedControl
            label="Which stream"
            value={stream}
            onValueChange={(value) => setSearch({ stream: value === 'both' ? undefined : value })}
            options={STREAMS.map((option) => ({ ...option }))}
          />
        }
      />
      <div className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2">
        <div className="relative min-w-0 flex-1 basis-48 md:max-w-80">
          <label htmlFor="run-output-filter" className="sr-only">
            Filter the output
          </label>
          <Search aria-hidden="true" className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-faint" />
          <Input
            id="run-output-filter"
            ref={filterRef}
            aria-keyshortcuts="/"
            type="search"
            value={query}
            placeholder="Filter lines"
            className="pr-9 pl-9"
            onChange={(event) => {
              setQuery(event.target.value);
              setSearch({ q: event.target.value || undefined });
            }}
            onKeyDown={(event) => {
              if (event.key === 'Escape' && query) {
                event.preventDefault();
                setQuery('');
                setSearch({ q: undefined });
              }
            }}
          />
          <Kbd aria-hidden="true" className="absolute top-1/2 right-3 -translate-y-1/2 max-md:hidden">/</Kbd>
        </div>
        <Button
          size="sm"
          variant="ghost"
          className="ml-auto"
          disabled={!text.first}
          icon={<CircleAlert className="size-4" />}
          onClick={() => {
            if (!text.first) return;
            setFollow(false);
            onJump(text.first.stream, text.first.line);
          }}
        >
          First failure
        </Button>
      </div>
      {trimmed ? (
        <p className="border-b border-line px-5 py-2 text-secondary text-muted">
          The start is not shown: this page keeps the newest 2 MB of each stream. The whole log is on {job.node}:{' '}
          <span className="font-mono text-code">{logsCommand(job)}</span>
        </p>
      ) : null}
      {job.dropped_bytes > 0 ? (
        <p className="border-b border-line px-5 py-2 text-secondary text-muted">
          {job.node} kept the first part of this output and only counted the last {bytes(job.dropped_bytes)}, so the end is not shown.
        </p>
      ) : null}
      <div className="bg-terminal">
        <OutputView
          items={shown}
          label={`Output of ${job.command}`}
          wrap={wrap}
          follow={follow}
          onFollowChange={onFollowChange}
          query={query}
          target={target}
          className="max-h-[min(70vh,720px)] min-h-40"
          empty={
            <p className="px-4 text-terminal-dim">
              {query
                ? `No line has “${query}”${running ? ' yet' : ''}.`
                : running
                  ? 'Nothing printed yet. Lines show here as the command writes them.'
                  : 'The command printed nothing.'}
            </p>
          }
        />
      </div>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-t border-line py-2 pr-3 pl-5 text-secondary text-muted">
        <span className="inline-flex min-w-0 items-center gap-2">
          {running ? <StatusDot tone="live" /> : null}
          {sentence}
        </span>
        <span className="tabular">
          {query ? `${shown.filter(isRow).length.toLocaleString('en')} of ` : ''}
          {lineCount.toLocaleString('en')} {lineCount === 1 ? 'line' : 'lines'}
        </span>
        <span className="tabular">{bytes(Math.max(0, job.stdout_bytes + job.stderr_bytes - job.dropped_bytes))} kept on {job.node}</span>
        <span className="ml-auto flex items-center gap-1">
          <Button
            size="sm"
            variant="ghost"
            aria-pressed={wrap}
            className={toggle}
            icon={<WrapText className="size-4" />}
            onClick={() => setSearch({ wrap: wrap ? undefined : true })}
            shortcut="W"
          >
            Wrap
          </Button>
          <Button
            size="sm"
            variant="ghost"
            aria-pressed={follow}
            className={toggle}
            icon={<ArrowDownToLine className="size-4" />}
            onClick={() => setFollow((value) => !value)}
            shortcut="F"
          >
            {follow ? 'Following' : 'Follow'}
          </Button>
        </span>
      </div>
    </Card>
  );
}
