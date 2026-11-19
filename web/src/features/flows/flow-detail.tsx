/**
 * One routine, read in five seconds: the sentence of what it does, the steps as a picture, when it
 * next runs, and what it really produced lately. Ask me first is one quiet line under the title.
 * Everything technical waits behind Technical details.
 */
import { Link, useParams } from '@tanstack/react-router';
import { ArrowLeft, ChevronRight, Clock, Lock, Pencil, Play, Workflow } from 'lucide-react';
import { useMemo, useState } from 'react';
import { useAccess, useFlows, useRoutineCatalog, useRoutineRuns, useSchedules, useWorkspaces } from '@/api/hooks';
import type { Flow, RoutineCatalog, RoutineRun, RoutineSendLimit } from '@/api/types';
import { BusinessMark } from '@/components/brand/identity';
import { SiteMark } from '@/components/brand/site-mark';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { CopyButton, Detail, Meter } from '@/components/ui/misc';
import { Status } from '@/components/ui/status';
import { StateGlyph } from '@/components/ui/state-badge';
import { EmptyState, QueryView, Skeleton } from '@/components/ui/states';
import { RoutineAskPanel } from '@/features/ask-first/ask-first';
import { cn } from '@/lib/cn';
import { dateTime, plural, timeAgo, usd } from '@/lib/format';
import { runStatus } from '@/lib/labels';
import { fileOf, findStep, TRIGGER_TARGET } from '@/lib/routines';
import { useTechnical } from '@/lib/technical';
import { EFFECT_WORDS } from '@/lib/vocabulary';
import { FlowMap } from './flow-map';
import { OutputCard } from './output-card';
import { RunDialog } from './run-dialog';
import { Rhythm } from './rhythm';
import { NEEDS_REVIEW_WORDS, routineParts, routineTitle, whenNext } from './routine-words';
import { RunSheet } from './run-sheet';
import { NextFire, PauseSwitch, ScheduleDialog, triggerOf } from './schedules';
import { useZones } from './use-zones';

const SHOWN_OUTPUTS = 3;

/** The headline: the routine's own sentence. Each phrase lights its step on the map, and back. */
function Sentence({ flow, catalog, lit, onLight }: { flow: Flow; catalog: RoutineCatalog | undefined; lit: string | null; onLight: (id: string | null) => void }) {
  const parts = routineParts(flow, catalog);
  if (!parts) return <h1 className="text-headline text-balance">{routineTitle(flow)}</h1>;
  return (
    <h1 className="max-w-4xl text-page-title-compact text-balance sm:text-[36px] sm:leading-[42px] sm:tracking-[-0.04em]">
      {parts.map((part, index) =>
        part.target ? (
          <span
            key={index}
            onPointerEnter={() => onLight(part.target ?? null)}
            onPointerLeave={() => onLight(null)}
            className={cn('rounded-sm transition-colors duration-(--duration-fast) ease-standard', lit === part.target ? 'bg-pressed' : '')}
          >
            {part.text}
          </span>
        ) : (
          <span key={index} className="text-faint">
            {part.text}
          </span>
        ),
      )}
    </h1>
  );
}

/** Where it stands, as one sentence. */
function Standing({ org, flow, onOpen }: { org: string; flow: Flow; onOpen: (id: string) => void }) {
  if (flow.stuck) {
    const [ws, lane] = (flow.stuck.lane ?? '').split('/');
    return (
      <div className="mt-4 flex flex-wrap items-center gap-x-3 gap-y-2 text-body text-pretty">
        <StateGlyph state="stuck" size={16} />
        <span className="min-w-0">Stopped at "{flow.stuck.label}". {flow.stuck.words}</span>
        {flow.stuck.fix && ws && lane ? (
          <Link to="/$org/lanes/$ws/$name" params={{ org, ws, name: lane }} className="link whitespace-nowrap">
            {flow.stuck.fix}
          </Link>
        ) : null}
        <button type="button" onClick={() => onOpen(flow.stuck?.run ?? '')} className="link whitespace-nowrap">
          See what happened
        </button>
      </div>
    );
  }
  const next = flow.enabled === false ? 'It is paused.' : flow.next_at ? `Next ${whenNext(flow.next_at)}.` : flow.trigger && flow.trigger.kind !== 'schedule' ? 'It runs when it is needed.' : null;
  return (
    <p className="mt-4 text-body text-pretty text-muted">
      {flow.waiting ? (
        <>
          <Link to="/$org/needs-you" params={{ org }} className="link">
            Waiting for you
          </Link>{' '}
          to read this week's email.{' '}
        </>
      ) : flow.latest ? (
        <>
          <span className="text-fg">{flow.latest.result}</span>, {timeAgo(flow.latest.at).toLowerCase()}.{' '}
        </>
      ) : (
        'It has not run yet. '
      )}
      {next}
      {flow.needs_review ? ` ${NEEDS_REVIEW_WORDS}` : null}
    </p>
  );
}

/** Daily limit, suppression and unsubscribe, said plainly. These are rules, not settings. */
function SendRules({ limits, flow }: { limits: RoutineSendLimit[]; flow: Flow }) {
  return (
    <section aria-label="Who it can reach" className="grid gap-10">
      {limits.map((limit) => {
        const label = findStep(flow.steps ?? [], limit.step)?.label ?? 'This step';
        return (
          <div key={limit.step} className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)] lg:gap-16">
            <div>
              <h2 className="text-heading">Who it can reach</h2>
              <p className="mt-1 text-body text-pretty text-muted">
                "{label.charAt(0).toUpperCase() + label.slice(1)}" only goes to <span className="text-fg">{limit.list ?? 'a list you choose'}</span>
                {limit.list_size ? ` (${limit.list_size.toLocaleString('en')} people)` : ''}. These always apply. They are not settings.
              </p>
              <div className="mt-4 flex items-center gap-3">
                <Meter value={limit.sent_today} max={limit.daily_limit} label="Sent today" className="w-40 shrink-0" />
                <span className="tabular text-secondary text-muted">
                  {limit.sent_today} of {limit.daily_limit} today
                </span>
              </div>
            </div>
            <ul className="grid content-start divide-y divide-line text-body">
              {[`Stops at ${limit.daily_limit} a day`, `Skips ${plural(limit.suppressed, 'person', 'people')} who asked not to hear from you`, 'Every email carries an unsubscribe link'].map((rule) => (
                <li key={rule} className="flex items-center gap-3 py-2">
                  <Lock aria-hidden="true" className="size-4 shrink-0 text-faint" />
                  {rule}
                </li>
              ))}
            </ul>
          </div>
        );
      })}
    </section>
  );
}

function Runs({ runs, onOpen }: { runs: RoutineRun[]; onOpen: (run: RoutineRun) => void }) {
  const [all, setAll] = useState(false);
  const listed = runs.slice(0, all ? 30 : 6);
  return (
    <div>
      <ul className="divide-y divide-line">
        {listed.map((run) => (
          <li key={run.id}>
            <button type="button" onClick={() => onOpen(run)} className="group/run -mx-2 flex w-[calc(100%+16px)] items-center gap-3 rounded-md px-2 py-2 text-left transition-colors duration-(--duration-fast) ease-standard hover:bg-hover">
              <span className="min-w-0 flex-1">
                <span className="block truncate text-body">{run.result}</span>
                <span className="tabular block truncate text-caption text-faint">{dateTime(run.started_at)} · {run.cause}</span>
              </span>
              <Status status={runStatus[run.status]} className="hidden sm:inline-flex" />
              <ChevronRight aria-hidden="true" className="size-4 text-faint" />
            </button>
          </li>
        ))}
      </ul>
      {runs.length > 6 ? (
        <Button variant="ghost" size="sm" className="mt-2" onClick={() => setAll(!all)}>
          {all ? 'Show fewer' : 'Show more runs'}
        </Button>
      ) : null}
    </div>
  );
}

function Technical({ flow }: { flow: Flow }) {
  const params = Object.entries(flow.params);
  const file = flow.trigger && flow.steps ? fileOf({ name: routineTitle(flow), workspace: flow.lane.split('/')[0] ?? null, trigger: flow.trigger, steps: flow.steps }) : null;
  return (
    <section aria-label="Technical details" className="grid gap-4 border-t border-line pt-6 lg:grid-cols-[20rem_minmax(0,1fr)]">
      <dl className="divide-y divide-line">
        <Detail label="Id"><span className="mono">{flow.id}</span></Detail>
        <Detail label="Account"><span className="mono">{flow.lane}</span></Detail>
        <Detail label="Effect">{EFFECT_WORDS[flow.effect].technical}</Detail>
        <Detail label="Version"><span className="tabular">{flow.version}</span></Detail>
        <Detail label="Last cost">{flow.last_run ? usd(flow.last_run.cost.usd) : '–'}</Detail>
        {params.map(([key, param]) => (
          <Detail key={key} label={key}>{`${param.type}${param.required ? ', required' : ''}`}</Detail>
        ))}
      </dl>
      {file ? (
        <div className="relative min-w-0">
          <div className="absolute top-2 right-2"><CopyButton text={file} label="Copy the file" /></div>
          <pre className="mono max-h-96 overflow-auto rounded-lg border border-line bg-inset p-4 text-caption">{file}</pre>
        </div>
      ) : null}
    </section>
  );
}

function Body({ org, flow }: { org: string; flow: Flow }) {
  const canOperate = useAccess(org).can('lanes.operate');
  const [technical] = useTechnical();
  const catalog = useRoutineCatalog(org).data;
  const runs = useRoutineRuns(org, flow.id);
  const schedules = useSchedules(org).data?.filter((item) => item.flow === flow.id) ?? [];
  const { zoneOf } = useZones(org);
  const workspace = useWorkspaces(org).data?.find((item) => item.id === flow.lane.split('/')[0]);
  const [running, setRunning] = useState(false);
  const [scheduling, setScheduling] = useState(false);
  const [lit, setLit] = useState<string | null>(null);
  const [open, setOpen] = useState<string | null>(null);

  const list = runs.data ?? [];
  const newest = list.find((run) => run.steps.length);
  const locked = useMemo(() => new Set((flow.always_asks_steps ?? []).filter((item) => item.because !== 'ask_step').map((item) => item.step)), [flow.always_asks_steps]);
  const made = list.filter((run) => run.output || run.status === 'failed').slice(0, SHOWN_OUTPUTS);
  const site = flow.sites?.at(-1) ?? flow.lane.split('/')[1] ?? '';
  const limits = flow.send_limits ?? [];
  const opened = list.find((run) => run.id === open) ?? null;

  return (
    <div className="grid gap-10 md:gap-12">
      <div>
        <Link to="/$org/flows" params={{ org }} className="mb-6 inline-flex items-center gap-1 text-secondary text-muted hover:text-fg">
          <ArrowLeft className="size-3" /> Routines
        </Link>
        <header>
          <div className="mb-3 flex items-center justify-between gap-4">
            <p className="flex min-w-0 items-center gap-2 text-body text-muted">
              {workspace ? <BusinessMark name={workspace.name} color={workspace.color} size={20} /> : <SiteMark site={site} size={20} label={false} />}
              <span className="truncate">{[workspace?.name, routineTitle(flow)].filter(Boolean).join(' · ')}</span>
            </p>
            {canOperate ? (
              <span className="flex shrink-0 gap-2">
                {flow.steps ? (
                  <Button asChild>
                    <Link to="/$org/flows/$flow/edit" params={{ org, flow: flow.id }}>
                      <Pencil className="size-3" />
                      <span className="hidden sm:inline">Change steps</span>
                      <span className="sr-only sm:hidden">Change steps</span>
                    </Link>
                  </Button>
                ) : null}
                <Button icon={<Play className="size-3" />} onClick={() => setRunning(true)}>
                  Run
                </Button>
              </span>
            ) : null}
          </div>
          <Sentence flow={flow} catalog={catalog} lit={lit} onLight={setLit} />
          <Standing org={org} flow={flow} onOpen={setOpen} />
        </header>
        <div className="mt-6">
          <RoutineAskPanel org={org} flow={flow} />
          {schedules.map((item) => (
            <div key={item.id} className="flex items-center gap-3 border-b border-line py-3">
              <Clock aria-hidden="true" className="size-4 shrink-0 text-faint" />
              <p className="min-w-0 flex-1 text-secondary text-muted">
                <span className="text-label text-fg">{triggerOf(item)}</span> <NextFire schedule={item} zone={zoneOf(item)} />
              </p>
              {canOperate ? <PauseSwitch org={org} schedule={item} /> : null}
            </div>
          ))}
          {!flow.trigger && canOperate && !schedules.length ? (
            <Button variant="ghost" size="sm" icon={<Clock className="size-3" />} className="mt-3" onClick={() => setScheduling(true)}>
              Run it on a schedule
            </Button>
          ) : null}
          <ScheduleDialog org={org} routines={[flow]} open={scheduling} onOpenChange={setScheduling} />
        </div>
      </div>

      {flow.trigger && flow.steps ? (
        <FlowMap trigger={flow.trigger} steps={flow.steps} catalog={catalog} run={newest?.steps} next={flow.next_at ? `Next ${whenNext(flow.next_at)}` : null} locked={locked} lit={lit === TRIGGER_TARGET ? 'trigger' : lit} onLight={setLit} />
      ) : (
        <p className="text-body text-pretty text-muted">Its steps can't be shown here: the computer it runs on keeps it as a recording. Its runs show below. Updating that computer lets you see and change its steps here.</p>
      )}

      <section aria-label="What it made lately">
        <h2 className="mb-4 text-heading">What it made lately</h2>
        <QueryView query={runs} skeleton={<div className="grid gap-4 md:grid-cols-4">{[0, 1, 2, 3].map((item) => <Skeleton key={item} className="h-56 rounded-(--radius-card)" />)}</div>}>
          {() =>
            made.length ? (
              <div className="scrollbar-none -mx-4 flex snap-x gap-3 overflow-x-auto px-4 pb-1 sm:-mx-6 sm:px-6 lg:mx-0 lg:grid lg:grid-cols-4 lg:gap-4 lg:px-0">
                {made.map((run, index) => (
                  <div key={run.id} className={cn('w-72 shrink-0 snap-start lg:w-auto', index === 0 && 'lg:col-span-2')}>
                    <OutputCard run={run} site={site} lead={index === 0} onOpen={() => setOpen(run.id)} />
                  </div>
                ))}
              </div>
            ) : (
              <p className="text-body text-muted">Nothing yet. What it makes shows here after its first run.</p>
            )
          }
        </QueryView>
      </section>

      {list.length ? (
        <section aria-label="Every run" className="grid gap-8 lg:grid-cols-[auto_minmax(0,1fr)] lg:gap-16">
          <Rhythm runs={list} onOpen={(run) => setOpen(run.id)} />
          <Runs runs={list} onOpen={(run) => setOpen(run.id)} />
        </section>
      ) : null}

      {limits.length ? <SendRules limits={limits} flow={flow} /> : null}
      {technical ? <Technical flow={flow} /> : null}

      <RunDialog org={org} flow={running ? flow : null} onClose={() => setRunning(false)} />
      <RunSheet org={org} flow={flow} run={opened} site={site} onClose={() => setOpen(null)} />
    </div>
  );
}

export function FlowDetail() {
  const { org, flow: id } = useParams({ from: '/$org/flows/$flow' });
  const flows = useFlows(org);
  return (
    <QueryView
      query={flows}
      card
      skeleton={
        <div className="grid gap-6">
          <Skeleton className="h-24 w-3/4" />
          <Skeleton className="h-40 rounded-(--radius-card)" />
          <Skeleton className="h-56 rounded-(--radius-card)" />
        </div>
      }
    >
      {(items) => {
        const flow = items.find((item) => item.id === id);
        if (flow) return <Body org={org} flow={flow} />;
        return (
          <Card>
            <EmptyState
              icon={<Workflow />}
              title="This routine is not here"
              description="It may have been removed on the computer, or it belongs to a workspace you cannot see."
              action={
                <Link to="/$org/flows" params={{ org }} className="link text-body">
                  Back to routines
                </Link>
              }
            />
          </Card>
        );
      }}
    </QueryView>
  );
}
