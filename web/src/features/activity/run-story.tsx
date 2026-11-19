/**
 * One thing that happened, told as a story: where and when, what it did in a sentence, whether it
 * worked and how we know, the proof as it appeared on the site, the steps in words, and the
 * machinery folded away under Technical details (structure.md, section 7, "The detail").
 */
import { deciderName } from '@/features/needs-you/outcome';
import { Link } from '@tanstack/react-router';
import { ArrowUpRight, ChevronRight, CircleDashed, ImageOff, X } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';
import { useAccess, useApproval, useCancelRun, useRun, useSchedules, useWorkspaces } from '@/api/hooks';
import type { Run, Workspace } from '@/api/types';
import { AgentMark, BusinessMark } from '@/components/brand/identity';
import { SiteMark } from '@/components/brand/site-mark';
import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/dialog';
import { CopyButton, Detail } from '@/components/ui/misc';
import { StateGlyph } from '@/components/ui/state-badge';
import { Skeleton } from '@/components/ui/states';
import { cn } from '@/lib/cn';
import { describeCron } from '@/lib/cron';
import { clock, clockSeconds, compact, dateTime, elapsed, safeUrl, usd } from '@/lib/format';
import { principalId } from '@/lib/principal';
import { useTechnical } from '@/lib/technical';
import { WorkerSteps } from '@/features/workers/in-place';
import { useNow } from '@/lib/use-now';
import { usePrincipalName } from '@/lib/use-principal';
import { YesReceipt } from '@/features/confirmations/yes-receipt';
import { ArtifactView } from './artifact';
import { dayLabel } from './feed';
import { isOpenRun, spansOf, Waterfall } from './run-waterfall';
import type { Result } from './story';
import { storyOf } from './story';
import type { StoryView } from './story-view';
import { fromRun, titleOf } from './story-view';

/** The cloud serves its own path. Anything else came from a node, so it has to be a real web address. */
const screenshotSrc = (value: string | null | undefined) => (value?.startsWith('/api/') ? value : safeUrl(value));

export const businessLookup = (workspaces: Workspace[] | undefined) => {
  const names = new Map((workspaces ?? []).map((item) => [item.id, item.name]));
  return (id: string) => names.get(id);
};

/** "today at 12:04", "Monday 2 November at 12:04". */
export function whenSaid(iso: string): string {
  const day = dayLabel(iso);
  return `${day === 'Today' || day === 'Yesterday' ? day.toLowerCase() : day} at ${clock(iso)}`;
}


/** The verdict's mark: the four states' glyphs, and a dashed ring for "done, not checked". */
export function ResultGlyph({ result, size = 20 }: { result: Result; size?: 14 | 16 | 20 | 24 }) {
  if (result === 'unchecked') return <CircleDashed aria-hidden="true" width={size} height={size} className="shrink-0 text-faint" />;
  const state = result === 'checked' ? 'good' : result === 'failed' ? 'stuck' : result === 'working' ? 'working' : 'waiting';
  return <StateGlyph state={state} size={size} />;
}

export function laneParams(org: string, lane: string) {
  const [ws = '', name = ''] = lane.split('/');
  return { org, ws, name };
}

function Screenshot({ view }: { view: StoryView }) {
  const [broken, setBroken] = useState(false);
  const src = screenshotSrc(view.proof?.screenshot);
  if (!src) return null;
  if (broken) {
    return (
      <p className="flex items-center gap-2 rounded-lg border border-line px-3 py-2 text-secondary text-muted">
        <ImageOff className="size-4 shrink-0 text-faint" />
        The picture stays on the computer, and the computer could not send it just now.
      </p>
    );
  }
  return (
    <a href={src} target="_blank" rel="noreferrer noopener" className="block overflow-hidden rounded-xl border border-line bg-inset">
      <img
        src={src}
        alt={`What ${view.named} showed afterwards: ${view.sentence}`}
        loading="lazy"
        onError={() => setBroken(true)}
        className="block max-h-96 w-full object-cover object-top"
      />
    </a>
  );
}

function Proof({ view }: { view: StoryView }) {
  const url = safeUrl(view.proof?.url);
  const shot = screenshotSrc(view.proof?.screenshot);
  if (view.artifact) {
    return (
      <div className="grid gap-2">
        <ArtifactView artifact={view.artifact} />
        {url ? (
          <a href={url} target="_blank" rel="noreferrer noopener" className="link inline-flex items-center gap-1 justify-self-end text-secondary">
            Open on {view.named} <ArrowUpRight className="size-3" />
          </a>
        ) : null}
        {shot ? <Screenshot view={view} /> : null}
      </div>
    );
  }
  if (!shot && !url) {
    // Only a thing that went out is missing a picture. Looking leaves nothing to take one of.
    if (view.lookOnly || (view.result !== 'checked' && view.result !== 'unchecked')) return null;
    return (
      <p className="flex items-center gap-2 rounded-lg border border-dashed border-line px-3 py-3 text-secondary text-muted">
        <ImageOff className="size-4 shrink-0 text-faint" />
        No picture of this one. The agent did it, but did not take a screenshot.
      </p>
    );
  }
  return (
    <div className="grid gap-2">
      <Screenshot view={view} />
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 text-secondary">
        {shot ? <span className="tabular text-faint">Screenshot taken at {clockSeconds(view.at)}</span> : <span />}
        {url ? (
          <a href={url} target="_blank" rel="noreferrer noopener" className="link inline-flex items-center gap-1">
            Open on {view.named} <ArrowUpRight className="size-3" />
          </a>
        ) : null}
      </div>
    </div>
  );
}

function Steps({ view }: { view: StoryView }) {
  if (view.steps.length === 0) return null;
  return (
    <section className="grid gap-2">
      <h3 className="text-secondary text-faint font-semibold">What the agent did</h3>
      <ol className="grid gap-2" aria-label="What the agent did">
        {view.steps.map((step) => {
          const bad = step.status === 'failed' || step.status === 'partial' || step.status === 'cancelled';
          const live = step.status === 'running' || step.status === 'queued';
          return (
            <li key={step.id} className="flex items-start gap-3 text-body">
              <span className="mt-px shrink-0">
                {bad ? (
                  <span className="flex size-4 items-center justify-center rounded-full bg-(--stuck-dot) text-status-ink">
                    <X className="size-3" strokeWidth={3} />
                  </span>
                ) : (
                  <StateGlyph state={live ? 'working' : 'good'} size={16} />
                )}
              </span>
              <span className={cn('min-w-0 flex-1', live && 'text-live')}>
                {step.label}
                {bad ? <span className="text-muted">, and it stopped here</span> : null}
              </span>
              <span className="tabular shrink-0 text-secondary text-faint">{clock(step.at)}</span>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

function ApprovedBy({ org, view }: { org: string; view: StoryView }) {
  const approval = useApproval(org, view.approvedBy ? null : view.approvalId).data;
  if (view.approvedBy) return <>{`${view.approvedBy.by ?? 'Someone'} at ${clock(view.approvedBy.at)}`}</>;
  // A run a person started by hand never ran on its own, whatever no approval says.
  if (!view.approvalId) return <>{view.who.principal.kind === 'human' ? 'Nobody needed to: it was started by hand' : 'Nobody needed to: it ran on its own'}</>;
  if (!approval) return <>Asked first</>;
  if (!approval.decided_by) return <>Not decided yet</>;
  return <>{`${deciderName(approval.decided_by, undefined, approval.decided_by_name)}${approval.decided_at ? ` at ${clock(approval.decided_at)}` : ''}`}</>;
}

function WhyItRan({ org, view }: { org: string; view: StoryView }) {
  const flow = view.who.principal.kind === 'flow' ? view.who.principal.id.replace(/@.*$/, '') : null;
  const schedules = useSchedules(org).data;
  const schedule = flow ? schedules?.find((item) => item.flow.replace(/@.*$/, '') === flow) : undefined;
  if (schedule?.cron) return <>{`On its schedule: ${describeCron(schedule.cron)}`}</>;
  // Who approved it has its own line, so why it ran says what started it.
  if (view.why && !/^Approved/.test(view.why)) return <>{view.why}</>;
  if (view.approvedBy || view.approvalId) return <>Asked by an agent</>;
  if (flow) return <>{schedule?.watch ? 'Something new arrived' : 'A routine'}</>;
  return <>Asked by an agent</>;
}

function Facts({ org, view }: { org: string; view: StoryView }) {
  const live = view.result === 'working';
  const now = useNow(live);
  const took = (live ? now : new Date(view.at).getTime()) - new Date(view.startedAt).getTime();
  return (
    <dl className="grid grid-cols-2 gap-x-4 gap-y-3 text-body">
      <div className="min-w-0">
        <dt className="text-secondary text-faint">Why it ran</dt>
        <dd className="mt-1">
          <WhyItRan org={org} view={view} />
        </dd>
      </div>
      <div className="min-w-0">
        <dt className="text-secondary text-faint">Who did it</dt>
        <dd className="mt-1 flex items-center gap-2">
          <AgentMark principal={view.who.principal} name={view.who.name} size={16} />
          <span className="truncate">{view.who.name}</span>
        </dd>
      </div>
      <div className="min-w-0">
        <dt className="text-secondary text-faint">{view.yes ? 'Who said yes' : 'Approved by'}</dt>
        <dd className="mt-1">
          <ApprovedBy org={org} view={view} />
        </dd>
      </div>
      <div className="min-w-0">
        <dt className="text-secondary text-faint">{live ? 'So far' : 'Took'}</dt>
        <dd className="tabular mt-1">{elapsed(took)}</dd>
      </div>
    </dl>
  );
}

function StopRun({ org, run }: { org: string; run: Run }) {
  const cancel = useCancelRun(org);
  const canOperate = useAccess(org).can('lanes.operate');
  const [asking, setAsking] = useState(false);
  if (!canOperate || !isOpenRun(run)) return null;
  return (
    <>
      <Button size="sm" onClick={() => setAsking(true)}>
        Stop it
      </Button>
      <ConfirmDialog
        open={asking}
        onOpenChange={setAsking}
        title="Stop this now?"
        description="What already happened stays done, and nothing after this point happens. History records it as stopped, never as done."
        confirmLabel="Stop it"
        danger
        loading={cancel.isPending}
        onConfirm={() =>
          cancel.mutate(run.id, {
            onSuccess: () => {
              setAsking(false);
              toast('Stopped');
            },
          })
        }
      />
    </>
  );
}

/** What a person can do about it, right under the verdict. */
function NextSteps({ org, view, run }: { org: string; view: StoryView; run?: Run }) {
  const account = laneParams(org, view.lane);
  const proofUrl = safeUrl(view.proof?.url);
  const openAccount = (label: string) => (
    <Button size="sm" asChild>
      <Link to="/$org/lanes/$ws/$name" params={account}>
        {label}
      </Link>
    </Button>
  );
  if (view.result === 'working') {
    return (
      <div className="flex flex-wrap gap-2">
        {openAccount('Watch it live')}
        {run ? <StopRun org={org} run={run} /> : null}
      </div>
    );
  }
  if (view.result === 'waiting') {
    return (
      <Button size="sm" asChild>
        <Link to="/$org/needs-you" params={{ org }}>
          Open Needs you
        </Link>
      </Button>
    );
  }
  if (view.result === 'unchecked') {
    return proofUrl ? (
      <Button size="sm" asChild>
        <a href={proofUrl} target="_blank" rel="noreferrer noopener">
          Check it myself
        </a>
      </Button>
    ) : (
      openAccount('Check it myself')
    );
  }
  if (view.result === 'failed' && view.status !== 'cancelled') return openAccount(view.accountProblem ? 'Fix the account' : 'Watch the account');
  return null;
}

export function Figure({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="min-w-0">
      <dt className="text-caption text-faint">{label}</dt>
      <dd className="mono tabular mt-1 truncate text-lede">{value}</dd>
      {note ? <dd className="tabular truncate text-caption text-faint">{note}</dd> : null}
    </div>
  );
}

/** The run's own numbers and waterfall, read only once someone opens the fold. */
function RunMachinery({ org, id }: { org: string; id: string }) {
  const run = useRun(org, id);
  const data = run.data;
  const live = data ? isOpenRun(data) && !data.finished_at : false;
  const now = useNow(live);
  if (!data) return run.isError ? null : <Skeleton className="h-24 rounded-lg" />;
  const { start, end } = spansOf(data, now);
  return (
    <>
      <dl className="grid grid-cols-3 gap-4 border-y border-line py-3">
        <Figure label={live ? 'So far' : 'Took'} value={elapsed(end - start)} />
        <Figure label="Steps" value={String(data.steps?.length ?? 0)} />
        <Figure label="Cost" value={usd(data.cost.usd)} note={`${compact(data.cost.tokens)} tokens`} />
      </dl>
      {data.steps?.length ? <Waterfall run={data} /> : null}
    </>
  );
}

function Technical({ org, view }: { org: string; view: StoryView }) {
  const [technical] = useTechnical();
  const [open, setOpen] = useState(technical);
  return (
    <details open={open} className="group border-t border-line">
      <summary
        onClick={(event) => {
          // Held in state, so the fold reads the run only once it is open.
          event.preventDefault();
          setOpen(!open);
        }}
        className="flex cursor-pointer list-none items-center gap-3 py-3 text-secondary text-muted hover:text-fg [&::-webkit-details-marker]:hidden">
        <ChevronRight className="size-4 shrink-0 transition-transform duration-(--duration-fast) group-open:rotate-90" />
        <span className="shrink-0">Technical details</span>
        <span className="mono min-w-0 flex-1 truncate text-right text-caption text-faint">
          {view.runId ?? view.id} · {view.lane} · {usd(view.technical.cost.usd)}
        </span>
      </summary>
      {open ? (
        <div className="grid gap-4 pb-2">
          {view.runId ? <RunMachinery org={org} id={view.runId} /> : null}
          <dl className="divide-y divide-line">
            <Detail label={view.runId ? 'Run id' : 'Call id'}>
              <span className="inline-flex flex-row-reverse items-center gap-1">
                <span className="mono">{view.runId ?? view.id}</span>
                <CopyButton text={view.runId ?? view.id} label="Copy id" />
              </span>
            </Detail>
            <Detail label="Lane">
              <span className="mono">{view.lane}</span>
            </Detail>
            <Detail label={view.runId ? 'Flow' : 'Tool'}>
              <span className="mono">{view.technical.what ?? '–'}</span>
            </Detail>
            <Detail label="Started by">
              <span className="mono">{principalId(view.who.principal)}</span>
            </Detail>
            <Detail label="Started">{dateTime(view.startedAt)}</Detail>
            {view.technical.detail ? (
              <Detail label="Node said">
                <span className="text-secondary break-words text-muted">{view.technical.detail}</span>
              </Detail>
            ) : null}
          </dl>
        </div>
      ) : null}
    </details>
  );
}

interface StoryDetailProps {
  org: string;
  view: StoryView;
  /** The run itself, when it was read, so a live one can be stopped from here. */
  run?: Run;
}

export function StoryDetail({ org, view, run }: StoryDetailProps) {
  const workspaces = useWorkspaces(org).data;
  const business = workspaces?.find((item) => item.id === view.workspace);
  return (
    <article className="grid gap-5" aria-label={view.sentence}>
      <header className="grid gap-2">
        <p className="flex min-w-0 items-center gap-2 text-secondary text-muted">
          {business ? <BusinessMark name={business.name} color={business.color} size={16} /> : <SiteMark site={view.siteKey} size={16} label={false} />}
          <span className="truncate">
            {[view.business, view.siteLabel].filter(Boolean).join(' · ')} · {whenSaid(view.startedAt)}
          </span>
        </p>
        <h2 className="text-heading text-balance">{titleOf(view)}</h2>
      </header>
      <section className="grid gap-3">
        <div className="flex items-start gap-3">
          <span className="shrink-0">
            <ResultGlyph result={view.status === 'cancelled' ? 'unchecked' : view.result} />
          </span>
          <div className="min-w-0">
            <p className="text-body font-semibold">{view.verdict}</p>
            {view.because !== view.verdict ? <p className="mt-1 text-body text-muted">{view.because}</p> : null}
          </div>
        </div>
        <div className="pl-8 empty:hidden">
          <NextSteps org={org} view={view} run={run} />
        </div>
      </section>
      <Proof view={view} />
      <Steps view={view} />
      {view.worker ? <WorkerSteps thing={view.worker} /> : null}
      {view.yes ? <YesReceipt {...view.yes} /> : null}
      <div className="border-t border-line pt-4">
        <Facts org={org} view={view} />
      </div>
      <Technical org={org} view={view} />
    </article>
  );
}

/** A run read on its own (a routine's run, an old link), told here. */
export function useRunView(org: string, run: Run | undefined): StoryView | null {
  const workspaces = useWorkspaces(org).data;
  const nameOf = usePrincipalName(org);
  if (!run) return null;
  const story = storyOf({ lane: run.lane, effect: run.effect ?? 'commit', status: run.status, summary: run.summary }, businessLookup(workspaces));
  return fromRun(run, story, nameOf(run.principal));
}
