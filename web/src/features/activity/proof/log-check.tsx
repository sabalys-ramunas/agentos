import { useProofVerify } from '@/api/hooks';
import { QueryView, Skeleton } from '@/components/ui/states';
import { StatusDot } from '@/components/ui/status';
import { verdictOf } from './words';

/**
 * Whether the signed log is intact, said once in a quiet strip above the day. Only the owner
 * asks (`enabled`), since the node checks the whole log for the owner alone.
 */
export function LogCheck({ org }: { org: string }) {
  const verify = useProofVerify(org);
  return (
    <QueryView query={verify} skeleton={<Skeleton className="h-12 w-full rounded-xl" />}>
      {(report) => {
        const verdict = verdictOf(report);
        return (
          <section aria-label="Log check" className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-xl border border-line px-4 py-3">
            <p className="flex min-w-0 items-center gap-2 text-label">
              <StatusDot tone={verdict.tone} />
              {verdict.label}
            </p>
            <p className="min-w-0 text-secondary text-muted">{verdict.sentence}</p>
          </section>
        );
      }}
    </QueryView>
  );
}
