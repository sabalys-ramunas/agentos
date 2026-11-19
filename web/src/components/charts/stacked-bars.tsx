/* eslint-disable jsx-a11y/no-noninteractive-element-interactions -- the chart is driven by pointer and keys, and is labelled as a group */
import { useRef, useState } from 'react';
import { cn } from '@/lib/cn';
import { ChartTable, fullTime, Legend, LineKey, niceMax, Readout, shortTime, tickIndexes, useWidth } from './shared';

export interface BarSeries {
  key: string;
  label: string;
  color: string;
}

export interface BarBucket {
  at: string;
  values: Record<string, number>;
}

interface StackedBarsProps {
  buckets: BarBucket[];
  series: BarSeries[];
  bucketSeconds: number;
  label: string;
  height?: number;
  format?: (value: number) => string;
  /** Drag a range or click a bar. Omit for a chart that only reads. */
  onSelect?: (range: { from: number; to: number } | null) => void;
  selected?: { from: number; to: number } | null;
}

const AXIS_X = 20;
const AXIS_Y = 32;
/** Thin on purpose. A wide bar is a block of colour, and these sit in a calm page. */
const MAX_BAR = 14;
/** Room above the top tick, so its label is not cut off. */
const TOP = 8;
const GAP = 2;
const RADIUS = 4;

/** A bar with a rounded data end and a square foot, so it stands on the baseline. */
function roundedTop(x: number, y: number, width: number, height: number): string {
  const r = Math.min(RADIUS, width / 2, height);
  return `M${x} ${y + height} V${y + r} Q${x} ${y} ${x + r} ${y} H${x + width - r} Q${x + width} ${y} ${x + width} ${y + r} V${y + height} Z`;
}

/** Stacks over time. Dragging selects a range, a click selects one bar, keys do the same. */
export function StackedBars({ buckets, series, bucketSeconds, label, height = 136, format = String, onSelect, selected }: StackedBarsProps) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [active, setActive] = useState<number | null>(null);
  const [drag, setDrag] = useState<{ from: number; to: number } | null>(null);
  const dragging = useRef(false);

  const total = (bucket: BarBucket) => series.reduce((sum, item) => sum + (bucket.values[item.key] ?? 0), 0);
  const top = niceMax(Math.max(0, ...buckets.map(total)));
  const plotWidth = Math.max(1, width - AXIS_Y);
  const plotHeight = height - AXIS_X;
  const count = buckets.length;
  const slot = plotWidth / Math.max(1, count);
  const bar = Math.max(2, Math.min(MAX_BAR, slot - GAP));
  const barX = (index: number) => AXIS_Y + index * slot + (slot - bar) / 2;
  const scale = (value: number) => (value / top) * (plotHeight - TOP);

  const indexAt = (clientX: number, element: HTMLElement) =>
    Math.max(0, Math.min(count - 1, Math.floor((clientX - element.getBoundingClientRect().left - AXIS_Y) / slot)));

  const finish = (range: { from: number; to: number }) => {
    const ordered = { from: Math.min(range.from, range.to), to: Math.max(range.from, range.to) };
    const same = selected?.from === ordered.from && selected?.to === ordered.to;
    onSelect?.(same ? null : ordered);
  };

  const onKey = (event: React.KeyboardEvent) => {
    const step = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0;
    if (step) {
      event.preventDefault();
      setActive((index) => Math.max(0, Math.min(count - 1, (index ?? count - 1) + step)));
    } else if ((event.key === 'Enter' || event.key === ' ') && active !== null && onSelect) {
      event.preventDefault();
      finish({ from: active, to: active });
    } else if (event.key === 'Escape') {
      setActive(null);
      if (selected) onSelect?.(null);
    }
  };

  const shown = drag ?? selected ?? null;
  const inRange = (index: number) => !shown || (index >= Math.min(shown.from, shown.to) && index <= Math.max(shown.from, shown.to));
  const bucket = active === null ? null : buckets[active];
  const multi = series.length > 1;

  return (
    <div className="grid gap-2">
      {multi ? <Legend items={series.map(({ label: name, color }) => ({ label: name, color }))} /> : null}
      <div
        ref={ref}
        role="group"
        // Arrow keys and Enter drive the chart, so it has to be reachable by keyboard.
        // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
        tabIndex={0}
        aria-label={`${label}. Use the left and right arrow keys to read each bar${onSelect ? ', and Enter to filter by it' : ''}.`}
        className={cn('focus-inset relative rounded-sm touch-pan-y select-none', onSelect && 'cursor-crosshair')}
        style={{ height }}
        onPointerDown={(event) => {
          if (!onSelect) return;
          const index = indexAt(event.clientX, event.currentTarget);
          dragging.current = true;
          event.currentTarget.setPointerCapture?.(event.pointerId);
          setDrag({ from: index, to: index });
        }}
        onPointerMove={(event) => {
          const index = indexAt(event.clientX, event.currentTarget);
          setActive(index);
          if (dragging.current) setDrag((range) => (range ? { ...range, to: index } : range));
        }}
        onPointerUp={() => {
          if (dragging.current && drag) finish(drag);
          dragging.current = false;
          setDrag(null);
        }}
        onPointerLeave={() => {
          if (!dragging.current) setActive(null);
        }}
        onKeyDown={onKey}
        onBlur={() => setActive(null)}
      >
        <svg width={width} height={height} aria-hidden="true" className="block">
          <g className="text-micro font-normal" fill="var(--foreground-subtle)">
            {[top, top / 2].map((tick) => (
              <g key={tick}>
                <line x1={AXIS_Y} x2={width} y1={plotHeight - scale(tick) - 0.5} y2={plotHeight - scale(tick) - 0.5} stroke="var(--border)" />
                <text x={AXIS_Y - 8} y={plotHeight - scale(tick) + 4} textAnchor="end" className="tabular">
                  {format(tick)}
                </text>
              </g>
            ))}
            <line x1={AXIS_Y} x2={width} y1={plotHeight - 0.5} y2={plotHeight - 0.5} stroke="var(--border)" />
            {tickIndexes(count, width).map((index, position, all) => {
              const item = buckets[index];
              if (!item) return null;
              const anchor = position === 0 ? 'start' : position === all.length - 1 ? 'end' : 'middle';
              return (
                <text key={index} x={barX(index) + (anchor === 'end' ? bar : 0)} y={height - 4} textAnchor={anchor} className="tabular">
                  {shortTime(item.at, bucketSeconds)}
                </text>
              );
            })}
          </g>
          {buckets.map((item, index) => {
            let base = plotHeight - 1;
            const parts = series.filter((entry) => (item.values[entry.key] ?? 0) > 0);
            return (
              <g key={item.at} opacity={inRange(index) ? 1 : 0.28} className="transition-opacity duration-(--duration-fast) ease-standard">
                {index === active ? <rect x={AXIS_Y + index * slot} y={0} width={slot} height={plotHeight} fill="var(--fill-1)" /> : null}
                {parts.map((entry, position) => {
                  const size = Math.max(1, scale(item.values[entry.key] ?? 0));
                  const y = base - size;
                  base = y - GAP;
                  return position === parts.length - 1 ? (
                    <path key={entry.key} d={roundedTop(barX(index), y, bar, size)} fill={entry.color} />
                  ) : (
                    <rect key={entry.key} x={barX(index)} y={y} width={bar} height={size} fill={entry.color} />
                  );
                })}
              </g>
            );
          })}
        </svg>
        {bucket && active !== null ? (
          <Readout x={barX(active) + bar} width={width}>
            <p className="tabular font-medium text-fg">{format(total(bucket))}</p>
            <p className="tabular text-faint">{fullTime(bucket.at, bucketSeconds)}</p>
            {multi
              ? series
                  .filter((entry) => (bucket.values[entry.key] ?? 0) > 0)
                  .map((entry) => (
                    <p key={entry.key} className="flex items-center gap-2 text-muted">
                      <LineKey color={entry.color} />
                      <span className="tabular text-fg">{format(bucket.values[entry.key] ?? 0)}</span>
                      {entry.label}
                    </p>
                  ))
              : null}
          </Readout>
        ) : null}
      </div>
      <ChartTable
        caption={label}
        columns={['Time', ...series.map((item) => item.label)]}
        rows={buckets.map((item) => [fullTime(item.at, bucketSeconds), ...series.map((entry) => format(item.values[entry.key] ?? 0))])}
      />
    </div>
  );
}
