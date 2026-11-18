/**
 * agentos's own composites, from the mock: one environment as its page draws it. They live in their
 * feature (features/environments, features/lane) because each reads its own data; a new screen about an
 * environment or an account reuses them instead of drawing its own.
 */
import { useParams } from '@tanstack/react-router';
import { useEnvironment } from '@/api/hooks';
import type { EnvDetail } from '@/api/types';
import { Card } from '@/components/ui/card';
import { ErrorState, Skeleton } from '@/components/ui/states';
import { CheckpointsCard } from '@/features/environments/checkpoints';
import { BrowserCard, CommandsCard, MachineCard } from '@/features/environments/env-cards';
import { Facts } from '@/features/environments/env-header';
import { ProofsCard } from '@/features/environments/proofs';
import { useNow } from '@/lib/use-now';

/** The mock's environment that is working now: it has a live browser, commands, proofs and checkpoints. */
const SAMPLE = 'e7k2';

function useSample(): { org: string; env: EnvDetail | undefined; error: unknown; retry: () => void } {
  const { org = '' } = useParams({ strict: false });
  const query = useEnvironment(org, SAMPLE);
  return { org, env: query.data, error: query.error, retry: () => void query.refetch() };
}

function Loading({ children }: { children: (org: string, env: EnvDetail, now: number) => React.ReactNode }) {
  const { org, env, error, retry } = useSample();
  const now = useNow(false);
  if (error) {
    return (
      <Card>
        <ErrorState error={error} onRetry={retry} />
      </Card>
    );
  }
  if (!env) return <Skeleton className="h-48 w-full rounded-xl" />;
  return <>{children(org, env, now)}</>;
}

export function FactsDemo() {
  return <Loading>{(_org, env, now) => <Facts env={env} now={now} />}</Loading>;
}

export function LiveViewDemo() {
  return <Loading>{(org, env) => <BrowserCard org={org} env={env} />}</Loading>;
}

export function TerminalListDemo() {
  return <Loading>{(org, env, now) => <CommandsCard org={org} env={env} now={now} />}</Loading>;
}

export function ProofDemo() {
  return <Loading>{(_org, env, now) => <ProofsCard env={env} now={now} />}</Loading>;
}

export function TimelineDemo() {
  return <Loading>{(org, env, now) => <CheckpointsCard org={org} env={env} now={now} />}</Loading>;
}

export function CostLineDemo() {
  return <Loading>{(_org, env) => <MachineCard env={env} />}</Loading>;
}
