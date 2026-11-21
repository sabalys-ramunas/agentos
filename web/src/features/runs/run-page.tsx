/**
 * One run (`openajar run`): its state, the command, the facts the node keeps, its output followed live,
 * the failed tests read from that output, and what the machine recorded about it. Nothing here is
 * made up: a fact the node does not have is named as missing.
 */
import { Link, useNavigate, useParams, useSearch } from '@tanstack/react-router';
import { Check, Copy, Lock, SearchX } from 'lucide-react';
import { useDeferredValue, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { useAccess, useCancelJob, useJobFollow } from '@/api/hooks';
import { ApiError, isNotServed } from '@/api/http';
import type { Job } from '@/api/types';
import { usePageCrumb } from '@/components/shell/crumb';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { ConfirmDialog } from '@/components/ui/dialog';
import { KindEyebrow, KindTile } from '@/components/ui/kind';
import { PageHeader, useCopy } from '@/components/ui/misc';
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
    <Button asChild variant="primary">
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
              No machine you can see has a run called <span className="font-mono text-code">{id}</span>. A machine keeps its
              newest runs and clears older ones, or the address is not quite right.
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
      <div className="grid grid-cols-[auto_minmax(0,1fr)] items-start gap-x-4">
        <Skeleton className="size-10 rounded-lg" />
        <div className="grid gap-3">
          <Skeleton className="h-4 w-16" />
          <Skeleton className="h-8 w-72 max-w-full" />
          <Skeleton className="h-5 w-80 max-w-full" />
        </div>
      </div>
      <Skeleton className="h-28 w-full rounded-xl" />
      <div className="grid items-start gap-6 lg:grid-cols-12">
        <Skeleton className="h-96 w-full rounded-xl lg:col-span-8" />
        <Skeleton className="h-48 w-full rounded-xl lg:col-span-4" />
      </div>
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
    <>
      <RunHeader org={org} job={job} kind={kind} title={title} />
      <div className="grid gap-6">
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
    </>
  );
}

/**
 * The sentence under the command, only when it says what the chip and the facts do not: how a run ended
 * badly, that it ran out of memory, the machine's own note. A running or passed run needs none.
 */
function lede(job: Job): string | null {
  const parts: string[] = [];
  if (job.state === 'failed') {
    parts.push(
      job.exit_code !== null
        ? `It ended with exit code ${job.exit_code}.`
        : job.signal
          ? `It was stopped by ${job.signal}.`
          : STATE_SENTENCE.failed,
    );
  } else if (job.state !== 'running' && job.state !== 'passed') parts.push(STATE_SENTENCE[job.state]);
  if (job.oom_killed) parts.push('It ran out of memory and was stopped.');
  if (job.note && job.state !== 'running') parts.push(`The machine said: ${job.note}.`);
  return parts.length ? parts.join(' ') : null;
}

function RunHeader({ org, job, kind, title }: { org: string; job: Job; kind: ReturnType<typeof runKind>; title: string }) {
  const access = useAccess(org);
  const cancel = useCancelJob(org);
  const [asking, setAsking] = useState(false);
  const { copied, copy } = useCopy();
  const status = statusOf(job);
  const command = `openajar run -- ${job.command}`;
  const canCancel = job.state === 'running' && access.can('lanes.operate');
  const sentence = lede(job);

  return (
    <>
      <PageHeader
        lead={<KindTile kind={kind} size="xl" />}
        eyebrow={<KindEyebrow kind={kind} tile={false} />}
        title={title}
        aside={<Status status={status} chip />}
        description={
          <>
            <span className="flex min-w-0 items-start gap-2 font-mono text-code text-muted">
              <span aria-hidden="true" className="text-faint">
                $
              </span>
              <span title={command} className="min-w-0 [overflow-wrap:anywhere] md:truncate">
                {command}
              </span>
            </span>
            {sentence ? <span>{sentence}</span> : null}
          </>
        }
        actions={
          <>
            <Button icon={copied ? <Check className="size-4 text-ok" /> : <Copy className="size-4" />} onClick={() => copy(followCommand(job))}>
              {copied ? 'Copied' : 'Copy follow command'}
            </Button>
            {canCancel ? (
              <Button variant="danger" onClick={() => setAsking(true)}>
                Cancel
              </Button>
            ) : null}
          </>
        }
      />
      {cancel.error ? (
        <div className="-mt-4 mb-6">
          <InlineError>
            {cancel.error instanceof ApiError && cancel.error.status === 403
              ? 'Your role does not let you stop runs in this workspace. Nothing was changed.'
              : 'The machine did not stop the run. Nothing was changed; try again.'}
          </InlineError>
        </div>
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
    </>
  );
}

function Fact({ label, children, sub, className }: { label: string; children: ReactNode; sub?: ReactNode; className?: string }) {
  return (
    <div className={cn('grid min-w-0 content-start gap-1', className)}>
      <dt className="text-secondary text-muted">{label}</dt>
      <dd className="min-w-0 text-card-title [overflow-wrap:anywhere] text-fg">{children}</dd>
      {sub ? <dd className="min-w-0 text-secondary [overflow-wrap:anywhere] text-muted">{sub}</dd> : null}
    </div>
  );
}

function Facts({ job, now }: { job: Job; now: number }) {
  const source = job.source;
  const tests = job.tests;
  const took = durationOf(job, now);
  const limit = job.timeout_s ? `limit ${Math.round(job.timeout_s / 60)} min` : undefined;
  return (
    <Card>
      <dl className="grid grid-cols-2 gap-x-6 gap-y-5 px-5 py-5 lg:grid-cols-5">
        <Fact
          label="Branch"
          className="col-span-2 lg:col-span-1"
          sub={
            source ? (
              <>
                {source.commit ? <span className="font-mono text-code-caption">{source.commit.slice(0, 7)}</span> : 'no commit'}
                {source.dirty ? ' · plus edits not committed' : source.dirty === false ? ' · clean' : ''}
              </>
            ) : (
              'An older openajar, or nothing was synced'
            )
          }
        >
          {source?.branch ? (
            <span title={source.branch} className="font-mono text-code font-medium">
              {source.branch}
            </span>
          ) : source ? (
            <span className="font-normal text-muted">No branch (a detached commit)</span>
          ) : (
            <span className="font-normal text-muted">Not recorded by this client</span>
          )}
        </Fact>
        <Fact label="Machine" sub={<span title={job.lane}>{job.lane}</span>}>
          {job.node}
        </Fact>
        <Fact label="Started" sub={job.started_by ? `by ${job.started_by}` : 'Who started it was not kept'}>
          <time dateTime={job.started_at} title={dateTime(job.started_at)} className="tabular">
            {clock(job.started_at)}
          </time>
        </Fact>
        <Fact label={job.state === 'running' ? 'Running for' : 'Took'} sub={limit}>
          <span className="tabular">{took ?? 'Not known'}</span>
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
            <span className="tabular">
              {tests.passed.toLocaleString('en')} of {tests.total.toLocaleString('en')} passed
            </span>
          ) : (
            <span className="font-normal text-muted">{job.state === 'running' ? 'Not reported yet' : 'Not reported'}</span>
          )}
        </Fact>
      </dl>
    </Card>
  );
}
