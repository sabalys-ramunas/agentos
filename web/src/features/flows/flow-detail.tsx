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
import { usePageCrumb } from '@/components/shell/crumb';
import { SiteMark } from '@/components/brand/site-mark';
import { Button } from '@/components/ui/button';
import { Card, CardHeader } from '@/components/ui/card';
import { CopyButton, Detail, Meter, PageHeader } from '@/components/ui/misc';
import { Status } from '@/components/ui/status';
import { Banner, EmptyState, QueryView, Skeleton } from '@/components/ui/states';
import { RoutineAskPanel } from '@/features/ask-first/ask-first';
import { cn } from '@/lib/cn';
import { dateTime, monthDay, plural, timeAgo, usd } from '@/lib/format';
import { runStatus } from '@/lib/labels';
import { fileOf, findStep, TRIGGER_TARGET } from '@/lib/routines';
import { siteName } from '@/lib/sites';
import { useTechnical } from '@/lib/technical';
import { EFFECT_WORDS } from '@/lib/vocabulary';
import { StepFlow } from './flow-map';
import { RunDialog } from './run-dialog';
import { Rhythm } from './rhythm';
import { NEEDS_REVIEW_WORDS, routineParts, routineTitle, whenNext } from './routine-words';
import { RunSheet } from './run-sheet';
import { NextFire, PauseSwitch, ScheduleDialog, triggerOf } from './schedules';
import { useZones } from './use-zones';

const SHOWN_OUTPUTS = 4;

/** The lede: the routine's own sentence. Each phrase lights its step in the flow, and back. */
function Sentence({ flow, catalog, lit, onLight }: { flow: Flow; catalog: RoutineCatalog | undefined; lit: string | null; onLight: (id: string | null) => void }) {
  const parts = routineParts(flow, catalog);
  if (!parts) return null;
  return (
    <span className="text-pretty text-fg">
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
          <span key={index} className="text-muted">
            {part.text}
          </span>
        ),
      )}
    </span>
  );
}

/** Where it stopped, as a red banner with the way to look. */
function Stopped({ org, flow, onOpen }: { org: string; flow: Flow; onOpen: (id: string) => void }) {
  if (!flow.stuck) return null;
  const [ws, lane] = (flow.stuck.lane ?? '').split('/');
  return (
    <Banner
      tone="bad"
      action={
        <>
          {flow.stuck.fix && ws && lane ? (
            <Button asChild size="sm">
              <Link to="/$org/lanes/$ws/$name" params={{ org, ws, name: lane }}>
                {flow.stuck.fix}
              </Link>
            </Button>
          ) : null}
          <Button size="sm" onClick={() => onOpen(flow.stuck?.run ?? '')}>
            See what happened
          </Button>
        </>
      }
    >
      Stopped at "{flow.stuck.label}". {flow.stuck.words}
    </Banner>
  );
}

/** How it stands when nothing is stuck, as one quiet sentence. */
function Standing({ org, flow }: { org: string; flow: Flow }) {
  const next = flow.enabled === false ? 'It is paused.' : flow.next_at ? `Next ${whenNext(flow.next_at)}.` : flow.trigger && flow.trigger.kind !== 'schedule' ? 'It runs when it is needed.' : null;
  return (
    <span className="text-body text-pretty text-muted">
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
    </span>
  );
}

const money = (amount: number, currency = 'EUR') => new Intl.NumberFormat('en', { style: 'currency', currency, maximumFractionDigits: amount % 1 ? 2 : 0 }).format(amount);

/** One thing it made, as a row: the amount when there is one, what it was, and how we know. */
function MadeRow({ run, onOpen }: { run: RoutineRun; onOpen: () => void }) {
  const failed = run.status === 'failed';
  const output = run.output;
  const what = failed ? 'It did not finish' : (output?.title || run.result || '');
  const amount = output && output.amount !== undefined && output.amount !== null ? money(output.amount, output.currency ?? 'EUR') : null;
  const detail = output ? [output.who, output.body].filter(Boolean).join(' · ') : null;
  return (
    <li>
      <button
        type="button"
        onClick={onOpen}
        className="focus-inset flex w-full items-start gap-4 px-5 py-4 text-left transition-colors duration-(--duration-fast) hover:bg-hover"
      >
        {amount ? <span className="shrink-0 text-card-title tabular">{amount}</span> : null}
        <span className="grid min-w-0 flex-1 gap-1">
          <span className="line-clamp-2 text-label text-fg">{what}</span>
          {failed ? <span className="line-clamp-2 text-secondary text-muted">{run.result}</span> : detail ? <span className="line-clamp-2 text-secondary text-muted">{detail}</span> : null}
          <span className="text-caption text-faint tabular">
            {monthDay(run.started_at)}
            {!failed && output?.proof ? ` · Checked on ${siteName(output.proof.site)}` : ''}
          </span>
        </span>
        {failed ? (
          <Status chip status={{ label: 'Did not work', tone: 'bad' }} />
        ) : output?.proof ? (
          <Status chip status={{ label: 'Checked', tone: 'ok' }} />
        ) : (
          <Status chip status={{ label: 'Not checked', tone: 'idle' }} />
        )}
      </button>
    </li>
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
      <ul className="divide-y divide-line border-t border-line">
        {listed.map((run) => (
          <li key={run.id}>
            <button type="button" onClick={() => onOpen(run)} className="group/run focus-inset flex w-full items-center gap-3 px-5 py-3 text-left transition-colors duration-(--duration-fast) ease-standard hover:bg-hover">
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
        <Button variant="ghost" size="sm" className="mx-3 mb-3" onClick={() => setAll(!all)}>
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

  usePageCrumb(routineTitle(flow), { mono: false });
  const sentence = <Sentence flow={flow} catalog={catalog} lit={lit} onLight={setLit} />;

  return (
    <>
      <Link to="/$org/flows" params={{ org }} className="mb-6 inline-flex items-center gap-1 rounded-sm text-secondary text-muted hover:text-fg">
        <ArrowLeft className="size-3.5" /> Routines
      </Link>
      <PageHeader
        lead={workspace ? <BusinessMark name={workspace.name} color={workspace.color} size={40} /> : <SiteMark site={site} size={40} tile label={false} />}
        eyebrow={workspace ? <span className="text-body text-muted">{workspace.name}</span> : undefined}
        title={routineTitle(flow)}
        description={
          <>
            {routineParts(flow, catalog) ? sentence : null}
            {flow.stuck ? null : <Standing org={org} flow={flow} />}
          </>
        }
        actions={
          canOperate ? (
            <>
              {flow.steps ? (
                <Button asChild>
                  <Link to="/$org/flows/$flow/edit" params={{ org, flow: flow.id }}>
                    <Pencil aria-hidden="true" className="size-4" />
                    Change steps
                  </Link>
                </Button>
              ) : null}
              <Button variant="primary" icon={<Play className="size-4" />} onClick={() => setRunning(true)}>
                Run now
              </Button>
            </>
          ) : undefined
        }
      />
      <div className="grid gap-6">
        <Stopped org={org} flow={flow} onOpen={setOpen} />

        <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_360px]">
          <Card className="min-w-0">
            <CardHeader title="The steps" meta={schedules.length ? plural(schedules.length, 'schedule') : undefined} />
            <div className="-mt-px px-5 empty:hidden">
              <RoutineAskPanel org={org} flow={flow} />
            </div>
            {schedules.map((item) => (
              <div key={item.id} className="flex items-center gap-3 border-b border-line px-5 py-3">
                <Clock aria-hidden="true" className="size-4 shrink-0 text-faint" />
                <p className="min-w-0 flex-1 text-secondary text-muted">
                  <span className="text-label text-fg">{triggerOf(item)}</span> <NextFire schedule={item} zone={zoneOf(item)} />
                </p>
                {canOperate ? <PauseSwitch org={org} schedule={item} /> : null}
              </div>
            ))}
            <div className="p-5">
              {flow.trigger && flow.steps ? (
                <StepFlow trigger={flow.trigger} steps={flow.steps} catalog={catalog} run={newest?.steps} next={flow.next_at ? `Next ${whenNext(flow.next_at)}` : null} locked={locked} lit={lit === TRIGGER_TARGET ? 'trigger' : lit} onLight={setLit} />
              ) : (
                <p className="text-body text-pretty text-muted">Its steps can't be shown here: the computer it runs on keeps it as a recording. Its runs show beside them. Updating that computer lets you see and change its steps here.</p>
              )}
              {!flow.trigger && canOperate && !schedules.length ? (
                <Button size="sm" icon={<Clock className="size-4" />} className="mt-4" onClick={() => setScheduling(true)}>
                  Run it on a schedule
                </Button>
              ) : null}
            </div>
            <ScheduleDialog org={org} routines={[flow]} open={scheduling} onOpenChange={setScheduling} />
          </Card>

          <div className="grid min-w-0 gap-6">
            <Card>
              <CardHeader title={<span id="made-lately">What it made lately</span>} />
              <QueryView query={runs} skeleton={<div className="grid gap-3 p-5">{[0, 1, 2].map((item) => <Skeleton key={item} className="h-14" />)}</div>}>
                {() =>
                  made.length ? (
                    <ul aria-labelledby="made-lately" className="divide-y divide-line">
                      {made.map((run) => (
                        <MadeRow key={run.id} run={run} onOpen={() => setOpen(run.id)} />
                      ))}
                    </ul>
                  ) : (
                    <p className="px-5 py-4 text-body text-muted">Nothing yet. What it makes shows here after its first run.</p>
                  )
                }
              </QueryView>
            </Card>
            {list.length ? (
              <Card>
                <section aria-label="Every run">
                  <div className="p-5">
                    <Rhythm runs={list} onOpen={(run) => setOpen(run.id)} />
                  </div>
                  <Runs runs={list} onOpen={(run) => setOpen(run.id)} />
                </section>
              </Card>
            ) : null}
          </div>
        </div>

        {limits.length ? <SendRules limits={limits} flow={flow} /> : null}
        {technical ? <Technical flow={flow} /> : null}
      </div>

      <RunDialog org={org} flow={running ? flow : null} onClose={() => setRunning(false)} />
      <RunSheet org={org} flow={flow} run={opened} site={site} onClose={() => setOpen(null)} />
    </>
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
        <div role="status" aria-label="Loading the routine" className="grid gap-6">
          <div className="grid grid-cols-[auto_minmax(0,1fr)] items-start gap-x-4">
            <Skeleton className="size-10 rounded-lg" />
            <div className="grid gap-3">
              <Skeleton className="h-4 w-24" />
              <Skeleton className="h-8 w-72 max-w-full" />
              <Skeleton className="h-5 w-full max-w-2xl" />
            </div>
          </div>
          <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_360px]">
            <Skeleton className="h-96 rounded-xl" />
            <div className="grid gap-6">
              <Skeleton className="h-64 rounded-xl" />
              <Skeleton className="h-48 rounded-xl" />
            </div>
          </div>
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
