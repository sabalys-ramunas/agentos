/**
 * The raw log, for Technical details: every trace including steps, with status, cost and who
 * started it by id, under the runs-by-outcome chart. The everyday view is the story feed.
 */
import { useMemo } from 'react';
import { useActivityMetrics } from '@/api/hooks';
import { isNotServed } from '@/api/http';
import type { ActivityRange, TraceEvent } from '@/api/types';
import { StackedBars } from '@/components/charts/stacked-bars';
import type { BarSeries } from '@/components/charts/stacked-bars';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Status, StatusDot } from '@/components/ui/status';
import { Skeleton } from '@/components/ui/states';
import { Table, Td, Th, Tr } from '@/components/ui/table';
import { cn } from '@/lib/cn';
import { dateTime, timeAgo, usd } from '@/lib/format';
import { effectLabel, runStatus } from '@/lib/labels';
import { usePrincipalName } from '@/lib/use-principal';

const RUN_SERIES: BarSeries[] = [
  { key: 'succeeded_verified', label: 'Verified', color: 'var(--mark-verified)' },
  { key: 'succeeded_unverified', label: 'Unverified', color: 'var(--mark-unverified)' },
  { key: 'partial', label: 'Partial', color: 'var(--mark-partial)' },
  { key: 'failed', label: 'Failed', color: 'var(--mark-failed)' },
];

interface ChartProps {
  org: string;
  range: ActivityRange;
  scope: { workspace?: string; lane?: string };
  from?: string;
  to?: string;
  /** A dragged window on the time axis, or null to clear it. */
  onWindow: (window: { from?: string; to?: string }) => void;
}

/** Runs by outcome over time. Dragging across it narrows the log to that window. */
export function RunsChart({ org, range, scope, from, to, onWindow }: ChartProps) {
  const metrics = useActivityMetrics(org, range, scope);
  const buckets = useMemo(() => metrics.data?.buckets ?? [], [metrics.data]);
  const seconds = metrics.data?.bucket_seconds ?? 3_600;
  const selected = useMemo(() => {
    if (!from || !to) return null;
    const first = buckets.findIndex((bucket) => bucket.at >= from);
    const end = new Date(to).getTime() - seconds * 1000;
    let last = -1;
    buckets.forEach((bucket, index) => {
      if (new Date(bucket.at).getTime() <= end) last = index;
    });
    return first < 0 || last < 0 ? null : { from: first, to: last };
  }, [buckets, from, to, seconds]);

  const select = (next: { from: number; to: number } | null) => {
    const first = next ? buckets[next.from] : undefined;
    const last = next ? buckets[next.to] : undefined;
    onWindow({ from: first?.at, to: last ? new Date(new Date(last.at).getTime() + seconds * 1000).toISOString() : undefined });
  };

  if (isNotServed(metrics.error)) return null;
  return (
    <Card className="mb-4 p-4">
      {metrics.data ? (
        <div className={cn('transition-opacity duration-(--duration-base) ease-standard', metrics.isPlaceholderData && 'opacity-50')}>
          <StackedBars
            label={`Runs by outcome, last ${range}`}
            series={RUN_SERIES}
            bucketSeconds={seconds}
            selected={selected}
            onSelect={select}
            buckets={buckets.map((bucket) => ({ at: bucket.at, values: bucket.runs }))}
          />
        </div>
      ) : (
        <Skeleton className="h-40 rounded-md" />
      )}
      {from && to ? (
        <div className="mt-2 flex items-center gap-2 text-secondary text-muted">
          <span className="tabular">
            {dateTime(from)} to {dateTime(to)}
          </span>
          <Button size="sm" variant="ghost" onClick={() => select(null)}>
            Clear range
          </Button>
        </div>
      ) : null}
    </Card>
  );
}

export function ActivityTable({ org, rows, onOpen }: { org: string; rows: TraceEvent[]; onOpen: (runId: string) => void }) {
  const nameOf = usePrincipalName(org);
  return (
    <Table aria-label="Activity log">
      <thead className="hidden md:table-header-group">
        <tr>
          <Th>Time</Th>
          <Th>Status</Th>
          <Th className="w-full">Summary</Th>
          <Th className="hidden xl:table-cell">Started by</Th>
          <Th className="text-right">Cost</Th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <Tr key={row.id} className="hover:bg-hover">
            <Td className="tabular hidden text-secondary whitespace-nowrap text-faint md:table-cell" title={dateTime(row.at)}>
              {timeAgo(row.at)}
            </Td>
            <Td className="hidden md:table-cell">
              <Status status={runStatus[row.status]} />
            </Td>
            <Td className="w-full max-w-0 pl-4 md:pl-4">
              <div className="flex items-baseline gap-2">
                <StatusDot tone={runStatus[row.status].tone} className="translate-y-px md:hidden" />
                <div className="min-w-0 flex-1">
                  {row.run_id ? (
                    <button
                      type="button"
                      aria-label={`View run: ${row.summary}`}
                      className="block max-w-full truncate rounded-sm text-left text-body hover:underline"
                      onClick={() => row.run_id && onOpen(row.run_id)}
                    >
                      {row.summary}
                    </button>
                  ) : (
                    <p className="truncate text-body">{row.summary}</p>
                  )}
                  <p className="truncate text-caption text-faint">
                    <span className="mono">{row.lane}</span>
                    <span className="mono"> · {row.kind} · {row.tool}</span>
                    <span className="hidden md:inline"> · {effectLabel[row.effect]}</span>
                    <span className="md:hidden"> · {runStatus[row.status].label} · {timeAgo(row.at)}</span>
                  </p>
                </div>
              </div>
            </Td>
            <Td className="hidden text-secondary whitespace-nowrap text-muted xl:table-cell">{nameOf(row.principal)}</Td>
            <Td className="mono tabular pr-4 pl-0 text-right text-secondary text-muted md:pr-6 md:pl-4">{usd(row.cost.usd)}</Td>
          </Tr>
        ))}
      </tbody>
    </Table>
  );
}
