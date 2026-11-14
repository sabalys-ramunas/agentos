/**
 * Ten weeks of a routine, a square a day: when it ran and whether it worked. Status colours only,
 * always with the legend and the words in a tooltip, and the list beside it says the same in text.
 */
import { useMemo } from 'react';
import type { RoutineRun, RunStatus } from '@/api/types';
import { Tooltip } from '@/components/ui/menu';
import { Legend } from '@/components/charts/shared';
import { cn } from '@/lib/cn';
import { plural } from '@/lib/format';
import { OUTCOME } from './routine-words';

const WEEKS = 10;
const DAY = 86_400_000;
const WORST: RunStatus[] = ['failed', 'partial', 'succeeded_unverified', 'succeeded_verified'];

const DAYS = ['Mon', '', 'Wed', '', 'Fri', '', 'Sun'];

function startOfWeek(now: number) {
  const day = new Date(now);
  day.setHours(0, 0, 0, 0);
  day.setDate(day.getDate() - ((day.getDay() + 6) % 7));
  return day.getTime();
}

interface Cell {
  at: number;
  runs: RoutineRun[];
  status: RunStatus | null;
  future: boolean;
}

export function rhythmOf(runs: RoutineRun[], now = Date.now()): Cell[][] {
  const thisWeek = startOfWeek(now);
  const byDay = new Map<string, RoutineRun[]>();
  for (const run of runs) {
    const key = new Date(run.started_at).toDateString();
    byDay.set(key, [...(byDay.get(key) ?? []), run]);
  }
  return Array.from({ length: WEEKS }, (_, week) =>
    Array.from({ length: 7 }, (_, weekday) => {
      const date = new Date(thisWeek - (WEEKS - 1 - week) * 7 * DAY);
      date.setDate(date.getDate() + weekday);
      const on = byDay.get(date.toDateString()) ?? [];
      return { at: date.getTime(), runs: on, status: WORST.find((status) => on.some((run) => run.status === status)) ?? null, future: date.getTime() > now };
    }),
  );
}

const dateWords = (at: number) => new Date(at).toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' });

export function Rhythm({ runs, onOpen }: { runs: RoutineRun[]; onOpen: (run: RoutineRun) => void }) {
  const weeks = useMemo(() => rhythmOf(runs), [runs]);
  const within = weeks.flat().flatMap((cell) => cell.runs);
  const failed = within.filter((run) => run.status === 'failed').length;
  const legend = WORST.slice().reverse().filter((status) => within.some((run) => run.status === status));
  return (
    <div>
      <p className="text-20 font-semibold">{plural(within.length, 'run')}</p>
      <p className="mt-1 text-13 text-muted">
        {failed ? `Over the last ${WEEKS} weeks, ${failed} did not work.` : within.length ? `Over the last ${WEEKS} weeks, every run finished.` : `Over the last ${WEEKS} weeks, nothing ran.`}
      </p>
      <div className="mt-4 flex gap-1">
        <div aria-hidden="true" className="grid grid-rows-7 gap-1 pr-1">
          {DAYS.map((word, index) => (
            <span key={index} className="flex h-3 items-center text-11 text-faint">
              {word}
            </span>
          ))}
        </div>
        {weeks.map((week) => (
          <div key={week[0]?.at} className="grid grid-rows-7 gap-1">
            {week.map((cell) => {
              const words = `${dateWords(cell.at)}: ${cell.runs.length ? `${plural(cell.runs.length, 'run')}, ${(cell.status ? (OUTCOME[cell.status]?.word ?? '') : '').toLowerCase()}` : 'no run'}`;
              const first = cell.runs[0];
              return first ? (
                <Tooltip key={cell.at} label={words}>
                  <button type="button" aria-label={words} onClick={() => onOpen(first)} className="relative size-3 rounded-[3px] transition-transform duration-(--duration-fast) ease-standard before:absolute before:-inset-0.5 hover:scale-125" style={{ backgroundColor: cell.status ? OUTCOME[cell.status]?.color : 'var(--fill-3)' }} />
                </Tooltip>
              ) : (
                <span key={cell.at} aria-hidden="true" className={cn('size-3 rounded-[3px]', cell.future ? '' : 'bg-fill-1')} />
              );
            })}
          </div>
        ))}
      </div>
      <div className="mt-4">
        <Legend items={legend.map((status) => ({ label: OUTCOME[status]?.word ?? '', color: OUTCOME[status]?.color ?? '' }))} />
      </div>
    </div>
  );
}
