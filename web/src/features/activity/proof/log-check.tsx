import { ShieldAlert, ShieldCheck, ShieldQuestion } from 'lucide-react';
import { useProofVerify } from '@/api/hooks';
import { Banner, QueryView, Skeleton } from '@/components/ui/states';
import { verdictOf } from './words';

/**
 * Whether the signed log is intact, said once in a banner above the day (proof.png): green when every step matches
 * its checkpoint, red when one does not, quiet when this computer cannot check. Only the owner asks (`enabled`),
 * since the node checks the whole log for the owner alone.
 */
export function LogCheck({ org }: { org: string }) {
  const verify = useProofVerify(org);
  return (
    <QueryView query={verify} skeleton={<Skeleton className="h-14 w-full rounded-xl" />}>
      {(report) => {
        const verdict = verdictOf(report);
        const tone = verdict.tone === 'ok' ? 'ok' : verdict.tone === 'bad' ? 'bad' : 'neutral';
        const icon = tone === 'ok' ? <ShieldCheck /> : tone === 'bad' ? <ShieldAlert /> : <ShieldQuestion />;
        return (
          <section aria-label="Log check">
            <Banner tone={tone} icon={icon} title={`${verdict.label}.`}>
              {verdict.sentence}
            </Banner>
          </section>
        );
      }}
    </QueryView>
  );
}
