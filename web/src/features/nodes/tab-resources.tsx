import type { NodeDetail, NodeMetrics, NodeRange, NodeService } from '@/api/types';
import { Segmented } from '@/components/charts/small';
import { TimeChart } from '@/components/charts/time-chart';
import type { Band } from '@/components/charts/time-chart';
import { Card } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/states';
import { gb } from '@/lib/format';
import { span } from './describe';
import { Panel } from './parts';

const RANGES: { value: NodeRange; label: string }[] = [
  { value: '1h', label: '1 hour' },
  { value: '24h', label: '24 hours' },
  { value: '7d', label: '7 days' },
];

/** The processes that make agentos run, as opposed to what it runs. */
const RUNTIME = new Set(['agentosd', 'scheduler', 'websockify']);

interface Part {
  label: string;
  mb: number;
  color: string;
  note: string;
}

/**
 * Memory by what uses it. Lanes and services are reported; the rest of what is used is the
 * system and its cache. One hue in steps, darkest for what agents use, because these parts
 * are ordered by how close they are to the work, and each is named in text beside the bar.
 */
export function memorySplit(node: NodeDetail, services: NodeService[] | null): Part[] | null {
  const lanes = node.lanes.reduce((sum, lane) => sum + (lane.mem_mb ?? 0), 0);
  if (!services && !node.lanes.some((lane) => lane.mem_mb !== undefined)) return null;
  const runtime = (services ?? []).filter((item) => RUNTIME.has(item.name)).reduce((sum, item) => sum + (item.mem_mb ?? 0), 0);
  const others = (services ?? []).filter((item) => !RUNTIME.has(item.name));
  const other = others.reduce((sum, item) => sum + (item.mem_mb ?? 0), 0);
  const used = node.mem.used_mb;
  const system = Math.max(0, used - lanes - runtime - other);
  const warm = node.lanes.filter((lane) => lane.mem_mb).length;
  const parts: Part[] = [
    { label: 'Browser and app accounts', mb: lanes, color: 'var(--foreground)', note: `${warm} warm` },
    { label: 'agentos runtime', mb: runtime, color: 'var(--foreground-muted)', note: 'agentosd, scheduler, remote view' },
    { label: 'Other services', mb: other, color: 'var(--control)', note: others.map((item) => item.name).slice(0, 3).join(', ') || 'None' },
    { label: 'System and cache', mb: system, color: 'var(--fill-3)', note: 'The OS, page cache, everything else' },
    { label: 'Free', mb: Math.max(0, node.mem.total_mb - used), color: 'transparent', note: `of ${gb(node.mem.total_mb)}` },
  ];
  // A part this node does not have is left out, rather than shown as a zero.
  return parts.filter((part) => part.mb > 0 || part.label === 'Free');
}

function MemorySplit({ parts, total }: { parts: Part[]; total: number }) {
  return (
    <Card className="p-4 sm:p-6">
      <div
        role="img"
        aria-label={`Memory: ${parts.map((part) => `${part.label} ${gb(part.mb)}`).join(', ')}`}
        // The 2 px gap between marks is a chart rule, so it is set here and not from the spacing grid.
        style={{ gap: 2 }}
        className="flex h-3 overflow-hidden rounded-sm border border-line"
      >
        {parts
          .filter((part) => part.mb > 0)
          .map((part) => (
            <span key={part.label} className="block h-full transition-[width] duration-(--duration-slow) ease-standard" style={{ width: `${(part.mb / total) * 100}%`, backgroundColor: part.color }} />
          ))}
      </div>
      <ul className="mt-4 grid gap-x-6 gap-y-3 sm:grid-cols-2 lg:grid-flow-col lg:auto-cols-fr">
        {parts.map((part) => (
          <li key={part.label} className="min-w-0">
            <p className="flex items-center gap-2 text-12 text-faint">
              <span aria-hidden="true" className="size-2 shrink-0 rounded-[2px] border border-line" style={{ backgroundColor: part.color }} />
              <span className="truncate">{part.label}</span>
            </p>
            <p className="mono tabular mt-1 text-14">{gb(part.mb)}</p>
            <p className="truncate text-12 text-faint">{part.note}</p>
          </li>
        ))}
      </ul>
    </Card>
  );
}

interface ResourcesProps {
  node: NodeDetail;
  metrics: NodeMetrics | undefined;
  services: NodeService[] | null;
  range: NodeRange;
  onRange: (range: NodeRange) => void;
  bands: Band[];
}

export function ResourcesTab({ node, metrics, services, range, onRange, bands }: ResourcesProps) {
  const parts = memorySplit(node, services);
  const points = metrics?.points ?? [];
  const seconds = metrics?.bucket_seconds ?? 300;
  const series = (read: (point: (typeof points)[number]) => number | null) => points.map((point) => ({ at: point.at, value: point.up ? read(point) : null }));
  const first = points[0] ? Date.parse(points[0].at) : Date.now();
  const inView = bands.filter((band) => band.to > first);
  const whole = (value: number) => `${Math.round(value / 1024)}G`;
  const charts = [
    { label: 'CPU', format: (value: number) => `${Math.round(value)}%`, tick: undefined, max: 100, data: series((point) => point.cpu_pct) },
    { label: 'Memory used', format: (value: number) => gb(value), tick: whole, max: node.mem.total_mb, data: series((point) => point.mem_used_mb) },
    { label: 'Disk free', format: (value: number) => gb(value), tick: whole, max: undefined, data: series((point) => point.disk_free_mb) },
    { label: 'Warm accounts', format: (value: number) => String(Math.round(value)), tick: undefined, max: node.max_warm_lanes, data: series((point) => point.warm_lanes) },
  ];
  return (
    <div className="grid gap-10">
      <Panel title="Memory, by what uses it" description="Right now. The machine reports accounts and services, and the rest is the system.">
        {parts ? <MemorySplit parts={parts} total={node.mem.total_mb} /> : <Card className="p-4 text-13 text-muted sm:p-6">This machine does not report what uses its memory yet, only the total: {gb(node.mem.used_mb)} of {gb(node.mem.total_mb)}.</Card>}
      </Panel>
      <Panel
        title="Over time"
        description={
          inView.length
            ? `Shaded: this machine was away for ${inView.map((band) => span(band.to - band.from)).join(' and ')}. The lines stop there, because nothing was reported.`
            : 'Gaps are real: a line stops while this machine is away, and never draws through it.'
        }
        action={<Segmented label="Range" value={range} onChange={onRange} options={RANGES} />}
      >
        <div className="grid gap-px overflow-hidden rounded-xl border border-line bg-line lg:grid-cols-2">
          {charts.map((chart) => (
            <div key={chart.label} className="bg-bg p-4 sm:p-6">
              <p className="mb-3 text-13 text-muted">{chart.label}</p>
              {metrics ? (
                <TimeChart
                  points={chart.data}
                  label={`${chart.label}, ${RANGES.find((item) => item.value === range)?.label ?? ''}`}
                  format={chart.format}
                  tickFormat={chart.tick}
                  bucketSeconds={seconds}
                  max={chart.max}
                  height={140}
                  gapLabel="Machine away, nothing reported"
                  bands={bands}
                />
              ) : (
                <Skeleton className="h-[140px] rounded-md" />
              )}
            </div>
          ))}
        </div>
      </Panel>
    </div>
  );
}
