/**
 * One environment (/environments/:id): the browser's live view large, with take over as on an account; the
 * commands its terminal ran, each job linked to its run page; the ports with a link where the node forwards
 * one; git (branch, ahead, changed files, what was pushed); the proofs it kept; the agent's last calls; the machine and its cost;
 * and Extend and Close. A closed one is a read-only summary that says why it closed. Read every few seconds
 * while it has a machine, since no event carries it.
 */
import { Link, useParams } from '@tanstack/react-router';
import { Lock, SearchX } from 'lucide-react';
import { useEnvironment } from '@/api/hooks';
import { ApiError, isNotServed } from '@/api/http';
import type { EnvDetail } from '@/api/types';
import { usePageCrumb } from '@/components/shell/crumb';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { EmptyState, ErrorState, NotLive, Skeleton } from '@/components/ui/states';
import { useNow } from '@/lib/use-now';
import {
  isOpen,
} from './words';
import { BrowserCard, CallsCard, CommandsCard, GitCard, MachineCard, PortsCard } from './env-cards';
import { ClosedNotice, EnvHeader, Facts } from './env-header';
import { ProofsCard } from './proofs';

export const EXTEND = [
  { minutes: 30, label: '30 minutes' },
  { minutes: 60, label: '1 hour' },
  { minutes: 120, label: '2 hours' },
];
export const CALLS_SHOWN = 10;

export function EnvironmentPage() {
  const { org, id } = useParams({ from: '/$org/environments/$id' });
  const query = useEnvironment(org, id);
  usePageCrumb(query.data?.title, { mono: false });

  if (query.isPending) return <EnvSkeleton />;
  if (query.isError || !query.data) return <EnvMissing org={org} id={id} error={query.error} onRetry={() => void query.refetch()} />;
  return <EnvLoaded org={org} env={query.data} />;
}

function EnvSkeleton() {
  return (
    <div role="status" aria-label="Loading the environment" className="grid gap-6">
      <div className="grid gap-3">
        <Skeleton className="h-8 w-72 max-w-full" />
        <Skeleton className="h-5 w-96 max-w-full" />
      </div>
      <Skeleton className="h-24 w-full rounded-xl" />
      <div className="grid gap-6 lg:grid-cols-12">
        <Skeleton className="aspect-[16/10] h-auto w-full rounded-xl lg:col-span-8" />
        <Skeleton className="h-64 w-full rounded-xl lg:col-span-4" />
      </div>
    </div>
  );
}

function EnvMissing({ org, id, error, onRetry }: { org: string; id: string; error: unknown; onRetry: () => void }) {
  const back = (
    <Button asChild>
      <Link to="/$org/environments" params={{ org }}>
        All environments
      </Link>
    </Button>
  );
  if (isNotServed(error)) {
    return (
      <Card>
        <NotLive />
      </Card>
    );
  }
  if (error instanceof ApiError && error.status === 404) {
    return (
      <Card>
        <EmptyState
          icon={<SearchX />}
          title="This environment is not here"
          description={
            <>
              This computer has no environment called <span className="font-mono text-code">{id}</span>. The address may not be quite right.
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
        <EmptyState icon={<Lock />} title="This environment is not for you" description="Seeing it needs the env workspace in your role. An owner or admin can add it." action={back} />
      </Card>
    );
  }
  return (
    <Card>
      <ErrorState error={error} onRetry={onRetry} />
    </Card>
  );
}

function EnvLoaded({ org, env }: { org: string; env: EnvDetail }) {
  const open = isOpen(env);
  const now = useNow(open);
  return (
    <div className="grid gap-6">
      <EnvHeader org={org} env={env} now={now} />
      {open ? null : <ClosedNotice env={env} />}
      <Facts env={env} now={now} />
      <div className="grid items-start gap-6 lg:grid-cols-12">
        <div className="grid min-w-0 gap-6 lg:col-span-8">
          {open ? <BrowserCard org={org} env={env} /> : null}
          <ProofsCard env={env} now={now} />
          <CommandsCard org={org} env={env} now={now} />
          <CallsCard env={env} now={now} />
        </div>
        <div className="grid min-w-0 gap-6 lg:col-span-4">
          <GitCard env={env} now={now} />
          {open ? <PortsCard env={env} /> : null}
          <MachineCard env={env} />
        </div>
      </div>
    </div>
  );
}
