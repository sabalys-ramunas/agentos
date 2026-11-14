/**
 * On a lane's page: which standing approvals cover it, whether its node applies them, and whether
 * the limit holds here at all: an account that runs on its own uses none of it.
 */
import { Link } from '@tanstack/react-router';
import { useFlows, useLaneAskFirst, useStandingApprovals } from '@/api/hooks';
import type { Lane } from '@/api/types';
import { StatusDot } from '@/components/ui/status';
import { accountName } from '@/features/home/words';
import { covers, describeStanding, limitGap } from './describe';
import { LimitNote } from './enforcement';

export function LaneStanding({ org, lane }: { org: string; lane: Lane }) {
  const items = (useStandingApprovals(org).data ?? []).filter((item) => item.state === 'active' && covers(item, lane.id));
  const asks = useLaneAskFirst(org).data?.filter((item) => item.lane === lane.id);
  const flows = useFlows(org).data?.filter((item) => item.lane === lane.id);
  if (!items.length) return null;
  const gaps = items.map((item) => limitGap(item, asks, flows));
  const gap = gaps.find(Boolean) ?? null;
  const applied = items.every((item) => item.enforcement.applied_by.includes(lane.node));
  return (
    <section aria-label="Always allowed on this account">
      <h2 className="mb-2 text-13 text-faint">Standing approvals</h2>
      <ul className="grid gap-2">
        {items.map((item) => (
          <li key={item.id} className="text-13">
            <span className="text-fg">{describeStanding(item)}</span>
            <span className="mono tabular text-faint"> · {item.count === null ? `${item.used} used` : `${item.used} of ${item.count}`}</span>
          </li>
        ))}
      </ul>
      {gap ? (
        <div className="mt-2">
          <LimitNote org={org} gap={gap} nameOf={() => accountName(lane)} />
        </div>
      ) : null}
      {applied ? null : (
        <p className="mt-2 flex items-baseline gap-2 text-13">
          <StatusDot tone="warn" className="translate-y-px" />
          <span className="min-w-0 text-muted">
            <span className="mono text-fg">{lane.node}</span> does not apply these rules yet, so whatever asks first here still asks a person each time.{' '}
            <Link to="/$org/settings/standing" params={{ org }} className="text-fg hover:underline">
              See them all
            </Link>
          </span>
        </p>
      )}
    </section>
  );
}
