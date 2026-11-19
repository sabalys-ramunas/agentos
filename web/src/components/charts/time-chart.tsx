/* eslint-disable jsx-a11y/no-noninteractive-element-interactions -- the chart is read by pointer and keys, and carries a text summary */
import { useState } from 'react';
import { cn } from '@/lib/cn';
import { fullTime, niceMax, Readout, shortTime, tickIndexes, useWidth } from './shared';

export interface TimePoint {
  at: string;
  /** Null is a real gap: the line stops, and never draws through it. */
  value: number | null;
}

interface TimeChartProps {
  points: TimePoint[];
  /** The accessible name, and what the readout means. */
  label: string;
  format: (value: number) => string;
  bucketSeconds: number;
  height?: number;
  /** A fixed top, such as 100 for a percentage. Otherwise a clean value above the highest. */
  max?: number;
  axes?: boolean;
  className?: string;
  /** What to say when hovering a gap. */
  gapLabel?: string;
  /** Axis labels, when the readout format is too long for the axis. */
  tickFormat?: (value: number) => string;
  /** Incident windows, shaded across the plot so every chart on a page lines up on them. */
  bands?: Band[];
}

export interface Band {
  from: number;
  to: number;
  label: string;
}

const AXIS_X = 20;
const AXIS_Y = 40;

/** One line, one axis. Several measures are several charts, never a second scale. */
export function TimeChart({ points, label, format, bucketSeconds, height = 120, max, axes = true, className, gapLabel = 'No data', bands = [], tickFormat }: TimeChartProps) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [active, setActive] = useState<number | null>(null);
  const values = points.map((point) => point.value).filter((value): value is number => value !== null);
  const top = max ?? niceMax(Math.max(0, ...values));
  const left = axes ? AXIS_Y : 0;
  const plotWidth = Math.max(1, width - left);
  const plotHeight = height - (axes ? AXIS_X : 0);
  const count = points.length;

  const x = (index: number) => left + (count > 1 ? (index / (count - 1)) * plotWidth : 0);
  // With axes, room above the top tick so its label is not cut off.
  const inset = axes ? 8 : 2;
  const y = (value: number) => inset + (plotHeight - inset - 2) * (1 - value / top);

  // Split at every gap, so a line and its wash stop and start again honestly.
  const runs: { index: number; value: number }[][] = [];
  let current: { index: number; value: number }[] = [];
  points.forEach((point, index) => {
    if (point.value === null) {
      if (current.length) runs.push(current);
      current = [];
    } else current.push({ index, value: point.value });
  });
  if (current.length) runs.push(current);

  const line = (run: { index: number; value: number }[]) =>
    run.map((point, position) => `${position ? 'L' : 'M'}${x(point.index).toFixed(1)} ${y(point.value).toFixed(1)}`).join(' ');
  const area = (run: { index: number; value: number }[]) => {
    const first = run[0];
    const last = run[run.length - 1];
    if (!first || !last) return '';
    return `${line(run)} L${x(last.index).toFixed(1)} ${plotHeight} L${x(first.index).toFixed(1)} ${plotHeight} Z`;
  };

  const pick = (clientX: number, element: HTMLElement) => {
    const offset = clientX - element.getBoundingClientRect().left - left;
    setActive(Math.max(0, Math.min(count - 1, Math.round((offset / plotWidth) * (count - 1)))));
  };

  const onKey = (event: React.KeyboardEvent) => {
    const step = event.key === 'ArrowRight' ? 1 : event.key === 'ArrowLeft' ? -1 : 0;
    if (step) {
      event.preventDefault();
      setActive((index) => Math.max(0, Math.min(count - 1, (index ?? count - 1) + step)));
    } else if (event.key === 'Escape') setActive(null);
  };

  const point = active === null ? null : points[active];
  const first = points[0] ? Date.parse(points[0].at) : 0;
  const last = points[count - 1] ? Date.parse(points[count - 1]?.at ?? '') + bucketSeconds * 1000 : 1;
  const xAt = (at: number) => left + Math.max(0, Math.min(1, (at - first) / Math.max(1, last - first))) * plotWidth;
  const shown = bands.filter((band) => band.to > first && band.from < last);
  const pointAt = point ? Date.parse(point.at) : null;
  const inBand = pointAt === null ? undefined : shown.find((band) => pointAt + bucketSeconds * 1000 > band.from && pointAt < band.to);
  const latest = values[values.length - 1];
  const gaps = points.reduce<number[]>((found, item, index) => (item.value === null ? [...found, index] : found), []);

  return (
    <div
      ref={ref}
      role="img"
      // Arrow keys read the chart, so it has to be reachable by keyboard.
      // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
      tabIndex={0}
      aria-label={`${label}. Latest ${latest === undefined ? 'unknown' : format(latest)}. Highest ${format(Math.max(0, ...values))}.`}
      className={cn('focus-inset relative rounded-sm touch-pan-y select-none', className)}
      style={{ height }}
      onPointerMove={(event) => pick(event.clientX, event.currentTarget)}
      onPointerDown={(event) => pick(event.clientX, event.currentTarget)}
      onPointerLeave={() => setActive(null)}
      onKeyDown={onKey}
      onBlur={() => setActive(null)}
    >
      <svg width={width} height={height} aria-hidden="true" className="block">
        {axes ? (
          <g className="text-micro font-normal" fill="var(--foreground-subtle)">
            {[top, top / 2].map((tick) => (
              <g key={tick}>
                <line x1={left} x2={width} y1={y(tick)} y2={y(tick)} stroke="var(--border)" />
                <text x={left - 8} y={y(tick) + 4} textAnchor="end" className="tabular">
                  {(tickFormat ?? format)(tick)}
                </text>
              </g>
            ))}
            <line x1={left} x2={width} y1={plotHeight - 0.5} y2={plotHeight - 0.5} stroke="var(--border)" />
            {tickIndexes(count, width).map((index, position, all) => {
              const item = points[index];
              if (!item) return null;
              const anchor = position === 0 ? 'start' : position === all.length - 1 ? 'end' : 'middle';
              return (
                <text key={index} x={x(index)} y={height - 4} textAnchor={anchor} className="tabular">
                  {shortTime(item.at, bucketSeconds)}
                </text>
              );
            })}
          </g>
        ) : null}
        {shown.map((band) => (
          <g key={band.from}>
            <rect x={xAt(band.from)} y={0} width={Math.max(2, xAt(band.to) - xAt(band.from))} height={plotHeight} fill="var(--red)" fillOpacity={0.07} />
            <line x1={xAt(band.from)} x2={xAt(band.from)} y1={0} y2={plotHeight} stroke="var(--red)" strokeOpacity={0.45} strokeDasharray="2 3" />
          </g>
        ))}
        {gaps.map((index) => (
          // Down time is shown as space with a faint wash, so it reads as absence and not as zero.
          <rect key={index} x={x(index) - plotWidth / count / 2} y={0} width={plotWidth / count + 1} height={plotHeight} fill="var(--fill-1)" />
        ))}
        {runs.map((run) => (
          <g key={run[0]?.index}>
            <path d={area(run)} fill="var(--foreground)" fillOpacity={0.08} />
            <path d={line(run)} fill="none" stroke="var(--foreground)" strokeWidth={axes ? 2 : 1.5} strokeLinejoin="round" strokeLinecap="round" />
          </g>
        ))}
        {active !== null ? (
          <line x1={x(active)} x2={x(active)} y1={0} y2={plotHeight} stroke="var(--foreground-subtle)" />
        ) : null}
        {active !== null && point?.value !== null && point?.value !== undefined ? (
          <circle cx={x(active)} cy={y(point.value)} r={4} fill="var(--foreground)" stroke="var(--background)" strokeWidth={2} />
        ) : null}
      </svg>
      {active !== null && point ? (
        <Readout x={x(active)} width={width}>
          <span className="tabular font-medium text-fg">{point.value === null ? gapLabel : format(point.value)}</span>
          <span className="tabular ml-2 text-faint">{fullTime(point.at, bucketSeconds)}</span>
          {inBand ? <span className="ml-2 text-bad">{inBand.label}</span> : null}
        </Readout>
      ) : null}
    </div>
  );
}
