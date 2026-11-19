import { Link } from '@tanstack/react-router';
import { useEffect, useMemo, useState } from 'react';
import type { PointerEvent } from 'react';
import type { Schedule } from '@/api/types';
import { Segmented } from '@/components/charts/small';
import { cn } from '@/lib/cn';
import { routineName } from '@/lib/principal';
import { projectFires } from '@/lib/cron';
import { plural } from '@/lib/format';
import { fireLabel, instantOf, localZone, startOfDay, wallClock, zoneCity, zoneClock, zoneDay, zonesDiffer } from '@/lib/zone';

export type TimelineRange = '1d' | '7d' | '30d';

const HOUR = 3_600_000;
const DAY = 24 * HOUR;
const CAP = 600;
const RANGES: { value: TimelineRange; label: string }[] = [
  { value: '1d', label: '24 hours' },
  { value: '7d', label: '7 days' },
  { value: '30d', label: '30 days' },
];

export interface TimelineMark {
  at: number;
  /** The node said so. Every other mark is read from the cron line here. */
  confirmed: boolean;
}

export interface TimelineRow {
  schedule: Schedule;
  zone: string;
  marks: TimelineMark[];
  state: 'fires' | 'paused' | 'watch';
}

/** What a schedule with no routine is called: the computer keeps it, and nothing a person knows starts it. */
export const NO_ROUTINE = 'Not tied to a routine';

export const isPaused = (schedule: Schedule) => schedule.enabled === false;
export const nextFire = (schedule: Schedule) => schedule.next_fire_at ?? schedule.next_run ?? null;

/** The node's next time is the truth. A projection only fills in the fires after it. */
export function timelineRow(schedule: Schedule, zone: string, now: number, to: number): TimelineRow {
  if (isPaused(schedule)) return { schedule, zone, marks: [], state: 'paused' };
  if (!schedule.cron) return { schedule, zone, marks: [], state: 'watch' };
  const next = nextFire(schedule);
  const confirmed = next ? Date.parse(next) : null;
  const projected = projectFires(schedule.cron, now, to, zone, CAP) ?? [];
  const marks = projected
    .filter((at) => confirmed === null || at > confirmed)
    .map((at) => ({ at, confirmed: false }));
  if (confirmed !== null && confirmed >= now && confirmed < to) marks.unshift({ at: confirmed, confirmed: true });
  return { schedule, zone, marks, state: 'fires' };
}

interface Tick {
  at: number;
  label: string;
  rest: boolean;
}

export function axisOf(range: TimelineRange, now: number, zone: string): { from: number; to: number; ticks: Tick[] } {
  const ticks: Tick[] = [];
  if (range === '1d') {
    const from = Math.floor(now / HOUR) * HOUR;
    for (let at = from; at < from + DAY; at += 3 * HOUR) ticks.push({ at, label: zoneClock(at, zone), rest: false });
    return { from, to: from + DAY, ticks };
  }
  const from = startOfDay(now, zone);
  const wall = wallClock(from, zone);
  const dayAt = (index: number) => instantOf(zone, wall.year, wall.month, wall.day + index);
  const days = range === '7d' ? 7 : 30;
  for (let index = 0; index < days; index += range === '7d' ? 1 : 5) {
    const at = dayAt(index);
    const [weekday = '', day = '', month = ''] = zoneDay(at, zone).split(' ');
    ticks.push({ at, label: range === '7d' ? `${weekday} ${day}` : `${day} ${month}`, rest: range === '7d' && (weekday === 'Sat' || weekday === 'Sun') });
  }
  return { from, to: dayAt(days), ticks };
}

/** A clock for marks that move slowly: once a minute is plenty, and it keeps the page quiet. */
function useMinute() {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 60_000);
    return () => clearInterval(timer);
  }, []);
  return now;
}

interface Axis {
  from: number;
  to: number;
  ticks: Tick[];
}

function Track({ row, axis, now }: { row: TimelineRow; axis: Axis; now: number }) {
  const [peek, setPeek] = useState<{ mark: TimelineMark; x: number; width: number } | null>(null);
  const span = axis.to - axis.from;
  const position = (at: number) => ((at - axis.from) / span) * 100;

  const move = (event: PointerEvent<HTMLDivElement>) => {
    const box = event.currentTarget.getBoundingClientRect();
    const at = axis.from + ((event.clientX - box.left) / box.width) * span;
    let nearest: TimelineMark | null = null;
    let distance = Infinity;
    for (const mark of row.marks) {
      const gap = Math.abs(mark.at - at);
      if (gap < distance) {
        nearest = mark;
        distance = gap;
      }
    }
    const close = nearest && (distance / span) * box.width <= 12;
    setPeek(close && nearest ? { mark: nearest, x: (position(nearest.at) / 100) * box.width, width: box.width } : null);
  };

  const local = localZone();
  const flip = peek ? peek.x > peek.width - 180 : false;

  return (
    <div className="relative h-9" onPointerMove={move} onPointerLeave={() => setPeek(null)}>
      {axis.ticks.map((tick, index) => {
        const end = axis.ticks[index + 1]?.at ?? axis.to;
        return (
          <span key={tick.at} aria-hidden="true">
            {tick.rest ? <span className="absolute inset-y-0 bg-hover" style={{ left: `${position(tick.at)}%`, width: `${position(end) - position(tick.at)}%` }} /> : null}
            {index > 0 ? <span className="absolute inset-y-0 w-px bg-line" style={{ left: `${position(tick.at)}%` }} /> : null}
          </span>
        );
      })}
      {now > axis.from ? <span aria-hidden="true" className="absolute inset-y-0 w-px bg-faint" style={{ left: `${position(now)}%` }} /> : null}
      {row.state === 'paused' ? (
        <>
          <span
            aria-hidden="true"
            className="absolute inset-x-0 top-1/2 h-3 -translate-y-1/2 rounded-sm"
            style={{ left: `${position(now)}%`, backgroundImage: 'repeating-linear-gradient(135deg, var(--fill-3) 0 1px, transparent 1px 6px)' }}
          />
          <span className="absolute top-1/2 -translate-y-1/2 rounded-sm bg-card px-1 text-caption text-faint" style={{ left: `calc(${position(now)}% + 8px)` }}>
            Paused
          </span>
        </>
      ) : null}
      {row.state === 'watch' ? (
        <span className="absolute top-1/2 max-w-[80%] -translate-y-1/2 truncate rounded-sm bg-card px-1 text-caption text-faint" style={{ left: `calc(${position(now)}% + 8px)` }}>
          On change: {row.schedule.watch}
        </span>
      ) : null}
      {row.state === 'fires' && row.marks.length === 0 ? (
        <span className="absolute top-1/2 -translate-y-1/2 rounded-sm bg-card px-1 text-caption text-faint" style={{ left: `calc(${position(now)}% + 8px)` }}>
          No fire in this window
        </span>
      ) : null}
      {row.marks.map((mark) => (
        <span
          key={mark.at}
          aria-hidden="true"
          className="absolute top-1/2 w-0.5 -translate-y-1/2 rounded-full"
          style={{
            left: `${position(mark.at)}%`,
            height: mark.confirmed ? 18 : 10,
            backgroundColor: mark.confirmed || peek?.mark.at === mark.at ? 'var(--foreground)' : 'var(--foreground-subtle)',
          }}
        />
      ))}
      {peek ? (
        <div
          className="pointer-events-none absolute bottom-full z-10 mb-1 rounded-md border border-line bg-raised px-2 py-1 text-caption whitespace-nowrap shadow-popover"
          style={flip ? { right: peek.width - peek.x } : { left: peek.x }}
        >
          <p className="tabular text-fg">{fireLabel(peek.mark.at, row.zone, now)}</p>
          <p className="text-faint">{peek.mark.confirmed ? 'Next run, from the computer' : 'Worked out from the schedule'}</p>
          {zonesDiffer(row.zone, local, peek.mark.at) ? <p className="tabular text-faint">Your time: {fireLabel(peek.mark.at, local, now)}</p> : null}
        </div>
      ) : null}
    </div>
  );
}

interface TimelineProps {
  org: string;
  schedules: Schedule[];
  zoneOf: (schedule: Schedule) => string;
  fallback: string;
  active: string | null;
  onActive: (id: string | null) => void;
  /** Routine names by id, so a schedule reads as the routine it starts. */
  titles?: ReadonlyMap<string, string>;
}

/** What fires when, on each node's own clock. The table under it carries the same facts as text. */
export function ScheduleTimeline({ org, schedules, zoneOf, fallback, active, onActive, titles = new Map() }: TimelineProps) {
  const [range, setRange] = useState<TimelineRange>('7d');
  const titleOf = (schedule: Schedule) => (schedule.flow ? titles.get(schedule.flow) ?? routineName(schedule.flow) : NO_ROUTINE);
  const now = useMinute();
  const zones = useMemo(() => [...new Set(schedules.map(zoneOf))], [schedules, zoneOf]);
  const zone = zones[0] ?? fallback;
  const axis = useMemo(() => axisOf(range, now, zone), [range, now, zone]);
  const rows = useMemo(() => schedules.map((item) => timelineRow(item, zoneOf(item), now, axis.to)), [schedules, zoneOf, now, axis.to]);

  const fires = rows.reduce((sum, row) => sum + row.marks.length, 0);
  const capped = rows.some((row) => row.marks.length >= CAP);
  const soonest = rows
    .flatMap((row) => row.marks.slice(0, 1).map((mark) => ({ row, mark })))
    .sort((a, b) => a.mark.at - b.mark.at)[0];
  const window = RANGES.find((item) => item.value === range)?.label ?? '';

  return (
    <div className="grid gap-4">
      <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-2">
        <div className="min-w-0">
          {soonest ? (
            <p className="text-body">
              Next: {titleOf(soonest.row.schedule)}, <span className="tabular">{fireLabel(soonest.mark.at, soonest.row.zone, now)}</span>
            </p>
          ) : (
            <p className="text-body">Nothing fires in this window.</p>
          )}
          <p className="tabular text-secondary text-faint">
            {capped ? `${fires}+ fires` : plural(fires, 'fire')} in the next {window}.{' '}
            {zones.length > 1 ? `Axis on ${zoneCity(zone)} time. Each schedule keeps its own computer's clock.` : `On the computer's clock, ${zoneCity(zone)} time.`}
          </p>
        </div>
        <Segmented label="Timeline range" value={range} onChange={setRange} options={RANGES} />
      </div>
      <div>
        <div className="hidden md:grid md:grid-cols-[minmax(0,13rem)_minmax(0,1fr)] md:gap-x-4">
          <span />
          <TickLabels axis={axis} />
        </div>
        <TickLabels axis={axis} className="md:hidden" />
        <ul>
          {rows.map((row) => {
            const first = row.marks[0];
            return (
              <li
                key={row.schedule.id}
                onPointerEnter={() => onActive(row.schedule.id)}
                onPointerLeave={() => onActive(null)}
                className={cn(
                  'grid border-t border-line transition-colors duration-(--duration-fast) ease-standard md:grid-cols-[minmax(0,13rem)_minmax(0,1fr)] md:items-center md:gap-x-4',
                  active === row.schedule.id && 'bg-hover',
                )}
              >
                {row.schedule.flow ? (
                  <Link to="/$org/flows/$flow" params={{ org, flow: row.schedule.flow }} className="truncate pt-2 text-body hover:underline md:pt-0">
                    {titleOf(row.schedule)}
                  </Link>
                ) : (
                  <span className="truncate pt-2 text-body text-muted md:pt-0">{titleOf(row.schedule)}</span>
                )}
                <Track row={row} axis={axis} now={now} />
                <span className="sr-only">
                  {row.state === 'paused' ? 'Paused' : row.state === 'watch' ? `Runs on change: ${row.schedule.watch ?? ''}` : first ? `Next fire ${fireLabel(first.at, row.zone, now)}, ${plural(row.marks.length, 'fire')} in this window` : 'No fire in this window'}
                </span>
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
}

function TickLabels({ axis, className }: { axis: Axis; className?: string }) {
  const span = axis.to - axis.from;
  return (
    <div aria-hidden="true" className={cn('relative h-5', className)}>
      {axis.ticks.map((tick) => (
        <span key={tick.at} className="tabular absolute truncate text-caption text-faint" style={{ left: `${((tick.at - axis.from) / span) * 100}%` }}>
          {tick.label}
        </span>
      ))}
    </div>
  );
}
