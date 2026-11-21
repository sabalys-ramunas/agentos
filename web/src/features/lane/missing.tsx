/**
 * A lane its node has stopped reporting. It stays on that node, so no other node can take its id,
 * until an admin releases it (security review 26).
 */
import { useState } from 'react';
import { useAccess, useReleaseLane } from '@/api/hooks';
import type { Lane } from '@/api/types';
import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/dialog';
import { describeError } from '@/lib/errors';
import { computerName } from '@/lib/computers';
import { dateTime } from '@/lib/format';
import { Banner, InlineError } from '@/components/ui/states';

export function MissingLane({ org, lane }: { org: string; lane: Lane }) {
  const canRelease = useAccess(org).can('nodes.manage');
  const release = useReleaseLane(org, lane.workspace, lane.name);
  const [confirming, setConfirming] = useState(false);
  if (!lane.missing_since) return null;
  return (
    <>
      <Banner
        tone="warn"
        title={`Not reported by ${computerName(lane.node)} since ${dateTime(lane.missing_since)}.`}
        action={
          canRelease ? (
            <Button size="sm" onClick={() => setConfirming(true)}>
              Release this account
            </Button>
          ) : null
        }
      >
        It stays on that computer, so no other computer can take it, until an admin releases it.
      </Banner>
      {release.isError ? <InlineError>{describeError(release.error)}</InlineError> : null}
      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title="Release this account?"
        description={`${computerName(lane.node)} no longer reports it. Releasing frees its name, so another computer can report an account by the same name. Its history stays.`}
        confirmLabel="Release account"
        loading={release.isPending}
        onConfirm={() => release.mutate(undefined, { onSettled: () => setConfirming(false) })}
      />
    </>
  );
}
