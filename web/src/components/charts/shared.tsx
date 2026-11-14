/** What every chart shares: its size, its axis maths and its hover readout. */
import type { ReactNode } from 'react';
import { useEffect, useRef, useState } from 'react';
import { cn } from '@/lib/cn';

/** Width of the element, kept current. A chart draws to real pixels so hairlines stay 1 px. */
export function useWidth<T extends HTMLElement>(fallback = 640) {
  const ref = useRef<T>(null);
  const [width, setWidth] = useState(fallback);
  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const measure = () => {
      const next = element.getBoundingClientRect().width;
      if (next > 0) setWidth(next);
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return [ref, width] as const;
}

/** A clean top for the axis: 1, 2 or 5 times a power of ten, at or above the highest value. */
export function niceMax(value: number): number {
  if (value <= 0) return 1;
  const power = 10 ** Math.floor(Math.log10(value));
  const scaled = value / power;
  const step = scaled <= 1 ? 1 : scaled <= 2 ? 2 : scaled <= 5 ? 5 : 10;
  return step * power;
}

export function shortTime(iso: string, seconds: number): string {
  const date = new Date(iso);
  if (seconds >= 86_400) return date.toLocaleDateString('en', { month: 'short', day: 'numeric' });
  if (seconds >= 21_600) return date.toLocaleDateString('en', { weekday: 'short' });
  return date.toLocaleTimeString('en', { hour: '2-digit', minute: '2-digit', hour12: false });
}

export function fullTime(iso: string, seconds: number): string {
  const date = new Date(iso);
  const day = date.toLocaleDateString('en', { month: 'short', day: 'numeric' });
  if (seconds >= 86_400) return day;
  return `${day}, ${date.toLocaleTimeString('en', { hour: '2-digit', minute: '2-digit', hour12: false })}`;
}

/** Three or four evenly spaced indexes for x labels. Few ticks, always including the ends. */
export function tickIndexes(count: number, width: number): number[] {
  const slots = width < 360 ? 3 : 4;
  if (count <= slots) return Array.from({ length: count }, (_, index) => index);
  return Array.from({ length: slots }, (_, index) => Math.round((index * (count - 1)) / (slots - 1)));
}

interface ReadoutProps {
  x: number;
  width: number;
  children: ReactNode;
}

/** The floating value. It flips to the other side near an edge, so it never leaves the chart. */
export function Readout({ x, width, children }: ReadoutProps) {
  const flip = x > width - 150;
  return (
    <div
      className="pointer-events-none absolute top-0 z-10 rounded-md border border-line bg-raised px-2 py-1 text-12 whitespace-nowrap shadow-popover"
      style={flip ? { right: width - x + 8 } : { left: x + 8 }}
    >
      {children}
    </div>
  );
}

/** A short stroke of the mark's colour: the key for a row in a readout. */
export function LineKey({ color }: { color: string }) {
  return <span aria-hidden="true" className="inline-block h-0.5 w-2 shrink-0 rounded-full" style={{ backgroundColor: color }} />;
}

/** Text beside a mark stays in text tokens. The swatch carries the identity. */
export function Legend({ items, className }: { items: { label: string; color: string }[]; className?: string }) {
  return (
    <ul className={cn('flex flex-wrap items-center gap-x-4 gap-y-1 text-12 text-muted', className)}>
      {items.map((item) => (
        <li key={item.label} className="flex items-center gap-1">
          <span aria-hidden="true" className="size-2 rounded-sm" style={{ backgroundColor: item.color }} />
          {item.label}
        </li>
      ))}
    </ul>
  );
}

/** The chart's twin for anyone who cannot use the picture. */
export function ChartTable({ caption, columns, rows }: { caption: string; columns: string[]; rows: (string | number)[][] }) {
  return (
    // On the div, not the table: a table grows past its height, and a long one stretches the page.
    <div className="sr-only">
    <table>
      <caption>{caption}</caption>
      <thead>
        <tr>
          {columns.map((column) => (
            <th key={column} scope="col">
              {column}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={String(row[0])}>
            {row.map((cell, index) => (
              <td key={index}>{cell}</td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
    </div>
  );
}
