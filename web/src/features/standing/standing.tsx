import { SegmentedControl } from '@/components/ui/tabs';
import { useNavigate, useParams, useSearch } from '@tanstack/react-router';
import { Plus, Stamp } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';
import { useAccess, useFlows, useLaneAskFirst, useRevokeStandingApproval, useStandingApprovals, useWorkspaces } from '@/api/hooks';
import { isNotServed } from '@/api/http';
import type { StandingApproval } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { ConfirmDialog } from '@/components/ui/dialog';
import { Status, StatusDot } from '@/components/ui/status';
import { EmptyState, InlineError, QueryView, Skeleton } from '@/components/ui/states';
import { cn } from '@/lib/cn';
import { date, timeUntil } from '@/lib/format';
import { laneName, standingState } from '@/lib/labels';
import { accountName } from '@/features/home/words';
import { SettingsHeader } from '../settings/page';
import { describeStanding, lanesWords, limitGap } from './describe';
import type { LimitGap } from './describe';
import { EnforcementNote, LimitNote } from './enforcement';
import { StandingDialog } from './standing-dialog';

/** Used of allowed, as a plain bar with the numbers beside it. Running out is not a fault. */
function Usage({ item }: { item: StandingApproval }) {
  const pct = item.count ? Math.min(100, (item.used / item.count) * 100) : 0;
  // A limit the node does not keep (null) is no bar: the count says what was used, and nothing more.
  return (
    <div className="flex items-center gap-4">
      <div
        role="meter"
        aria-label="Used"
        aria-valuenow={item.used}
        aria-valuemin={0}
        aria-valuemax={item.count ?? undefined}
        className="h-1 flex-1 overflow-hidden rounded-full bg-pressed"
      >
        <div className="h-full rounded-full bg-fg transition-[width] duration-(--duration-slow) ease-standard" style={{ width: `${pct}%` }} />
      </div>
      <span className="mono tabular shrink-0 text-caption text-muted">
        {item.count === null ? `${item.used} used` : `${item.used} of ${item.count} used`}
      </span>
    </div>
  );
}

interface ItemProps {
  org: string;
  item: StandingApproval;
  workspaceName: string;
  onRevoke: () => void;
  canRevoke: boolean;
  gap: LimitGap | null;
  nameOf: (lane: string) => string;
}

function Item({ org, item, workspaceName, onRevoke, canRevoke, gap, nameOf }: ItemProps) {
  const active = item.state === 'active';
  const ends = active ? `Ends in ${timeUntil(item.until)}, ${date(item.until)}` : item.state === 'revoked' && item.revoked_at ? `Revoked ${date(item.revoked_at)}` : `Ended ${date(item.until)}`;
  return (
    <li className={cn('grid gap-4 p-6', !active && 'text-muted')}>
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <p className={cn('text-label font-medium break-words', active ? 'text-fg' : 'text-muted')}>{describeStanding(item)}</p>
          <p className="mt-1 text-secondary text-faint">
            {workspaceName} · {lanesWords(item.lanes, workspaceName)}
            {item.per_day ? ` · at most ${item.per_day} a day` : ''}
            {item.contents ? ` · ${item.contents === 1 ? 'one exact content' : `${item.contents} exact contents`}` : ''}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Status status={standingState[item.state]} />
          {active && canRevoke ? (
            <Button size="sm" variant="ghost" onClick={onRevoke}>
              Revoke
            </Button>
          ) : null}
        </div>
      </div>
      <Usage item={item} />
      <div className="grid gap-2">
        {gap ? <LimitNote org={org} gap={gap} nameOf={nameOf} /> : null}
        {active ? <EnforcementNote enforcement={item.enforcement} /> : null}
        <p className="tabular text-secondary text-faint">{ends}</p>
        {item.note ? <p className="text-secondary break-words text-muted">{item.note}</p> : null}
      </div>
    </li>
  );
}

/** Said once at the top when nothing listed is applied anywhere, so the page never reads as cover. */
function NothingApplied({ items }: { items: StandingApproval[] }) {
  const active = items.filter((item) => item.state === 'active');
  if (!active.length || active.some((item) => item.enforcement.state === 'applied' || item.enforcement.state === 'partial')) return null;
  return (
    <div role="note" className="mb-6 flex items-baseline gap-2 rounded-xl border border-line bg-inset px-6 py-4 text-body">
      <StatusDot tone="warn" className="translate-y-px" />
      <p className="min-w-0">
        <span className="font-medium">No computer applies these yet.</span>{' '}
        <span className="text-muted">Whatever asks first still comes to Needs you and waits for a person. They are kept here, with their limits, for when computers do.</span>
      </p>
    </div>
  );
}

const VIEWS = [
  { value: 'active', label: 'Active' },
  { value: 'all', label: 'All' },
] as const;

export function StandingSettings() {
  const { org } = useParams({ from: '/$org/settings/standing' });
  const { show } = useSearch({ from: '/$org/settings/standing' });
  const navigate = useNavigate();
  const all = show === 'all';
  const access = useAccess(org);
  const standing = useStandingApprovals(org, all);
  const workspaces = useWorkspaces(org).data ?? [];
  const asks = useLaneAskFirst(org).data;
  const flows = useFlows(org).data;
  const revoke = useRevokeStandingApproval(org);
  const [creating, setCreating] = useState(false);
  const [revoking, setRevoking] = useState<StandingApproval | null>(null);
  const canCreate = access.can('standing_approvals.create');
  const canRevoke = access.can('grants.manage');
  const nameOf = (id: string) => workspaces.find((item) => item.id === id)?.name ?? id;
  const accountOf = (id: string) => {
    const lane = workspaces.flatMap((item) => item.lanes).find((item) => item.id === id);
    return lane ? accountName(lane) : laneName(id.split('/')[1] ?? id);
  };

  return (
    <>
      <SettingsHeader
        title="Standing approvals"
        description="Approve one kind of thing ahead of time, a set number of times, until a date. Payments, irreversible actions and base images are never covered."
        action={
          canCreate && !isNotServed(standing.error) ? (
            <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setCreating(true)}>
              New
            </Button>
          ) : undefined
        }
      />
      <div className="mb-4">
        <SegmentedControl
          label="Show"
          value={all ? 'all' : 'active'}
          options={[...VIEWS]}
          onValueChange={(value) => void navigate({ to: '.', search: value === 'all' ? { show: 'all' } : {}, replace: true, resetScroll: false })}
        />
      </div>
      <QueryView
        card
        query={standing}
        skeleton={
          <Card>
            {[0, 1].map((index) => (
              <div key={index} className="grid gap-4 border-b border-line p-6 last:border-b-0">
                <Skeleton className="w-1/2" />
                <Skeleton className="h-1" />
                <Skeleton className="h-3 w-2/3" />
              </div>
            ))}
          </Card>
        }
      >
        {(items) => (
          <>
            <NothingApplied items={items} />
            {items.length ? (
              <Card>
                <ul className="divide-y divide-line">
                  {items.map((item) => (
                    <Item key={item.id} org={org} item={item} workspaceName={nameOf(item.workspace)} canRevoke={canRevoke} onRevoke={() => setRevoking(item)} gap={limitGap(item, asks, flows)} nameOf={accountOf} />
                  ))}
                </ul>
              </Card>
            ) : (
              <Card>
                <EmptyState
                  icon={<Stamp />}
                  title={all ? 'No standing approvals yet' : 'No active standing approvals'}
                  description="What asks you first comes to Needs you. A standing approval can take a routine one off your plate, once computers apply them."
                  action={
                    canCreate ? (
                      <Button variant="primary" onClick={() => setCreating(true)}>
                        New standing approval
                      </Button>
                    ) : undefined
                  }
                />
              </Card>
            )}
          </>
        )}
      </QueryView>
      {revoke.isError ? (
        <InlineError className="mt-4">
          That did not go through, so it is still active. Try again.
        </InlineError>
      ) : null}
      {creating ? (
        <StandingDialog org={org} open={creating} onOpenChange={setCreating} onCreated={() => toast('Standing approval created')} />
      ) : null}
      <ConfirmDialog
        open={revoking !== null}
        onOpenChange={(open) => !open && setRevoking(null)}
        title="Revoke this standing approval?"
        description="It stops now, and is kept in the record with who revoked it and when."
        confirmLabel="Revoke"
        danger
        onConfirm={() => {
          if (!revoking) return;
          revoke.mutate(revoking.id, { onSuccess: () => toast('Revoked') });
          setRevoking(null);
        }}
      />
    </>
  );
}
