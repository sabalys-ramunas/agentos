/**
 * Engagement plans an agent drafted, waiting for the owner's yes. Nothing runs on an account until its plan is
 * approved, so the owner approves each one here, by name. The owner's alone (the node refuses anyone else).
 */
import { toast } from 'sonner';
import { useApproveProgram } from '@/api/hooks';
import type { EngageDay } from '@/api/types';
import { SiteMark } from '@/components/brand/site-mark';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { describeError } from '@/lib/errors';
import { siteOf } from '@/lib/sites';

/** One plan, with its account in words and the approval under them. */
function PlanRow({ org, plan, business }: { org: string; plan: EngageDay; business: string }) {
  const approve = useApproveProgram(org);
  const site = siteOf(plan.site)?.name ?? plan.site;
  const program = plan.program;
  if (!program) return null;
  return (
    <li className="flex flex-wrap items-center gap-3 py-3">
      <span className="flex size-10 shrink-0 items-center justify-center rounded-lg border border-line bg-subtle">
        <SiteMark site={plan.site} size={20} label={false} />
      </span>
      <span className="min-w-0 flex-1 basis-48">
        <span className="block text-14 font-medium break-words">
          {business}&apos;s {site} plan
        </span>
        <span className="block text-13 break-words text-faint">An agent drafted it. Nothing runs on this account until you approve it.</span>
      </span>
      <Button
        size="sm"
        loading={approve.isPending}
        onClick={() =>
          approve.mutate(
            { id: program.id, version: program.version },
            { onSuccess: () => toast(`Approved. ${business}'s ${site} plan can run now.`), onError: (error) => toast.error(describeError(error)) },
          )
        }
      >
        Approve plan
      </Button>
    </li>
  );
}

/** The section on Needs you: plans to approve. Renders nothing when none waits. */
export function PlansToApprove({ org, plans, businessOf }: { org: string; plans: EngageDay[]; businessOf: (lane: string) => string }) {
  if (plans.length === 0) return null;
  return (
    <section aria-label="Plans to approve">
      <div className="mb-1 px-2">
        <h2 className="text-14 font-semibold">Plans to approve</h2>
        <p className="text-12 text-faint">Each account runs only the plan you approved.</p>
      </div>
      <Card className="px-4">
        <ul className="divide-y divide-line">
          {plans.map((plan) => (
            <PlanRow key={plan.surface} org={org} plan={plan} business={businessOf(plan.surface)} />
          ))}
        </ul>
      </Card>
    </section>
  );
}
