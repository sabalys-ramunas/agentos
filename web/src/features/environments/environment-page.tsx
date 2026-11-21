/**
 * One environment (/environments/:id), web/DESIGN.md "One environment". Its top is the night band: the name and
 * state, the live screen with the control pill under it, and beside it three glass panels (time and cost, what
 * it is doing, its code). Below: how it opened (folded to a line once it is up), its checkpoints, the proofs it
 * kept, its terminal, what it did, its code, its network, its ports, and one quiet line for the machine. A
 * closed one keeps the same page, read-only: its replay leads in the screen's place (or the screen drawn
 * asleep), with why it closed and, for a save, Resume. On a phone the band holds the name and the screen; the
 * panels and Extend and Close follow.
 * Read every few seconds while it has a machine, since no event carries it.
 */
import { Link, useParams } from '@tanstack/react-router';
import { Lock, SearchX } from 'lucide-react';
import { useEnvironment, useSession } from '@/api/hooks';
import { ApiError, isNotServed } from '@/api/http';
import type { EnvDetail } from '@/api/types';
import { usePageCrumb } from '@/components/shell/crumb';
import { usePageBand } from '@/components/shell/page-band';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { EmptyState, ErrorState, NotLive, Skeleton } from '@/components/ui/states';
import { useMinWidth } from '@/lib/use-min-width';
import { useNow } from '@/lib/use-now';
import { holdsLane } from '@/features/lane/control';
import { CheckpointsCard } from './checkpoints';
import { CommandsCard, GitCard, MachineLine, PortsCard, StepsCard } from './env-cards';
import { EnvHeader, PhoneActions } from './env-header';
import { CodePanel, DoingPanel, TimePanel } from './env-panels';
import { EnvStage, useEnvLane } from './env-stage';
import { NetworkCard } from './network-card';
import { OpenedCard } from './opened';
import { ProofsCard } from './proofs';
import { ResumedNotice, SavedNotice } from './resume';
import { isOpen } from './words';

export { EXTEND } from './env-header';
export { CALLS_SHOWN } from './env-cards';

export function EnvironmentPage() {
  const { org, id } = useParams({ from: '/$org/environments/$id' });
  const query = useEnvironment(org, id);
  usePageCrumb(query.data?.title, { mono: false });

  if (query.isPending) return <EnvSkeleton />;
  if (query.isError || !query.data) return <EnvMissing org={org} id={id} error={query.error} onRetry={() => void query.refetch()} />;
  return <EnvLoaded org={org} env={query.data} />;
}

/** The band and its shapes while the environment is read, so nothing jumps when it arrives. */
function EnvSkeleton() {
  const band = usePageBand<HTMLDivElement>();
  const night = 'bg-side-hover';
  return (
    <div role="status" aria-label="Loading the environment" className="grid gap-6">
      <div ref={band} className="on-night grid gap-6">
        <div className="grid gap-3">
          <Skeleton className={`h-10 w-96 max-w-full ${night}`} />
          <Skeleton className={`h-5 w-80 max-w-full ${night}`} />
        </div>
        <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_312px]">
          <div className="grid gap-4">
            <Skeleton className={`aspect-video h-auto w-full rounded-lg ${night}`} />
            <Skeleton className={`mx-auto h-12 w-80 max-w-full rounded-full ${night}`} />
          </div>
          <div className="hidden gap-3 lg:grid">
            <Skeleton className={`h-24 rounded-xl ${night}`} />
            <Skeleton className={`h-64 rounded-xl ${night}`} />
          </div>
        </div>
      </div>
      <div className="grid gap-4 lg:grid-cols-2">
        <Skeleton className="h-64 w-full rounded-xl" />
        <Skeleton className="h-64 w-full rounded-xl" />
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
              This computer has no environment called <span className="font-mono text-code">{id}</span>. The address may not be quite right, or it
              was closed long ago.
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
  const band = usePageBand<HTMLDivElement>();
  const lane = useEnvLane(org, env);
  const user = useSession().data?.user;
  const yours = Boolean(lane.data && holdsLane(lane.data.control, user));
  // The panels sit beside the screen from 1024 px, and under the band (as cards) below it; rendered once.
  const wide = useMinWidth(1024);
  const tablet = useMinWidth(768);
  return (
    <div className="grid gap-6">
      <div ref={band} className="on-night grid gap-6">
        <EnvHeader org={org} env={env} now={now} yours={yours} actions={tablet} />
        <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_312px]">
          <div className="min-w-0">
            <EnvStage org={org} env={env} />
          </div>
          {wide ? (
            <div className="grid min-w-0 gap-3">
              <TimePanel env={env} now={now} glass />
              <DoingPanel env={env} glass />
              <CodePanel env={env} glass />
            </div>
          ) : null}
        </div>
      </div>
      {wide ? null : (
        <div className="grid gap-4 sm:grid-cols-2">
          <TimePanel env={env} now={now} glass={false} />
          <DoingPanel env={env} glass={false} steps={3} />
          {tablet ? null : <PhoneActions org={org} env={env} className="sm:col-span-2" />}
        </div>
      )}
      {open ? env.state === 'starting' ? null : <ResumedNotice env={env} /> : <SavedNotice org={org} env={env} />}
      <OpenedCard org={org} env={env} />
      <div className="grid items-start gap-4 lg:grid-cols-2">
        <div className="grid min-w-0 gap-4">
          <ProofsCard env={env} now={now} />
          <CheckpointsCard org={org} env={env} now={now} />
          <CommandsCard org={org} env={env} now={now} />
        </div>
        <div className="grid min-w-0 gap-4">
          <StepsCard env={env} now={now} />
          <GitCard env={env} now={now} />
          {env.network ? <NetworkCard network={env.network} now={now} /> : null}
          {open ? <PortsCard env={env} /> : null}
        </div>
      </div>
      <MachineLine env={env} />
    </div>
  );
}
