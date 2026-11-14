/**
 * The send plan as a shape: one dot for one person, in a day that has room for so many. Dragging the
 * pace moves the dots from day to day, and the day's limit is the top of the shape, so what cannot
 * happen cannot be drawn. A person sees when each email will go before any of them does.
 */
import { useMemo, useState } from 'react';
import type { ListDay } from '@/api/types';
import { cn } from '@/lib/cn';
import { plural } from '@/lib/format';
import type { PlanDay } from './plan';

interface Shape {
  columns: number;
  pitch: number;
  dot: number;
}

const shapeOf = (limit: number): Shape => (limit <= 60 ? { columns: 6, pitch: 10, dot: 8 } : { columns: 10, pitch: 6, dot: 5 });
const GAP = 16;

interface Dot {
  key: string;
  kind: 'first' | 'follow' | 'other' | 'sent';
  x: number;
  y: number;
  who?: string;
}

const weekday = (at: number) => new Date(at).toLocaleDateString('en', { weekday: 'short' });
const dayNumber = (at: number) => new Date(at).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
const longDay = (at: number) => new Date(at).toLocaleDateString('en', { weekday: 'long', day: 'numeric', month: 'short' });

interface Column {
  at: number;
  past: boolean;
  other: number;
  first: number;
  follow: number;
  sent: number;
  replies: number;
  words: string;
}

function columnsOf(past: ListDay[], plan: PlanDay[], other: number): Column[] {
  const before = past.map((day): Column => {
    const at = new Date(day.at).getTime();
    return { at, past: true, other, first: 0, follow: 0, sent: day.first + day.follow, replies: day.replies, words: `${longDay(at)}: ${plural(day.first + day.follow, 'email')} sent from this list, and about ${other} other` };
  });
  const after = plan.map((day): Column => ({
    at: day.at,
    past: false,
    other: day.other,
    first: day.first.length,
    follow: day.follow.length,
    sent: 0,
    replies: 0,
    words: `${longDay(day.at)}: ${plural(day.first.length, 'first email')}${day.follow.length ? `, up to ${plural(day.follow.length, 'follow-up')}` : ''}, and about ${day.other} other`,
  }));
  return [...before, ...after];
}

function dotsOf(columns: Column[], plan: PlanDay[], shape: Shape, height: number): Dot[] {
  const dots: Dot[] = [];
  const width = shape.columns * shape.pitch;
  columns.forEach((column, index) => {
    const planned = plan.find((day) => day.at === column.at);
    let slot = 0;
    const place = (key: string, kind: Dot['kind'], who?: string) => {
      dots.push({ key, kind, who, x: index * (width + GAP) + (slot % shape.columns) * shape.pitch, y: height - (Math.floor(slot / shape.columns) + 1) * shape.pitch });
      slot += 1;
    };
    for (let item = 0; item < column.other; item += 1) place(`other:${column.at}:${item}`, 'other');
    for (let item = 0; item < column.sent; item += 1) place(`sent:${column.at}:${item}`, 'sent');
    for (const person of planned?.follow ?? []) place(`follow:${person.id}`, 'follow', person.name);
    for (const person of planned?.first ?? []) place(`first:${person.id}`, 'first', person.name);
  });
  return dots;
}

const LEGEND: { kind: Dot['kind']; word: string }[] = [
  { kind: 'sent', word: 'Sent' },
  { kind: 'first', word: 'First email' },
  { kind: 'follow', word: 'Follow-up, only if no reply' },
  { kind: 'other', word: 'Other mail from this mailbox' },
];

export function SendPlan({ past, plan, limit, other }: { past: ListDay[]; plan: PlanDay[]; limit: number; other: number }) {
  const shape = shapeOf(limit);
  const height = Math.ceil(limit / shape.columns) * shape.pitch;
  const columns = useMemo(() => columnsOf(past, plan, other), [past, plan, other]);
  const dots = useMemo(() => dotsOf(columns, plan, shape, height), [columns, plan, shape, height]);
  const [day, setDay] = useState<number | null>(null);
  const width = shape.columns * shape.pitch;
  const foot = past.some((item) => item.replies) ? 60 : 44;
  const total = columns.length * (width + GAP) - GAP;
  const pointed = columns.find((column) => column.at === day);
  const kinds = new Set(dots.map((dot) => dot.kind));

  return (
    <div className="min-w-0">
      <div className="flex min-h-5 items-baseline justify-between gap-4">
        <p aria-live="polite" className="min-w-0 truncate text-13 text-muted">{pointed ? pointed.words : 'Point at a day to read it.'}</p>
        <p className="shrink-0 text-12 text-faint">Top of each day: the limit of {limit}</p>
      </div>
      <div className="scrollbar-none mt-2 overflow-x-auto pb-1">
        <div className="relative" style={{ width: total, height: height + foot }}>
          {columns.map((column, index) => (
            <button
              key={column.at}
              type="button"
              aria-label={column.words}
              onPointerEnter={() => setDay(column.at)}
              onPointerLeave={() => setDay(null)}
              onFocus={() => setDay(column.at)}
              onBlur={() => setDay(null)}
              className="focus-inset absolute top-0 rounded-sm"
              style={{ left: index * (width + GAP) - 4, width: width + 6, height: height + foot }}
            >
              <span className={cn('absolute inset-x-0 top-0 rounded-sm transition-colors duration-(--duration-fast) ease-standard', day === column.at ? 'bg-fill-2' : 'bg-fill-1')} style={{ height: height + 4 }} />
              <span className="absolute inset-x-1 top-0 h-px bg-(--board-wire-strong)" />
              <span className="absolute inset-x-0 text-center text-12" style={{ top: height + 10 }}>
                <span className="block font-medium">{weekday(column.at)}</span>
                <span className="block text-faint">{dayNumber(column.at)}</span>
                {column.replies ? <span className="block text-muted">{plural(column.replies, 'reply', 'replies')}</span> : null}
              </span>
            </button>
          ))}
          {dots.map((dot, index) => (
            <span key={dot.key} aria-hidden="true" className="plan-dot plan-arrive pointer-events-none" data-kind={dot.kind} style={{ width: shape.dot, height: shape.dot, left: 0, top: 0, transform: `translate(${dot.x}px, ${dot.y}px)`, animationDelay: `${Math.min(index * 3, 420)}ms` }} />
          ))}
        </div>
      </div>
      <ul aria-label="What the dots mean" className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-12 text-muted">
        {LEGEND.filter((item) => kinds.has(item.kind)).map((item) => (
          <li key={item.kind} className="flex items-center gap-2">
            <span className="plan-dot relative! size-2" data-kind={item.kind} />
            {item.word}
          </li>
        ))}
      </ul>
    </div>
  );
}
