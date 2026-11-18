/**
 * A lane its node has stopped reporting. It stays on that node, so no other node can take its id,
 * until an admin releases it (security review 26).
 */
import { useState } from 'react';
import { useAccess, useReleaseLane } from '@/api/hooks';
import type { Lane } from '@/api/types';
import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/dialog';
import { StatusDot } from '@/components/ui/status';
import { describeError } from '@/lib/errors';
import { dateTime } from '@/lib/format';
import { InlineError } from '@/components/ui/states';

export function MissingLane({ org, lane }: { org: string; lane: Lane }) {
  const canRelease = useAccess(org).can('nodes.manage');
  const release = useReleaseLane(org, lane.workspace, lane.name);
  const [confirming, setConfirming] = useState(false);
  if (!lane.missing_since) return null;
  return (
    <div role="status" className="flex flex-wrap items-start gap-x-4 gap-y-2 rounded-xl border border-line px-4 py-3">
      <p className="flex min-w-0 flex-1 basis-64 items-baseline gap-2 text-14">
        <StatusDot tone="warn" className="translate-y-px" />
        <span className="min-w-0">
          <span className="font-medium">
            Not reported by <span className="mono">{lane.node}</span> since {dateTime(lane.missing_since)}.
          </span>{' '}
          <span className="text-muted">It stays on that computer, so no other computer can take it, until an admin releases it.</span>
        </span>
      </p>
      {canRelease ? (
        <Button size="sm" onClick={() => setConfirming(true)}>
          Release this account
        </Button>
      ) : null}
      {release.isError ? (
        <InlineError className="w-full">
          {describeError(release.error)}
        </InlineError>
      ) : null}
      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title="Release this account?"
        description={`${lane.node} no longer reports it. Releasing frees the name, so another node can report a lane called ${lane.id}. Its history stays.`}
        confirmLabel="Release account"
        loading={release.isPending}
        onConfirm={() => release.mutate(undefined, { onSettled: () => setConfirming(false) })}
      />
    </div>
  );
}
