/**
 * Settings, Workers. An agent says what to do, and a worker does the steps on a person's own computer
 * with an AI model that costs far less. The page answers in the order a person asks: is it on, what
 * did it do, did it get stuck, is it inside its limits. Then where it runs and who does each job.
 * What it actually did stays in History, on the account, and in Needs you when it asks. Concept stage:
 * answered by the mock only.
 */
import { Link, useParams } from '@tanstack/react-router';
import { ArrowRight, ChevronDown, Lock, Monitor, MousePointerClick, Plus } from 'lucide-react';
import { useState } from 'react';
import { useAccess, useBrains, useSetWorkerJob, useSetWorkerPlace, useWorkers, useWorkspaces } from '@/api/hooks';
import { isCancelled } from '@/api/http';
import type { Brain, WorkerAssignment, WorkerOverview, WorkerPlace, WorkerThing } from '@/api/types';
import { AgentMark } from '@/components/brand/identity';
import { SiteMark } from '@/components/brand/site-mark';
import { Button } from '@/components/ui/button';
import { Card, Section } from '@/components/ui/card';
import { Select } from '@/components/ui/select';
import { StateBadge } from '@/components/ui/state-badge';
import { EmptyState, QueryView, Skeleton } from '@/components/ui/states';
import { Status } from '@/components/ui/status';
import { Switch } from '@/components/ui/toggle';
import { cn } from '@/lib/cn';
import { computerName } from '@/lib/computers';
import { timeAgo } from '@/lib/format';
import { principalName } from '@/lib/principal';
import { useTechnical } from '@/lib/technical';
import { accountName } from '@/features/home/words';
import { SettingsHeader } from '@/features/settings/page';
import { LimitsPanel } from './limits';
import { StepLine } from './step-line';
import { TurnOnDialog } from './turn-on';
import { JOB_NOTE, jobWord, laterFacts, NEVER, placeAsks, placeSentence, serviceName, servicesFor, workerAnswer, WORKER_NAME } from './words';

const EXAMPLE: WorkerThing = {
  id: 'example',
  worker: 'browser',
  lane: '',
  node: '',
  said_by: { kind: 'agent', id: 'claude-code', thread: null },
  told: '',
  state: 'done',
  stop: null,
  limit: 60,
  started_at: '',
  ended_at: null,
  run_id: null,
  steps: Array.from({ length: 29 }, (_, index) => ({
    kind: index === 9 ? ('rescue' as const) : index === 17 || index === 22 ? ('write' as const) : index === 27 ? ('yes' as const) : ('decide' as const),
    what: '',
    model: null,
    ms: 0,
    usd: 0,
  })),
};

function ThingHeader({ org, thing }: { org: string; thing: WorkerThing }) {
  const workspaces = useWorkspaces(org).data ?? [];
  const workspace = workspaces.find((item) => item.lanes.some((lane) => lane.id === thing.lane));
  const lane = workspace?.lanes.find((item) => item.id === thing.lane);
  const [, name = ''] = thing.lane.split('/');
  return (
    <div className="mb-4 grid min-w-0 gap-2">
      <div className="flex min-w-0 items-center justify-between gap-4">
        <p className="flex min-w-0 items-center gap-2 text-secondary text-muted">
          <SiteMark site={lane?.site?.key ?? name} size={16} label={false} />
          <span className="truncate">{[workspace?.name, lane ? accountName(lane) : null].filter(Boolean).join(' · ')} · {thing.state === 'working' ? 'started' : ''} {timeAgo(thing.started_at)}</span>
        </p>
        <Link to="/$org/activity" params={{ org }} className="inline-flex shrink-0 items-center gap-1 text-secondary text-muted hover:text-fg">
          History
          <ArrowRight aria-hidden="true" className="size-3" />
        </Link>
      </div>
      <p className="flex min-w-0 items-start gap-2 text-body text-pretty">
        <AgentMark principal={thing.said_by} size={20} className="mt-px" />
        <span className="min-w-0">
          <span className="text-muted">{principalName(thing.said_by)} said:</span> {thing.told}
        </span>
      </p>
    </div>
  );
}

/** Is it on, and what did it do. The one instrument on the page. */
function Now({ org, data, canChange, onTurnOn }: { org: string; data: WorkerOverview; canChange: boolean; onTurnOn: () => void }) {
  const answer = workerAnswer(data);
  const running = data.places.filter((place) => place.worker === 'on').map((place) => computerName(place.node));
  return (
    <section aria-label={WORKER_NAME.browser} className="board-surface registered rounded-xl border border-board-line">
      <div className="p-4 sm:p-6">
        <div className="flex min-h-5 flex-wrap items-center justify-between gap-x-4 gap-y-1">
          <p className="silk min-w-0 truncate">{[WORKER_NAME.browser, ...running].join(' · ')}</p>
          <StateBadge state={answer.state} label={answer.label} />
        </div>
        <h3 className="mt-3 max-w-2xl text-page-title-compact text-balance">
          {answer.lead} <span className="block text-faint">{answer.rest}</span>
        </h3>
        {data.today.waiting && answer.next !== 'needs-you' ? (
          <Link to="/$org/needs-you" params={{ org }} className="mt-2 inline-flex items-center gap-1 text-secondary text-muted hover:text-fg">
            {data.today.waiting === 1 ? 'One question waits' : `${data.today.waiting} questions wait`} for you in Needs you
            <ArrowRight aria-hidden="true" className="size-3" />
          </Link>
        ) : null}
        {answer.next === 'add-service' && canChange ? (
          <Button asChild variant="primary" className="mt-4">
            <Link to="/$org/settings/brains/add" params={{ org }} search={{ service: 'typesafe' }}>Add an AI service</Link>
          </Button>
        ) : answer.next === 'turn-on' && canChange ? (
          <Button variant="primary" className="mt-4" onClick={onTurnOn}>Turn it on</Button>
        ) : answer.next === 'needs-you' ? (
          <Button asChild variant="primary" className="mt-4">
            <Link to="/$org/needs-you" params={{ org }}>{data.latest?.stop === 'unsure' ? 'Answer it' : 'Do the check'}</Link>
          </Button>
        ) : null}
      </div>
      <div className="border-t border-board-line p-4 sm:p-6">
        {data.latest ? <ThingHeader org={org} thing={data.latest} /> : <p className="mb-4 text-secondary text-muted">How one thing will look, once it has done one.</p>}
        <StepLine thing={data.latest ?? EXAMPLE} example={!data.latest} />
      </div>
    </section>
  );
}

/**
 * The accounts under a computer, in one line, and only while it runs there: which ones it is off on,
 * each a link to where that switch is, and how its posts go. A long list folds into a count.
 */
function PlaceAccounts({ org, place }: { org: string; place: WorkerPlace }) {
  const workspaces = useWorkspaces(org).data ?? [];
  if (place.worker !== 'on') return null;
  const off = place.accounts_off.length;
  const named = off <= 2
    ? place.accounts_off.flatMap((id) => {
        const workspace = workspaces.find((item) => item.lanes.some((lane) => lane.id === id));
        const lane = workspace?.lanes.find((item) => item.id === id);
        return workspace && lane ? [{ id, ws: workspace.id, name: lane.name, words: `${workspace.name} ${accountName(lane)}` }] : [];
      })
    : [];
  return (
    <p className="text-secondary text-pretty text-muted">
      {named.length ? (
        <>
          Off on{' '}
          {named.map((item, index) => (
            <span key={item.id}>
              {index ? ' and ' : null}
              <Link to="/$org/lanes/$ws/$name" params={{ org, ws: item.ws, name: item.name }} className="link">{item.words}</Link>
            </span>
          ))}
          .{' '}
        </>
      ) : off ? `Off on ${off} accounts. ` : null}
      {placeAsks(place)}
    </p>
  );
}

function PlaceRow({ org, place, canChange, canAdd, onTurnOn }: { org: string; place: WorkerPlace; canChange: boolean; canAdd: boolean; onTurnOn: (node: string) => void }) {
  const set = useSetWorkerPlace(org);
  const [refused, setRefused] = useState<string | null>(null);
  const name = computerName(place.node);
  const switchable = place.state !== 'down' && place.worker !== 'needs_service';
  const flip = (on: boolean) => {
    setRefused(null);
    if (on) {
      onTurnOn(place.node);
      return;
    }
    set.mutate(
      { node: place.node, on: false },
      { onError: (error) => !isCancelled(error) && setRefused(`${name} did not answer, so the worker stays on.`) },
    );
  };
  return (
    <li className="flex items-start gap-3 px-4 py-4 sm:px-6">
      <span className="grid h-5 shrink-0 place-items-center text-faint"><Monitor aria-hidden="true" className="size-4" /></span>
      <div className="min-w-0 flex-1">
        <p className="text-label">{name}</p>
        <p aria-live="polite" className="text-secondary text-pretty text-muted">{refused ?? (set.isPending ? 'Telling the computer.' : placeSentence(place))}</p>
        <PlaceAccounts org={org} place={place} />
        {place.suggested_by ? <p className="text-secondary text-pretty text-muted">{principalName(place.suggested_by)} asked to turn it on here. Only a person can.</p> : null}
      </div>
      {switchable && canChange ? (
        <span className="flex h-5 shrink-0 items-center gap-2">
          <span className="text-secondary text-muted">{place.worker === 'on' ? 'On' : 'Off'}</span>
          <Switch
            aria-label={`Browser worker on ${name}`}
            aria-disabled={set.isPending || undefined}
            checked={place.worker === 'on'}
            onCheckedChange={(next) => !set.isPending && flip(next)}
          />
        </span>
      ) : place.state === 'down' ? (
        <Status status={{ label: 'Offline', tone: 'bad' }} className="shrink-0" />
      ) : place.worker === 'needs_service' && !canAdd ? null : place.worker === 'needs_service' && canChange ? (
        <Button asChild size="sm" icon={<Plus className="size-4" />} className="shrink-0">
          <Link to="/$org/settings/brains/add" params={{ org }} search={{ computer: place.node }}>
            <span className="max-sm:sr-only">Add an AI service</span>
            <span className="sm:hidden">Add one</span>
          </Link>
        </Button>
      ) : (
        <span className="shrink-0 text-secondary text-muted">{place.worker === 'on' ? 'On' : 'Off'}</span>
      )}
    </li>
  );
}

function JobRow({ org, item, brains, canChange }: { org: string; item: WorkerAssignment; brains: Brain[]; canChange: boolean }) {
  const set = useSetWorkerJob(org);
  const [technical] = useTechnical();
  const options = servicesFor(item.job, brains).map((brain) => ({ value: brain.name, label: serviceName(brain) }));
  const chosen = brains.find((brain) => brain.name === item.brain);
  return (
    <li className="grid gap-3 px-4 py-4 sm:grid-cols-[minmax(0,1fr)_200px] sm:items-center sm:gap-4 sm:px-6">
      <div className="min-w-0">
        <p className="text-label">{jobWord(item.job)}</p>
        <p className="text-secondary text-pretty text-muted">{JOB_NOTE[item.job]}</p>
        {technical && item.model ? <p className="mono mt-1 text-caption text-faint">{item.job} · {item.model}</p> : null}
      </div>
      {options.length && canChange ? (
        <Select
          aria-label={jobWord(item.job)}
          value={item.brain ?? ''}
          placeholder="Choose"
          options={options}
          onValueChange={(brain) => set.mutate({ job: item.job, brain })}
        />
      ) : options.length ? (
        <p className="text-body">{chosen ? serviceName(chosen) : 'Not chosen'}</p>
      ) : (
        <p className="text-body text-muted">Nothing can do this yet</p>
      )}
    </li>
  );
}

function LockLine({ children }: { children: React.ReactNode }) {
  return (
    <li className="flex items-start gap-2 text-secondary text-pretty text-muted">
      <span className="grid h-5 shrink-0 place-items-center text-faint"><Lock aria-hidden="true" className="size-3" /></span>
      <span className="min-w-0">{children}</span>
    </li>
  );
}

function Jobs({ org, data, canChange }: { org: string; data: WorkerOverview; canChange: boolean }) {
  const brains = useBrains(org).data ?? [];
  return (
    <Section title="Who does each job" description="A worker asks in this order, and goes no further than it has to.">
      <Card>
        <ol className="divide-y divide-line">
          {data.jobs.map((item) => <JobRow key={item.job} org={org} item={item} brains={brains} canChange={canChange} />)}
          <li className="grid gap-1 rounded-b-xl bg-inset px-4 py-4 sm:grid-cols-[minmax(0,1fr)_200px] sm:items-center sm:gap-4 sm:px-6">
            <div className="min-w-0">
              <p className="text-label">Still unsure, it stops and asks you</p>
              <p className="text-secondary text-pretty text-muted">The question waits in Needs you, and nothing changes until you answer.</p>
            </div>
            <p className="flex items-center gap-2 text-body text-muted"><Lock aria-hidden="true" className="size-3 text-faint" />You, always</p>
          </li>
        </ol>
      </Card>
      <ul className="grid gap-1">
        {NEVER.map((line) => <LockLine key={line}>{line}</LockLine>)}
      </ul>
    </Section>
  );
}

/** What is coming is said calmly: a line of words, with no button and no dead switch. */
function Later({ data }: { data: WorkerOverview }) {
  return (
    <section aria-label="Later" className="grid gap-3 border-t border-line pt-6">
      {data.later.map((item) => (
        <div key={item.kind} className="grid gap-1 sm:grid-cols-[160px_minmax(0,1fr)] sm:gap-4">
          <p className="text-label text-muted">{WORKER_NAME[item.kind]}</p>
          <p className="text-body text-pretty text-muted">
            Not yet. {item.sentence}
            {item.places.length ? <span className="text-faint"> {laterFacts(item.places)}</span> : null}
          </p>
        </div>
      ))}
    </section>
  );
}

function Technical({ data }: { data: WorkerOverview }) {
  const [technical] = useTechnical();
  const [open, setOpen] = useState(technical);
  const shown = open || technical;
  return (
    <div>
      <button type="button" aria-expanded={shown} onClick={() => setOpen(!open)} className="inline-flex items-center gap-1 text-secondary text-muted hover:text-fg">
        Technical details
        <ChevronDown aria-hidden="true" className={cn('size-3 transition-transform duration-(--duration-fast) ease-standard', shown && 'rotate-180')} />
      </button>
      {shown ? (
        <dl className="mono mt-2 grid gap-1 text-caption text-muted">
          <div className="flex gap-2"><dt className="w-20 shrink-0 text-faint">plugin</dt><dd className="min-w-0 break-all">{data.plugin}</dd></div>
          <div className="flex gap-2"><dt className="w-20 shrink-0 text-faint">version</dt><dd>{data.version}</dd></div>
          {data.jobs.map((item) => (
            <div key={item.job} className="flex gap-2"><dt className="w-20 shrink-0 text-faint">{item.job}</dt><dd className="min-w-0 break-all">{item.brain ?? 'none'} / {item.model ?? 'none'}</dd></div>
          ))}
        </dl>
      ) : null}
    </div>
  );
}

function PageSkeleton() {
  return (
    <div className="grid gap-10" role="status" aria-label="Loading">
      <div className="rounded-xl border border-line p-6">
        <Skeleton className="w-48" />
        <Skeleton className="mt-4 h-8 w-3/4" />
        <Skeleton className="mt-8 h-28 rounded-lg" />
        <Skeleton className="mt-4 w-2/3" />
      </div>
      <Skeleton className="h-32 rounded-xl" />
      <Skeleton className="h-40 rounded-xl" />
    </div>
  );
}

function Body({ org, data }: { org: string; data: WorkerOverview }) {
  const access = useAccess(org);
  const canChange = access.can('nodes.manage');
  const [turningOn, setTurningOn] = useState<string | null>(null);
  const first = data.places.find((place) => place.worker === 'off' && place.state !== 'down');
  if (!data.places.length) {
    return (
      <Card>
        <EmptyState
          icon={<MousePointerClick />}
          title="A worker needs a computer"
          description="It does the steps on a computer of yours, on the accounts kept there. Add a computer and the browser worker can run on it."
          action={
            <Button asChild variant="primary">
              <Link to="/$org/nodes" params={{ org }} search={{ add: true }}>Add a computer</Link>
            </Button>
          }
        />
      </Card>
    );
  }
  const on = data.places.some((place) => place.worker === 'on');
  // While nothing can decide anywhere, the panel's one button is the way in, and rows do not repeat it.
  const canAdd = data.places.some((place) => place.worker !== 'needs_service');
  return (
    <div className="grid gap-10">
      <Now org={org} data={data} canChange={canChange} onTurnOn={() => first && setTurningOn(first.node)} />
      {on ? <LimitsPanel org={org} data={data} canChange={canChange} /> : null}
      <Section title="Where it runs" description="A worker runs on a computer, and works on the accounts kept there.">
        <Card>
          <ul className="divide-y divide-line">
            {data.places.map((place) => <PlaceRow key={place.node} org={org} place={place} canChange={canChange} canAdd={canAdd} onTurnOn={setTurningOn} />)}
          </ul>
        </Card>
      </Section>
      <Jobs org={org} data={data} canChange={canChange} />
      <Later data={data} />
      <Technical data={data} />
      <TurnOnDialog org={org} data={data} node={turningOn} onClose={() => setTurningOn(null)} />
    </div>
  );
}

export function WorkersSettings() {
  const { org } = useParams({ from: '/$org/settings/workers' });
  const workers = useWorkers(org);
  return (
    <>
      <SettingsHeader title="Workers" description="An agent says what to do. A worker does the steps on your own computer, with an AI model that costs far less." />
      <QueryView card query={workers} skeleton={<PageSkeleton />}>
        {(data) => <Body org={org} data={data} />}
      </QueryView>
    </>
  );
}
