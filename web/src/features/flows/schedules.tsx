import { Link } from '@tanstack/react-router';
import { Trash2 } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';
import { useCreateSchedule, useSetScheduleEnabled } from '@/api/hooks';
import type { Flow, Schedule } from '@/api/types';
import { Button, IconButton } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Dialog } from '@/components/ui/dialog';
import { Field, Input, Textarea } from '@/components/ui/field';
import { Select } from '@/components/ui/select';
import { Status } from '@/components/ui/status';
import { mainCell, Table, Td, Th, Tr } from '@/components/ui/table';
import { Switch } from '@/components/ui/toggle';
import { cn } from '@/lib/cn';
import { describeCron } from '@/lib/cron';
import { runStatus } from '@/lib/labels';
import type { StatusLabel } from '@/lib/labels';
import { routineName } from '@/lib/principal';
import { useTechnical } from '@/lib/technical';
import { fireLabel, localZone, zonesDiffer } from '@/lib/zone';
import { paramsOf } from './run-dialog';
import { routineTitle } from './routine-words';
import { isPaused, NO_ROUTINE, nextFire, ScheduleTimeline } from './schedule-timeline';
import { useZones } from './use-zones';

const PAUSED: StatusLabel = { label: 'Paused', tone: 'idle' };

/** When a routine runs, as the two things a person chooses: the days, and the time. */
export const RHYTHMS = [
  { value: '1-5', label: 'Every weekday' },
  { value: '*', label: 'Every day' },
  { value: '1', label: 'Every Monday' },
  { value: '2', label: 'Every Tuesday' },
  { value: '3', label: 'Every Wednesday' },
  { value: '4', label: 'Every Thursday' },
  { value: '5', label: 'Every Friday' },
];

/** A cron line from a day rule and a clock time ("09:00"). */
export const cronOf = (days: string, time: string) => {
  const [hour = '9', minute = '0'] = time.split(':');
  return `${Number(minute)} ${Number(hour)} * * ${days}`;
};

/**
 * A schedule for a recorded routine, which has no time of its own (a routine built in the app sets its time
 * in the routine). The cron line is the technical part, so it shows only under Technical details.
 */
export function ScheduleDialog({ org, routines, initial = null, open, onOpenChange }: { org: string; routines: Flow[]; initial?: string | null; open: boolean; onOpenChange: (open: boolean) => void }) {
  const create = useCreateSchedule(org);
  const [technical] = useTechnical();
  const [picked, setPicked] = useState<string | null>(null);
  const [days, setDays] = useState('1-5');
  const [time, setTime] = useState('09:00');
  const [line, setLine] = useState<string | null>(null);
  const [deadman, setDeadman] = useState('30');
  const [values, setValues] = useState<Record<string, string>>({});
  const routine = routines.find((item) => item.id === (picked ?? initial)) ?? routines[0] ?? null;
  const cron = line ?? cronOf(days, time);
  const valid = cron.trim().split(/\s+/).length === 5;
  const missing = Object.entries(routine?.params ?? {}).some(([key, param]) => param.required && !values[key]?.trim());

  const submit = () => {
    if (!routine) return;
    create.mutate(
      { flow: routine.id, cron, params: paramsOf(routine.params, values), deadman_minutes: deadman ? Number(deadman) : null },
      {
        onSuccess: () => {
          toast('Schedule created');
          onOpenChange(false);
        },
      },
    );
  };

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title="New schedule"
      description="The computer keeps the schedule, so it runs even when you are away."
      footer={
        <>
          <Button onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button variant="primary" disabled={!routine || !valid || missing} loading={create.isPending} onClick={submit}>
            Create schedule
          </Button>
        </>
      }
    >
      <div className="grid gap-4">
        {routines.length ? (
          <Field label="Routine">
            <Select
              value={routine?.id ?? ''}
              onValueChange={(id) => {
                setPicked(id);
                setValues({});
              }}
              options={routines.map((item) => ({ value: item.id, label: routineTitle(item) }))}
            />
          </Field>
        ) : (
          <p className="text-14 text-pretty text-muted">A routine you build sets its time in the routine. A recording takes a schedule here.</p>
        )}
        <div className="grid grid-cols-[minmax(0,1fr)_7rem] gap-2">
          <Field label="When">
            <Select value={days} onValueChange={setDays} options={RHYTHMS.some((item) => item.value === days) ? RHYTHMS : [...RHYTHMS, { value: days, label: describeCron(cron) }]} />
          </Field>
          <Field label="At">
            <Input type="time" value={time} onChange={(event) => setTime(event.target.value)} />
          </Field>
        </div>
        <p className="text-13 text-faint">{valid ? describeCron(cron) : 'Choose the days and the time.'}</p>
        {technical ? (
          <Field label="Cron line" hint="Five fields: minute, hour, day, month, weekday.">
            <Input mono value={cron} onChange={(event) => setLine(event.target.value)} autoComplete="off" />
          </Field>
        ) : null}
        {Object.entries(routine?.params ?? {}).map(([key, param]) => (
          <Field key={key} label={`${param.label ?? key}${param.required ? '' : ' (optional)'}`}>
            {param.type === 'number' ? (
              <Input type="number" value={values[key] ?? ''} onChange={(event) => setValues({ ...values, [key]: event.target.value })} />
            ) : (
              <Textarea value={values[key] ?? ''} onChange={(event) => setValues({ ...values, [key]: event.target.value })} />
            )}
          </Field>
        ))}
        <Field label="Alert if it has not run for (minutes)" hint="Leave empty for no alert.">
          <Input type="number" min={1} value={deadman} onChange={(event) => setDeadman(event.target.value)} />
        </Field>
      </div>
    </Dialog>
  );
}

/** Told apart from "on" by more than the switch, because a viewer has no switch to read. */
export function NextFire({ schedule, zone }: { schedule: Schedule; zone: string }) {
  if (isPaused(schedule)) return <Status status={PAUSED} />;
  if (!schedule.cron) return <span>On change</span>;
  const next = nextFire(schedule);
  if (!next) return <span className="text-faint">Not reported yet</span>;
  const at = Date.parse(next);
  const local = localZone();
  return (
    <span className="tabular">
      {fireLabel(at, zone)}
      {zonesDiffer(zone, local, at) ? <span className="block text-12 text-faint">Your time: {fireLabel(at, local)}</span> : null}
    </span>
  );
}

export const triggerOf = (schedule: Schedule) => (schedule.cron ? describeCron(schedule.cron) : `When: ${schedule.watch ?? ''}`);

/**
 * Optimistic, because the node keeps the schedule and a failure rolls it straight back. A routine's own
 * schedule switches its routine, so the two never disagree.
 */
export function PauseSwitch({ org, schedule }: { org: string; schedule: Schedule }) {
  const set = useSetScheduleEnabled(org);
  return (
    <Switch
      aria-label={`${schedule.flow || 'This schedule'} runs on schedule`}
      checked={!isPaused(schedule)}
      onCheckedChange={(enabled) =>
        set.mutate({ id: schedule.id, enabled }, { onSuccess: () => toast(enabled ? 'Schedule resumed' : 'Schedule paused') })
      }
    />
  );
}

interface TableProps {
  org: string;
  schedules: Schedule[];
  titles: ReadonlyMap<string, string>;
  zoneOf: (schedule: Schedule) => string;
  canOperate: boolean;
  onDelete: (schedule: Schedule) => void;
  active: string | null;
  onActive: (id: string | null) => void;
}

/** The routine a schedule starts, by its name. A schedule with no routine says so, and has no page to open. */
export const titleOfSchedule = (schedule: Schedule, titles: ReadonlyMap<string, string>) => (schedule.flow ? titles.get(schedule.flow) ?? routineName(schedule.flow) : NO_ROUTINE);

function ScheduleTable({ org, schedules, titles, zoneOf, canOperate, onDelete, active, onActive }: TableProps) {
  return (
    <Table>
      <thead className="hidden md:table-header-group">
        <tr>
          <Th>Routine</Th>
          <Th>Trigger</Th>
          <Th className="w-44">Next fire</Th>
          <Th className="hidden w-32 lg:table-cell">Last run</Th>
          <Th className="hidden w-24 lg:table-cell">Alert after</Th>
          <Th className="w-24">
            <span className="sr-only">Actions</span>
          </Th>
        </tr>
      </thead>
      <tbody>
        {schedules.map((item) => {
          const trigger = triggerOf(item);
          const title = titleOfSchedule(item, titles);
          return (
            <Tr
              key={item.id}
              className={cn('hover:bg-fill-1', active === item.id && 'bg-fill-1')}
              onPointerEnter={() => onActive(item.id)}
              onPointerLeave={() => onActive(null)}
            >
              <Td className={mainCell}>
                {item.flow ? (
                  <Link to="/$org/flows/$flow" params={{ org, flow: item.flow }} className="block truncate text-14 hover:underline">
                    {title}
                  </Link>
                ) : (
                  <span className="block truncate text-14 text-muted">{title}</span>
                )}
                {item.routine ? <span className="block truncate text-12 text-faint">Set in the routine</span> : null}
                <div className="text-12 text-faint md:hidden">
                  <p className="truncate">{trigger}</p>
                  <NextFire schedule={item} zone={zoneOf(item)} />
                </div>
              </Td>
              <Td className="hidden text-13 text-muted md:table-cell">
                <span title={item.cron ?? undefined}>{trigger}</span>
              </Td>
              <Td className="hidden text-13 text-muted md:table-cell">
                <NextFire schedule={item} zone={zoneOf(item)} />
              </Td>
              <Td className="hidden lg:table-cell">
                {item.last_run ? <Status status={runStatus[item.last_run.status]} /> : <span className="text-13 text-faint">Not run yet</span>}
              </Td>
              <Td className="tabular hidden text-13 text-muted lg:table-cell">{item.deadman_minutes ? `${item.deadman_minutes} min` : '–'}</Td>
              <Td className="pr-4 md:pr-6">
                {canOperate ? (
                  <span className="flex items-center justify-end gap-2">
                    <PauseSwitch org={org} schedule={item} />
                    <IconButton label={`Delete schedule for ${title}`} size="sm" onClick={() => onDelete(item)}>
                      <Trash2 className="size-4" />
                    </IconButton>
                  </span>
                ) : null}
              </Td>
            </Tr>
          );
        })}
      </tbody>
    </Table>
  );
}

export function SchedulesPanel({ org, schedules, titles, canOperate, onDelete }: { org: string; schedules: Schedule[]; titles: ReadonlyMap<string, string>; canOperate: boolean; onDelete: (schedule: Schedule) => void }) {
  const { zoneOf, fallback } = useZones(org);
  const [active, setActive] = useState<string | null>(null);
  return (
    <div className="grid gap-6">
      <Card className="p-4 md:p-6">
        <ScheduleTimeline org={org} schedules={schedules} zoneOf={zoneOf} fallback={fallback} active={active} onActive={setActive} titles={titles} />
      </Card>
      <Card className="overflow-hidden">
        <ScheduleTable org={org} schedules={schedules} titles={titles} zoneOf={zoneOf} canOperate={canOperate} onDelete={onDelete} active={active} onActive={setActive} />
      </Card>
    </div>
  );
}
