import { useMutation } from '@tanstack/react-query';
import { useParams } from '@tanstack/react-router';
import { Receipt } from 'lucide-react';
import { fleet } from '@/api/client';
import { ApiError } from '@/api/http';
import { useAccess, useStatus } from '@/api/hooks';
import type { Status } from '@/api/types';
import { useRunsAlone } from '@/features/nodes/add-node';
import { Button } from '@/components/ui/button';
import { Card, FormCard } from '@/components/ui/card';
import { Meter } from '@/components/ui/misc';
import { EmptyState, NoAccess, QueryView, Skeleton } from '@/components/ui/states';
import { describeError } from '@/lib/errors';
import { safeRedirect } from '@/lib/format';
import { SettingsHeader } from './page';

/** A count, with its plan's limit when there is one. A machine has no plan caps, so it shows the count alone. */
function Usage({ label, value, max }: { label: string; value: number; max?: number }) {
  return (
    <div>
      <div className="mb-2 flex items-baseline justify-between text-14">
        <span>{label}</span>
        <span className="mono tabular text-13 text-muted">{max === undefined ? value : `${value} of ${max}`}</span>
      </div>
      {max === undefined ? null : <Meter value={value} max={max} label={label} />}
    </div>
  );
}

/** A machine has no plan and nothing to pay, so it sends none, and there is no portal to open. */
function NoPlan() {
  return (
    <FormCard title="Nothing to pay on this machine" description="It has no plan, no invoices and no usage limits." />
  );
}

function Body({ org, status }: { org: string; status: Status }) {
  // A self-hosted machine is billed by no one: there is no billing page to open and no invoice to send.
  const alone = useRunsAlone(org);
  const canManage = useAccess(org).can('billing.manage') && !alone;
  const portal = useMutation({
    mutationFn: async () => {
      const { url } = await fleet.billingPortal(org);
      const target = safeRedirect(url);
      if (!target) throw new ApiError(0, 'unsafe_redirect', 'The billing page address did not look right, so it was not opened. Nothing changed.');
      return target;
    },
    onSuccess: (target) => window.location.assign(target),
    meta: { inline: true },
  });
  if (!status.plan) return <NoPlan />;
  const plan = status.plan;
  return (
    <div className="grid gap-6">
      <FormCard
        title={`${plan.charAt(0).toUpperCase()}${plan.slice(1)} plan`}
        description={
          alone
            ? 'This machine is your own, so agentos does not bill it. Nothing is paid per run.'
            : 'agentos never bills per run. A watcher that fires a thousand times costs the same as one that fires twice.'
        }
        footnote={
          portal.isError ? (
            <span role="alert" className="text-red">
              {describeError(portal.error)}
            </span>
          ) : alone ? undefined : (
            'Over a limit after a downgrade, resources go read-only. Nothing is deleted.'
          )
        }
        action={
          canManage ? (
            <Button variant="primary" size="sm" loading={portal.isPending} onClick={() => portal.mutate()}>
              Manage billing
            </Button>
          ) : undefined
        }
      />
      {status.usage ? (
        <FormCard title="Usage" description={alone ? 'What this machine holds now.' : 'This billing period.'}>
          <div className="grid gap-6 sm:grid-cols-2">
            <Usage label="Active accounts" value={status.usage.active_lanes} max={status.limits?.active_lanes} />
            <Usage label="Machines" value={status.usage.nodes} max={status.limits?.nodes} />
            <Usage label="Workspaces" value={status.usage.workspaces} max={status.limits?.workspaces} />
            <Usage label="Members" value={status.usage.members} max={status.limits?.members} />
          </div>
          {status.limits ? <p className="text-13 text-faint">History is kept for {status.limits.retention_days} days on this plan.</p> : null}
        </FormCard>
      ) : null}
      {alone ? null : (
        <Card>
          <EmptyState icon={<Receipt />} title="No invoices yet" description="Invoices appear here after your first payment." className="py-10" />
        </Card>
      )}
    </div>
  );
}

export function BillingSettings() {
  const { org } = useParams({ from: '/$org/settings/billing' });
  const status = useStatus(org);
  const access = useAccess(org);
  if (!access.can('org.update')) return <NoAccess />;
  return (
    <>
      <SettingsHeader title="Billing and plan" />
      <QueryView card query={status} skeleton={<Skeleton className="h-64 rounded-xl" />}>
        {(data) => <Body org={org} status={data} />}
      </QueryView>
    </>
  );
}
