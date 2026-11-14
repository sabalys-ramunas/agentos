import { Link } from '@tanstack/react-router';
import { useAccess, useStatus } from '@/api/hooks';
import type { PlanLimits, PlanUsage } from '@/api/types';
import { plural } from '@/lib/format';
import { StatusDot } from './ui/status';

type Resource = keyof PlanUsage;

const NOUN: Record<Resource, string> = {
  members: 'seat',
  workspaces: 'workspace',
  nodes: 'node',
  active_lanes: 'active account',
};

const planName = (plan: string) => `${plan.charAt(0).toUpperCase()}${plan.slice(1)}`;

function Upgrade({ org }: { org: string }) {
  const access = useAccess(org);
  return access.can('billing.manage') ? (
    <Link to="/$org/settings/billing" params={{ org }} className="link">
      See plans
    </Link>
  ) : (
    <span>An owner can upgrade the plan.</span>
  );
}

/** How much room is left. `reserved` counts what is promised but not used, such as open invites. */
export function usePlanRoom(org: string, resource: Resource, reserved = 0) {
  const status = useStatus(org).data;
  const limit = status?.limits?.[resource as keyof PlanLimits];
  const used = (status?.usage?.[resource] ?? 0) + reserved;
  return { full: limit !== undefined && used >= limit, limit, used, plan: status?.plan ?? '' };
}

/** Says a limit has been reached before an action can fail on it. Calm, with the way forward. */
export function PlanNotice({ org, resource, reserved = 0 }: { org: string; resource: Resource; reserved?: number }) {
  const room = usePlanRoom(org, resource, reserved);
  if (!room.full || room.limit === undefined) return null;
  return (
    <p role="status" className="flex flex-wrap items-center gap-x-2 rounded-lg border border-line bg-subtle px-4 py-3 text-14 text-muted">
      <StatusDot tone="warn" />
      <span>
        The {planName(room.plan)} plan includes {plural(room.limit, NOUN[resource])}, and all are in use.
      </span>
      <Upgrade org={org} />
    </p>
  );
}

/** After a downgrade the extras go read-only. Nothing is deleted, and this says so. */
export function OverLimitBanner({ org }: { org: string }) {
  const status = useStatus(org).data;
  if (!status?.usage || !status.limits) return null;
  const over = status.usage.workspaces > status.limits.workspaces;
  if (!over) return null;
  return (
    <p role="status" className="flex flex-wrap items-center gap-x-2 rounded-xl border border-line px-4 py-3 text-14 text-muted">
      <StatusDot tone="warn" />
      <span>
        This org has {status.usage.workspaces} workspaces and the {planName(status.plan ?? '')} plan includes {status.limits.workspaces}. The
        newest are read-only, and nothing was deleted.
      </span>
      <Upgrade org={org} />
    </p>
  );
}
