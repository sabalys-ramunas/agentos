/**
 * Runs: every `agentos run` job the person may see, newest first, as the Mono table. Kind, title and
 * command, status, branch and commit when the client sent them, who started it and where, the test
 * totals read from the output, how long it took and when. Routine and agent runs live in History.
 */
import { Link, useParams } from '@tanstack/react-router';
import { Lock, SquareTerminal, Unplug } from 'lucide-react';
import { useJobs } from '@/api/hooks';
import { ApiError, isNotServed } from '@/api/http';
import type { Job } from '@/api/types';
import { Card } from '@/components/ui/card';
import { CodeBlock, PageHeader } from '@/components/ui/misc';
import { EmptyState, ErrorState, NotLive } from '@/components/ui/states';
import { TableSkeleton } from '@/components/ui/table';
import type { SortDirection } from '@/components/ui/table';
import { PLACES } from '@/lib/vocabulary';
import { createStored } from '@/lib/store';
import { useNow } from '@/lib/use-now';
import type { RunSort, RunStateFilter, RunsSearch } from './search';
import { durationMs, matchesState, runKind, summaryOf, titleOf } from './words';
import { RunsTable } from './runs-table';

/** The node lists at most this many; the page asks for all of them and filters here. */
export const LIMIT = 200;
export const density = createStored<'default' | 'compact'>('agentos.runs.density', 'default');

export const STATE_WORD: Record<RunStateFilter, string> = { running: 'Running', passed: 'Passed', failed: 'Failed', stopped: 'Stopped early' };
const RANGE_MS = { '1h': 3_600_000, '24h': 86_400_000, '7d': 7 * 86_400_000, '30d': 30 * 86_400_000 } as const;
export const RANGES = [
  { value: 'any', label: 'Any time' },
  { value: '1h', label: 'Last hour' },
  { value: '24h', label: 'Last 24 hours' },
  { value: '7d', label: 'Last 7 days' },
  { value: '30d', label: 'Last 30 days' },
];
/** Running first, then what went wrong, then what passed. */
const STATE_ORDER: Record<Job['state'], number> = { running: 0, failed: 1, lost: 2, timed_out: 3, cancelled: 4, passed: 5 };

export const COLUMNS = ['', 'Run', 'Result', 'Branch', 'Who and where', 'Passed', 'Failed', 'Took', 'Started', ''];

export function filterJobs(jobs: Job[], search: RunsSearch, now = Date.now()): Job[] {
  const needle = search.q?.trim().toLowerCase();
  return jobs.filter((job) => {
    if (search.state && !matchesState(job, search.state)) return false;
    if (search.kind && runKind(job) !== search.kind) return false;
    if (search.machine && job.node !== search.machine) return false;
    if (search.lane && job.lane !== search.lane) return false;
    if (search.by && job.started_by !== search.by) return false;
    if (search.range && now - Date.parse(job.started_at) > RANGE_MS[search.range]) return false;
    if (needle) {
      const hay = [job.id, job.command, job.lane, job.node, job.started_by, job.source?.branch, job.source?.commit, titleOf(job), job.note]
        .filter(Boolean)
        .join(' ')
        .toLowerCase();
      if (!hay.includes(needle)) return false;
    }
    return true;
  });
}

export function sortJobs(jobs: Job[], sort: RunSort = 'started', dir: SortDirection = 'desc', now = Date.now()): Job[] {
  const value = (job: Job): number => {
    if (sort === 'duration') return durationMs(job, now);
    if (sort === 'state') return STATE_ORDER[job.state];
    if (sort === 'failed') return job.tests ? job.tests.failed : -1;
    return Date.parse(job.started_at);
  };
  const sign = dir === 'asc' ? 1 : -1;
  return [...jobs].sort((a, b) => (value(a) - value(b)) * sign || b.started_at.localeCompare(a.started_at));
}

export function RunsPage() {
  const { org } = useParams({ from: '/$org/runs' });
  const query = useJobs(org, { limit: LIMIT });
  const jobs = query.data?.jobs;
  const now = useNow(!!jobs?.some((job) => job.state === 'running'));

  return (
    <div className="grid gap-6">
      <PageHeader
        title={PLACES.runs.word}
        description={
          <>
            Tests, builds and dev servers that you and your agents start on your machines with{' '}
            <code className="rounded-sm border border-line bg-hover px-1 font-mono text-code">agentos run</code>.
            {query.data?.available && jobs?.length ? <span className="text-fg"> {summaryOf(jobs, now)}</span> : null}
            <span className="mt-1 block text-body text-faint">
              Routines and the runs agents make on accounts are in{' '}
              <Link to="/$org/activity" params={{ org }} className="font-medium text-muted underline underline-offset-2 hover:text-fg">
                History
              </Link>
              .
            </span>
          </>
        }
      />
      <RunsBody org={org} query={query} jobs={jobs} />
    </div>
  );
}

function RunsBody({ org, query, jobs }: { org: string; query: ReturnType<typeof useJobs>; jobs: Job[] | undefined }) {
  if (query.isPending) {
    return (
      <Card role="status" aria-label="Loading runs">
        <TableSkeleton columns={COLUMNS.slice(1, 9)} />
      </Card>
    );
  }
  if (isNotServed(query.error)) {
    return (
      <Card>
        <NotLive />
      </Card>
    );
  }
  if (query.error instanceof ApiError && query.error.status === 403) {
    return (
      <Card>
        <EmptyState
          icon={<Lock />}
          title="Runs are not shown to your role"
          description="You see the runs in the workspaces your role includes, and this role includes none. An owner or admin can change that."
        />
      </Card>
    );
  }
  if (query.isError || !query.data) {
    return (
      <Card>
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      </Card>
    );
  }
  if (!query.data.available) {
    return (
      <Card>
        <EmptyState
          icon={<Unplug />}
          title="This machine runs no commands"
          description={query.data.reason ?? 'It has no terminal lanes switched on, so nothing can run on it with agentos run.'}
        />
      </Card>
    );
  }
  if (!jobs?.length) {
    return (
      <Card>
        <EmptyState
          icon={<SquareTerminal />}
          title="No runs yet"
          description="When you or an agent runs a command on a machine, it shows here with its live output, what passed and what failed."
          action={<CodeBlock code="agentos run -- pnpm test" label="Copy the command" className="text-left" />}
        />
      </Card>
    );
  }
  return <RunsTable org={org} jobs={jobs} capped={jobs.length >= LIMIT} />;
}
