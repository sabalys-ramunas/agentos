/**
 * One run (`agentos run`): its state, the command, the facts the node keeps, its output followed live,
 * the failed tests read from that output, and what the machine recorded about it. Nothing here is
 * made up: a fact the node does not have is named as missing.
 */
import { Link, useNavigate, useParams, useSearch } from '@tanstack/react-router';
import { Check, Copy, Lock, SearchX, Square } from 'lucide-react';
import { useDeferredValue, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { useAccess, useCancelJob, useJobFollow } from '@/api/hooks';
import { ApiError, isNotServed } from '@/api/http';
import type { Job } from '@/api/types';
import { usePageCrumb } from '@/components/shell/crumb';
import { BranchPill } from '@/components/ui/branch';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { ConfirmDialog } from '@/components/ui/dialog';
import { KindEyebrow } from '@/components/ui/kind';
import { useCopy } from '@/components/ui/misc';
import { EmptyState, ErrorState, InlineError, NotLive, Skeleton } from '@/components/ui/states';
import { Status } from '@/components/ui/status';
import { cn } from '@/lib/cn';
import { clock, dateTime } from '@/lib/format';
import { useNow } from '@/lib/use-now';
import { FailuresCard, HappenedCard } from './run-sections';
import { OutputCard, useRunText } from './output-card';
import { runKind, durationOf, followCommand, statusOf, titleOf, STATE_SENTENCE } from './words';

export function RunPage() {
  const { org, id } = useParams({ from: '/$org/runs/$id' });
  // A failed read is read again from the start when the person asks: the key starts a new follow.
  const [attempt, setAttempt] = useState(0);
  return <RunView key={`${id}:${attempt}`} org={org} id={id} onRetry={() => setAttempt((n) => n + 1)} />;
}

function RunView({ org, id, onRetry }: { org: string; id: string; onRetry: () => void }) {
  const follow = useJobFollow(org, id);
  const job = follow.job;
  usePageCrumb(job ? titleOf(job) : undefined, { mono: false });

  if (!job && follow.error) return <RunMissing org={org} id={id} error={follow.error} onRetry={onRetry} />;
  if (!job) return <RunSkeleton />;
  return <RunLoaded org={org} job={job} follow={follow} />;
}

function RunMissing({ org, id, error, onRetry }: { org: string; id: string; error: unknown; onRetry: () => void }) {
  const back = (
    <Button asChild>
      <Link to="/$org/runs" params={{ org }}>
        All runs
      </Link>
    </Button>
  );
  if (isNotServed(error)) return <Card><NotLive /></Card>;
  if (error instanceof ApiError && error.status === 404) {
    return (
      <Card>
        <EmptyState
          icon={<SearchX />}
          title="This run is not on the machine"
          description={
            <>
              No machine you can see has a run called <span className="font-mono text-code">{id}</span>. A machine keeps a
              lane&apos;s newest runs and clears older ones, or the address is not quite right.
            </>
          }
          action={back}
        />
      </Card>
    );
  }
  if (error instanceof ApiError && error.status === 403) {
    return (
      <Card>
        <EmptyState
          icon={<Lock />}
          title="This run is not for you"
          description="It ran in a workspace your role does not include. An owner or admin can add you to it."
          action={back}
        />
      </Card>
    );
  }
  return (
    <Card>
      <ErrorState error={error} onRetry={onRetry} />
    </Card>
  );
}

function RunSkeleton() {
  return (
    <div role="status" aria-label="Loading the run" className="grid gap-6">
      <div className="grid gap-3">
        <Skeleton className="h-4 w-20" />
        <Skeleton className="h-8 w-72 max-w-full" />
        <Skeleton className="h-5 w-96 max-w-full" />
      </div>
      <Skeleton className="h-24 w-full rounded-xl" />
      <Skeleton className="h-96 w-full rounded-xl" />
    </div>
  );
}

type Follow = ReturnType<typeof useJobFollow>;

function RunLoaded({ org, job, follow }: { org: string; job: Job; follow: Follow }) {
  const search = useSearch({ from: '/$org/runs/$id' });
  const navigate = useNavigate({ from: '/$org/runs/$id' });
  const running = job.state === 'running';
  const now = useNow(running);
  const kind = runKind(job);
  const title = titleOf(job);
  const [jump, setJump] = useState<{ stream: 'stdout' | 'stderr'; no: number; nonce: number } | undefined>(undefined);
  // While a long log streams in, React may skip the in-between texts: the newest one always lands.
  const stdout = useDeferredValue(follow.stdout);
  const stderr = useDeferredValue(follow.stderr);
  const text = useRunText(stdout, stderr, job);
  const failedRef = useRef<HTMLElement>(null);
  const happenedRef = useRef<HTMLElement>(null);

  // A link that names a section lands on it.
  useEffect(() => {
    const target = search.tab === 'tests' ? failedRef.current : search.tab === 'next' ? happenedRef.current : null;
    target?.scrollIntoView?.({ block: 'start' });
    // Once, when the page opens.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const jumpTo = (stream: 'stdout' | 'stderr', line: number) => {
    // Lines are 0-based where they were read; the output numbers them from 1.
    setJump({ stream, no: line + 1, nonce: Date.now() });
    if (search.q || (search.stream && search.stream !== 'both' && search.stream !== stream)) {
      void navigate({ search: (prev) => ({ ...prev, q: undefined, stream: undefined }), replace: true });
    }
  };

  return (
    <div className="grid gap-6">
      <RunHeader org={org} job={job} kind={kind} title={title} now={now} />
      <Facts job={job} now={now} />
      <div className="grid items-start gap-6 lg:grid-cols-12">
        <div className="min-w-0 lg:col-span-8">
          <OutputCard job={job} text={text} done={follow.done} trimmed={follow.trimmed} jump={jump} onJump={jumpTo} />
        </div>
        <div className="grid min-w-0 gap-6 lg:col-span-4">
          <FailuresCard ref={failedRef} job={job} text={text} trimmed={follow.trimmed} onJump={jumpTo} />
          <HappenedCard ref={happenedRef} job={job} text={text} onJump={jumpTo} />
        </div>
      </div>
    </div>
  );
}

/** The sentence under the title: how it went, in the node's own words when it gave some. */
function lede(job: Job, now: number): string {
  const took = durationOf(job, now);
  const where = `on ${job.node}`;
  const parts: string[] = [];
  if (job.state === 'running') parts.push(`Running ${where} for ${took}.`);
  else if (job.state === 'passed') parts.push(`Passed ${where}${took ? ` in ${took}` : ''}.`);
  else if (job.state === 'failed') parts.push(`Failed ${where}${took ? ` after ${took}` : ''}${job.exit_code !== null ? `, with exit code ${job.exit_code}` : job.signal ? `, stopped by ${job.signal}` : ''}.`);
  else parts.push(STATE_SENTENCE[job.state]);
  if (job.oom_killed) parts.push('It ran out of memory and was stopped.');
  if (job.note && job.state !== 'running') parts.push(`The machine said: ${job.note}.`);
  return parts.join(' ');
}

function RunHeader({ org, job, kind, title, now }: { org: string; job: Job; kind: ReturnType<typeof runKind>; title: string; now: number }) {
  const access = useAccess(org);
  const cancel = useCancelJob(org);
  const [asking, setAsking] = useState(false);
  const { copied, copy } = useCopy();
  const status = statusOf(job);
  const command = `agentos run -- ${job.command}`;
  const canCancel = job.state === 'running' && access.can('lanes.operate');

  return (
    <header className="grid gap-3">
      <KindEyebrow kind={kind} />
      <div className="flex flex-wrap items-start gap-x-4 gap-y-3">
        <div className="grid w-full min-w-0 gap-2 md:w-auto md:flex-1">
          <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
            <h1 tabIndex={-1} className="min-w-0 text-page-title-compact [overflow-wrap:anywhere] md:text-page-title">
              {title}
            </h1>
            <Status status={status} />
          </div>
          <div className="flex min-w-0 items-start gap-2 font-mono text-code text-muted md:items-center">
            <span aria-hidden="true" className="text-faint">
              $
            </span>
            <span title={command} className="min-w-0 [overflow-wrap:anywhere] md:truncate">
              {command}
            </span>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {canCancel ? (
            <Button variant="danger" icon={<Square className="size-4" />} onClick={() => setAsking(true)}>
              Cancel
            </Button>
          ) : null}
          <Button icon={copied ? <Check className="size-4 text-ok" /> : <Copy className="size-4" />} onClick={() => copy(followCommand(job))}>
            {copied ? 'Copied' : 'Copy follow command'}
          </Button>
        </div>
      </div>
      <p className="max-w-[60em] text-14 text-muted">{lede(job, now)}</p>
      {cancel.error ? (
        <InlineError>
          {cancel.error instanceof ApiError && cancel.error.status === 403
            ? 'Your role does not let you stop runs in this workspace. Nothing was changed.'
            : 'The machine did not stop the run. Nothing was changed; try again.'}
        </InlineError>
      ) : null}
      <ConfirmDialog
        open={asking}
        onOpenChange={setAsking}
        title="Cancel this run?"
        description={`${title} stops on ${job.node} now. Its output so far is kept, and it shows here as cancelled.`}
        confirmLabel="Cancel the run"
        danger
        loading={cancel.isPending}
        onConfirm={() => cancel.mutate(job.id, { onSettled: () => setAsking(false) })}
      />
    </header>
  );
}

function Fact({ label, children, sub, className }: { label: string; children: ReactNode; sub?: ReactNode; className?: string }) {
  return (
    <div className={cn('grid min-w-0 content-start gap-1 bg-card px-5 py-4', className)}>
      <dt className="text-caption text-faint">{label}</dt>
      <dd className="flex min-w-0 items-center gap-2 text-14 font-medium text-fg">{children}</dd>
      {sub ? (
        <dd title={typeof sub === 'string' ? sub : undefined} className="min-w-0 truncate text-secondary text-faint">
          {sub}
        </dd>
      ) : null}
    </div>
  );
}

function Facts({ job, now }: { job: Job; now: number }) {
  const source = job.source;
  const tests = job.tests;
  const took = durationOf(job, now);
  const limit = job.timeout_s ? `limit ${Math.round(job.timeout_s / 60)} min` : undefined;
  return (
    <Card className="overflow-hidden">
      {/* The hairlines between facts are the card's line showing through a 1 px gap. */}
      <dl className="grid grid-cols-2 gap-px bg-line lg:grid-cols-5">
        <Fact
          label="Branch"
          className="col-span-2 lg:col-span-1"
          sub={
            source
              ? [source.commit ? source.commit.slice(0, 7) : 'no commit', source.dirty ? 'plus edits not committed' : source.dirty === false ? 'clean' : ''].filter(Boolean).join(' · ')
              : 'An older agentos, or nothing was synced'
          }
        >
          {source?.branch ? (
            <BranchPill name={source.branch} main={source.branch === 'main' || source.branch === 'master'} />
          ) : source ? (
            <span className="text-muted">No branch (a detached commit)</span>
          ) : (
            <span className="font-normal text-muted">Not recorded by this client</span>
          )}
        </Fact>
        <Fact label="Machine" sub={<span title={job.lane} className="font-mono text-code-caption">{job.lane}</span>}>
          <span title={job.node} className="truncate">
            {job.node}
          </span>
        </Fact>
        <Fact label="Started" sub={job.started_by ? `by ${job.started_by}` : 'Who started it was not kept'}>
          <time dateTime={job.started_at} title={dateTime(job.started_at)} className="font-mono tabular">
            {clock(job.started_at)}
          </time>
        </Fact>
        <Fact label={job.state === 'running' ? 'Running for' : 'Took'} sub={limit}>
          <span className="font-mono tabular">{took ?? 'Not known'}</span>
        </Fact>
        <Fact
          label="Tests"
          sub={
            tests ? (
              <>
                {tests.failed ? <span className="text-bad">{tests.failed} failed</span> : '0 failed'}
                {tests.skipped ? ` · ${tests.skipped} skipped` : ''} · {tests.runner}
              </>
            ) : job.state === 'running' ? (
              'When the runner prints its summary'
            ) : (
              'No test summary in the output'
            )
          }
        >
          {tests ? (
            <span className="font-mono tabular">
              {tests.passed.toLocaleString('en')} of {tests.total.toLocaleString('en')} passed
            </span>
          ) : (
            <span className="font-normal text-muted">Not reported</span>
          )}
        </Fact>
      </dl>
    </Card>
  );
}
