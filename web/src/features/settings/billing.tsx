import { InlineError } from '@/components/ui/states';
import { useMutation } from '@tanstack/react-query';
import { useParams } from '@tanstack/react-router';
import { ExternalLink, Star } from 'lucide-react';
import { fleet } from '@/api/client';
import { ApiError } from '@/api/http';
import { useAccess, useStatus } from '@/api/hooks';
import type { Status } from '@/api/types';
import { useRunsAlone } from '@/features/nodes/add-node';
import { Button } from '@/components/ui/button';
import { FormCard } from '@/components/ui/card';
import { AppTile } from '@/components/ui/tile';
import { Meter } from '@/components/ui/misc';
import { NoAccess, QueryView, Skeleton } from '@/components/ui/states';
import { describeError } from '@/lib/errors';
import { safeRedirect } from '@/lib/format';
import { SettingsHeader } from './page';

/** A count, with its plan's limit when there is one. A machine has no plan caps, so it shows the count alone. */
function Usage({ label, value, max }: { label: string; value: number; max?: number }) {
  const full = max !== undefined && value >= max;
  return (
    <div className="grid content-start gap-2">
      <span className="text-secondary text-muted">{label}</span>
      <span className="flex items-baseline gap-1">
        <span className="tabular text-page-title-compact">{value}</span>
        {max === undefined ? null : <span className="tabular text-secondary text-faint">of {max}</span>}
      </span>
      {max === undefined ? null : <Meter value={value} max={max} label={`${label}: ${value} of ${max}`} />}
      {full ? <span className="text-caption text-warn">At the limit. New ones wait for a bigger plan.</span> : null}
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
    <div className="grid gap-4">
      <FormCard
        tile={<AppTile tone="violet" icon={Star} size="md" />}
        tileAlign="center"
        title={`${plan.charAt(0).toUpperCase()}${plan.slice(1)} plan`}
        description={
          alone
            ? 'This machine is your own, so openajar does not bill it. Nothing is paid per run.'
            : 'Over a limit after a downgrade, things go read-only. Nothing is deleted.'
        }
        aside={
          canManage ? (
            <Button icon={<ExternalLink className="size-4" />} loading={portal.isPending} onClick={() => portal.mutate()}>
              Manage billing
            </Button>
          ) : undefined
        }
      >
        {portal.isError ? <InlineError>{describeError(portal.error)}</InlineError> : null}
      </FormCard>
      {status.usage ? (
        <FormCard title="Usage" description={alone ? 'What this machine holds now.' : 'This billing period.'}>
          <div className="grid grid-cols-2 gap-6 md:grid-cols-4">
            <Usage label="Active accounts" value={status.usage.active_lanes} max={status.limits?.active_lanes} />
            <Usage label="Machines" value={status.usage.nodes} max={status.limits?.nodes} />
            <Usage label="Workspaces" value={status.usage.workspaces} max={status.limits?.workspaces} />
            <Usage label="Members" value={status.usage.members} max={status.limits?.members} />
          </div>
          {status.limits ? <p className="text-secondary text-faint">History is kept for {status.limits.retention_days} days on this plan.</p> : null}
        </FormCard>
      ) : null}
      {alone ? null : (
        <FormCard title="Invoices" description="No invoices yet. They show here after your first payment." />
      )}
    </div>
  );
}

export function BillingSettings() {
  const { org } = useParams({ from: '/$org/settings/billing' });
  const status = useStatus(org);
  const access = useAccess(org);
  const alone = useRunsAlone(org);
  if (!access.can('org.update')) return <NoAccess />;
  return (
    <>
      <SettingsHeader
        title="Billing and plan"
        description={alone ? undefined : 'openajar never bills per run. A watcher that fires a thousand times costs the same as one that fires twice.'}
      />
      <QueryView card query={status} skeleton={<Skeleton className="h-64 rounded-xl" />}>
        {(data) => <Body org={org} status={data} />}
      </QueryView>
    </>
  );
}
