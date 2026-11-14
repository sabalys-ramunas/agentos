/**
 * The top of a node page: who it is, what it does in one plain sentence, a heartbeat and the
 * numbers that matter, all live. The tabs under it hold the depth.
 */
import type { FreezeRecord, NodeConnections, NodeDetail, NodeMetrics, NodePoint, NodeSystem } from '@/api/types';
import type { Band } from '@/components/charts/time-chart';
import { Meter } from '@/components/ui/misc';
import { StateBadge } from '@/components/ui/state-badge';
import { Status } from '@/components/ui/status';
import { Skeleton } from '@/components/ui/states';
import { cn } from '@/lib/cn';
import { frozenSentence } from '@/lib/freeze';
import { clock, gb } from '@/lib/format';
import { FREEZE_WORDS } from '@/lib/vocabulary';
import { nodeState } from '@/lib/labels';
import { useNow } from '@/lib/use-now';
import { zoneCity, zoneClock } from '@/lib/zone';
import { span, uptime } from './describe';

/** A shape over time with the incident windows shaded. Decoration for a number printed beside it. */
export function MiniTrend({ points, read, bands, max, className }: { points: NodePoint[]; read: (point: NodePoint) => number | null; bands: Band[]; max?: number; className?: string }) {
  const width = 160;
  const height = 32;
  const values = points.map(read);
  const known = values.filter((value): value is number => value !== null);
  const top = max ?? Math.max(1, ...known) * 1.1;
  const low = max === undefined ? Math.min(...known, top) * 0.9 : 0;
  const first = points[0] ? Date.parse(points[0].at) : 0;
  const last = points[points.length - 1] ? Date.parse(points[points.length - 1]?.at ?? '') : 1;
  const x = (index: number) => (points.length > 1 ? (index / (points.length - 1)) * width : 0);
  const xAt = (at: number) => Math.max(0, Math.min(1, (at - first) / Math.max(1, last - first))) * width;
  const y = (value: number) => height - 2 - ((value - low) / Math.max(1e-9, top - low)) * (height - 4);
  let line = '';
  let open = false;
  values.forEach((value, index) => {
    if (value === null) {
      open = false;
      return;
    }
    line += `${open ? 'L' : 'M'}${x(index).toFixed(1)} ${y(value).toFixed(1)}`;
    open = true;
  });
  return (
    <svg viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" aria-hidden="true" className={cn('h-8 w-full', className)}>
      {bands
        .filter((band) => band.to > first && band.from < last)
        .map((band) => (
          <rect key={band.from} x={xAt(band.from)} y={0} width={Math.max(1.5, xAt(band.to) - xAt(band.from))} height={height} fill="var(--red)" fillOpacity={0.12} />
        ))}
      <path d={line} fill="none" stroke="var(--foreground-subtle)" strokeWidth={1.25} strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

/**
 * One tick a minute for the last hour, as tall as the CPU was. A minute the node was away is a
 * short red tick on the floor, so absence never reads as an idle machine.
 */
function Heartbeat({ points, beat }: { points: NodePoint[]; beat: string }) {
  const ticks = points.slice(-60);
  const missed = ticks.filter((point) => !point.up).length;
  return (
    <div className="flex min-w-0 items-end gap-4">
      <svg
        viewBox="0 0 240 28"
        preserveAspectRatio="none"
        role="img"
        aria-label={`Heartbeat for the last hour: ${missed ? `${missed} minutes missed` : 'every minute reported'}.`}
        className="h-7 min-w-0 flex-1"
      >
        {ticks.map((point, index) => {
          const height = point.up ? 4 + ((point.cpu_pct ?? 0) / 100) * 24 : 3;
          return (
            <rect
              key={point.at}
              x={index * 4}
              y={28 - height}
              width={2}
              height={height}
              rx={1}
              fill={point.up ? 'var(--foreground)' : 'var(--red)'}
              fillOpacity={point.up ? 0.18 + (index / ticks.length) * 0.5 : 0.8}
            />
          );
        })}
      </svg>
      {/* Keyed on the last report, so the pulse plays once per heartbeat that really arrives. */}
      <span key={beat} aria-hidden="true" className="mb-1 size-2 shrink-0 animate-beat rounded-full bg-green" />
    </div>
  );
}

function Tile({ label, value, note, children, live, wide }: { label: string; value: string; note: string; children?: React.ReactNode; live?: boolean; wide?: boolean }) {
  return (
    <div className={cn('flex min-w-0 flex-col bg-bg px-4 pt-4 pb-3 sm:px-5', wide && 'col-span-2 lg:col-span-1')}>
      <p className="flex items-center gap-2 text-12 text-faint">
        {label}
        {live ? <span className="sr-only">, live</span> : null}
      </p>
      <p className="mono tabular mt-1 truncate text-20">{value}</p>
      <div className="mt-2 h-8">{children}</div>
      <p className="mt-1 truncate text-12 text-faint">{note}</p>
    </div>
  );
}

interface HeaderProps {
  node: NodeDetail;
  sentence: string | null;
  system: NodeSystem | null | undefined;
  hour: NodeMetrics | undefined;
  day: NodeMetrics | undefined;
  connections: NodeConnections | null | undefined;
  bands: Band[];
  actions: React.ReactNode;
  /** The freeze on this whole computer, when there is one. */
  frozen?: FreezeRecord | null;
}

export function NodeHeader({ node, sentence, system, hour, day, connections, bands, actions, frozen }: HeaderProps) {
  const now = useNow(true);
  const down = node.state === 'down';
  const lastSeen = Date.parse(node.last_seen);
  const away = bands.find((band) => band.to >= now - 1_000 && band.from <= now);
  const ago = Math.max(0, Math.round((now - lastSeen) / 1000));
  const zone = system?.timezone ?? node.timezone ?? null;
  const meta = [
    system?.os ?? node.os,
    system?.arch ?? node.arch,
    system?.cpu_model ? `${system.cpu_model}${system.cores ? `, ${system.cores} cores` : ''}` : null,
    system?.mem_total_mb ? gb(system.mem_total_mb) : null,
    `agentos ${node.version}`,
    system?.uptime_s ? `up ${uptime(system.uptime_s)}` : null,
  ].filter(Boolean);

  return (
    <header className="grid gap-6">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-1">
            <h1 className="mono truncate text-24 font-semibold">{node.id}</h1>
            <Status status={nodeState[node.state]} />
            {frozen ? <StateBadge state="stuck" label={FREEZE_WORDS.frozen.word} sentence={frozenSentence(frozen)} className="shrink-0" /> : null}
          </div>
          {sentence ? <p className="mt-2 max-w-3xl text-16 text-muted text-pretty">{sentence}</p> : <Skeleton className="mt-3 h-4 w-96 max-w-full" />}
          <p className="mono mt-2 text-12 text-faint">
            {meta.join(' · ')}
            {zone ? (
              <span className="whitespace-nowrap">
                {' · '}
                <span className="tabular">{zoneClock(now, zone)}</span> {zoneCity(zone)}
              </span>
            ) : null}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">{actions}</div>
      </div>

      <div className="registered rounded-xl border border-line">
        <div className="flex flex-wrap items-center gap-x-6 gap-y-2 border-b border-line px-4 py-3 sm:px-5">
          <p className="silk shrink-0">Heartbeat</p>
          <div className="min-w-48 flex-1">{hour ? <Heartbeat points={hour.points} beat={node.last_seen} /> : <Skeleton className="h-7" />}</div>
          <p className="tabular w-56 shrink-0 text-right text-13 text-muted max-sm:w-full max-sm:text-left" aria-live="off">
            {down || away ? (
              <span className="text-red">
                Away since {clock(new Date(away?.from ?? lastSeen).toISOString())} ({span(now - (away?.from ?? lastSeen))})
              </span>
            ) : (
              <>Last report {ago < 5 ? 'just now' : `${ago} s ago`}</>
            )}
          </p>
        </div>
        {/* The hairlines between tiles are the gap, so they are right at any column count. */}
        <div className="grid grid-cols-2 gap-px overflow-hidden rounded-b-xl bg-line lg:grid-cols-5">
          <Tile label="CPU" value={down ? '–' : `${node.cpu}%`} note={day?.points.length ? 'Last 24 hours' : 'Right now'} live>
            {day ? <MiniTrend points={day.points} read={(point) => point.cpu_pct} bands={bands} max={100} /> : null}
          </Tile>
          <Tile label="Memory" value={down ? '–' : gb(node.mem.used_mb)} note={`of ${gb(node.mem.total_mb)}`} live>
            {day ? <MiniTrend points={day.points} read={(point) => point.mem_used_mb} bands={bands} max={node.mem.total_mb} /> : null}
          </Tile>
          <Tile label="Disk free" value={gb(node.load.disk_free_mb)} note={system?.disk_total_mb ? `of ${gb(system.disk_total_mb)}` : 'On this machine'}>
            {day ? <MiniTrend points={day.points} read={(point) => point.disk_free_mb} bands={bands} /> : null}
          </Tile>
          <Tile label="Warm accounts" value={`${node.warm_lanes} of ${node.max_warm_lanes}`} note="Limit set on the machine">
            <div className="flex h-8 items-center">
              <Meter value={node.warm_lanes} max={node.max_warm_lanes} label="Warm accounts" className="w-full" />
            </div>
          </Tile>
          <Tile wide label="Up, 30 days" value={connections ? `${connections.uptime_pct}%` : '–'} note={connections ? `${connections.days.reduce((sum, item) => sum + item.incidents, 0)} incidents` : 'Not reported'}>
            {connections ? (
              <svg viewBox="0 0 120 32" preserveAspectRatio="none" aria-hidden="true" className="h-8 w-full">
                {connections.days.map((item, index) => (
                  <g key={item.day}>
                    <rect x={index * 4} y={8} width={2.5} height={24} rx={1} fill="var(--foreground)" fillOpacity={0.22} />
                    {item.up_pct < 100 ? <rect x={index * 4} y={8} width={2.5} height={Math.min(24, Math.max(4, (100 - item.up_pct) * 2.4))} rx={1} fill="var(--red)" /> : null}
                  </g>
                ))}
              </svg>
            ) : null}
          </Tile>
        </div>
      </div>
    </header>
  );
}
