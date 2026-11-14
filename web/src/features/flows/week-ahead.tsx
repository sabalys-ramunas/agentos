/**
 * The next seven days, a column a day: what is set to run, at what time, by its site and its name.
 * Worked out from each schedule the computer keeps, which covers a recorded routine and a routine's own
 * time alike. A routine something outside starts has no time to show, so it is named underneath rather
 * than left out. On a phone the days stack, and a day with nothing in it is skipped.
 */
import { Link } from '@tanstack/react-router';
import { Plus } from 'lucide-react';
import { useMemo } from 'react';
import type { Flow, Schedule } from '@/api/types';
import { Button } from '@/components/ui/button';
import { SiteMark } from '@/components/brand/site-mark';
import { cn } from '@/lib/cn';
import { projectFires } from '@/lib/cron';
import { clock, plural } from '@/lib/format';
import { routineName } from '@/lib/principal';
import { routineTitle } from './routine-words';
import { NO_ROUTINE } from './schedule-timeline';

const DAY = 86_400_000;

export interface Fire {
  at: number;
  /** The routine's name, or what a schedule with no routine is called. */
  title: string;
  /** The routine's id, when a schedule starts one. */
  flow: string | null;
  /** Where it acts: the routine's last site, or nothing when the computer keeps no routine for it. */
  site: string;
}

const siteOf = (flow: Flow | undefined) => (flow ? (flow.sites?.at(-1) ?? flow.lane.split('/')[1] ?? '') : '');

/**
 * Every fire in the next seven days. A schedule the computer keeps is the source: a recorded routine's, and a
 * routine's own time. A routine built in the app whose time is not on a schedule yet is read from its trigger.
 */
export function weekOf(flows: Flow[], schedules: Schedule[], now: number, zone: string): { at: number; fires: Fire[] }[] {
  const start = new Date(now);
  start.setHours(0, 0, 0, 0);
  const days = Array.from({ length: 7 }, (_, index) => {
    const at = new Date(start);
    at.setDate(at.getDate() + index);
    return { at: at.getTime(), fires: [] as Fire[] };
  });
  const byId = new Map(flows.map((flow) => [flow.id, flow]));
  const titles = new Map(flows.map((flow) => [flow.id, routineTitle(flow)]));
  const timed = new Set<string>();
  const lines: { cron: string; flow: string; title: string; site: string }[] = [];
  for (const schedule of schedules) {
    if (schedule.enabled === false || !schedule.cron) continue;
    const flow = schedule.flow || null;
    if (flow) timed.add(flow);
    lines.push({
      cron: schedule.cron,
      flow: flow ?? '',
      title: flow ? (titles.get(flow) ?? routineName(flow)) : NO_ROUTINE,
      site: siteOf(flow ? byId.get(flow) : undefined),
    });
  }
  for (const flow of flows) {
    if (flow.enabled === false || !flow.trigger?.cron || timed.has(flow.id)) continue;
    lines.push({ cron: flow.trigger.cron, flow: flow.id, title: routineTitle(flow), site: siteOf(flow) });
  }
  for (const line of lines) {
    for (const at of projectFires(line.cron, now, start.getTime() + 7 * DAY, zone, 40) ?? []) {
      const day = days.findLast((item) => item.at <= at);
      day?.fires.push({ at, title: line.title, flow: line.flow || null, site: line.site });
    }
  }
  for (const day of days) day.fires.sort((a, b) => a.at - b.at);
  return days;
}


function dayName(at: number, index: number) {
  if (index === 0) return 'Today';
  if (index === 1) return 'Tomorrow';
  return new Date(at).toLocaleDateString('en-GB', { weekday: 'long' });
}

export function WeekAhead({ org, flows, schedules, onNew }: { org: string; flows: Flow[]; schedules: Schedule[]; onNew?: () => void }) {
  const days = useMemo(() => weekOf(flows, schedules, Date.now(), Intl.DateTimeFormat().resolvedOptions().timeZone), [flows, schedules]);
  const total = days.reduce((sum, day) => sum + day.fires.length, 0);
  const onDemand = flows.filter((flow) => flow.enabled !== false && flow.trigger && !flow.trigger.cron);
  return (
    <section aria-label="The week ahead" className="grid gap-4">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-20 font-semibold">The week ahead</h2>
          <p className="mt-1 text-14 text-muted">{plural(total, 'run')} planned in the next 7 days.</p>
        </div>
        {onNew ? (
          <Button icon={<Plus className="size-3" />} onClick={onNew}>
            New schedule
          </Button>
        ) : null}
      </div>
      <ol className="grid gap-6 lg:grid-cols-7 lg:gap-px lg:overflow-hidden lg:rounded-(--radius-card) lg:border lg:border-line lg:bg-line">
        {days.map((day, index) => (
          <li key={day.at} className={cn('min-w-0 bg-bg lg:min-h-64 lg:p-3', !day.fires.length && 'max-lg:hidden')}>
            <p className="tabular text-13 font-medium">
              {dayName(day.at, index)} <span className="font-normal text-faint">{new Date(day.at).getDate()}</span>
            </p>
            {day.fires.length ? null : <p className="mt-2 text-12 text-faint">{index === 0 ? 'Nothing more today' : 'Nothing planned'}</p>}
            <ul className="mt-2 grid gap-1 max-lg:divide-y max-lg:divide-line">
              {day.fires.map((fire) => {
                const body = (
                  <>
                    <SiteMark site={fire.site} size={16} label={false} className="shrink-0 lg:translate-y-0.5" />
                    <span className="flex min-w-0 flex-1 items-baseline justify-between gap-2 lg:block">
                      <span className="block truncate text-13 lg:text-12">{fire.title}</span>
                      <span className="mono tabular block shrink-0 text-12 text-faint">{clock(fire.at)}</span>
                    </span>
                  </>
                );
                const row = '-mx-1 flex items-center gap-2 rounded-md p-1 transition-colors duration-(--duration-fast) ease-standard lg:items-start';
                return (
                  <li key={`${fire.flow ?? fire.title}-${fire.at}`}>
                    {fire.flow ? (
                      <Link to="/$org/flows/$flow" params={{ org, flow: fire.flow }} className={cn(row, 'hover:bg-fill-1')}>
                        {body}
                      </Link>
                    ) : (
                      <span className={row}>{body}</span>
                    )}
                  </li>
                );
              })}
            </ul>
          </li>
        ))}
      </ol>
      {onDemand.length ? <p className="text-13 text-pretty text-muted">{plural(onDemand.length, 'routine runs', 'routines run')} when something happens, not at a set time: {onDemand.map(routineTitle).join(', ')}.</p> : null}
    </section>
  );
}
