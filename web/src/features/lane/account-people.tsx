/**
 * Whether an agent read outside text on this account, so its next post asks first. Shown only while it has. Clearing
 * it is the owner's, typed and confirmed, and the node asks for a fresh sign-in besides. Who holds the account is in
 * account-panels.tsx.
 */
import { useState } from 'react';
import { useAccess, useClearTaint, useLaneTaint } from '@/api/hooks';
import type { Lane } from '@/api/types';
import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/dialog';
import { describeError } from '@/lib/errors';
import { taintSentence } from './account-words';
import { Banner, InlineError } from '@/components/ui/states';

/**
 * What an agent read on this account, and what it changes for the next post, as an amber banner across the band.
 * Shown only while it is tainted.
 */
export function TaintBanner({ org, lane }: { org: string; lane: Lane }) {
  const taint = useLaneTaint(org, lane.workspace, lane.name).data;
  const owner = useAccess(org).can('taint.clear');
  const clear = useClearTaint(org);
  const [confirming, setConfirming] = useState(false);
  const family = taint?.families[0];
  if (!taint?.tainted || !family) return null;
  const grant = family.grant_id;
  return (
    <>
      <Banner
        tone="warn"
        title="Asks first for now."
        action={
          owner && grant ? (
            <Button size="sm" onClick={() => setConfirming(true)}>
              Clear it
            </Button>
          ) : null
        }
      >
        <span>{taintSentence(family, owner)}</span>
      </Banner>
      {clear.isError ? <InlineError>{describeError(clear.error)}</InlineError> : null}
      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title="Clear what this account read?"
        description="Until it is cleared, posts, payments and anything that cannot be undone wait for a person. Cleared, the agent's next post goes without asking, as it did before it read the page. Only clear this if you have read the page and trust it."
        confirmLabel="Clear it"
        typeToConfirm="clear"
        loading={clear.isPending}
        onConfirm={() => {
          if (!grant) return;
          clear.mutate(grant, { onSuccess: () => setConfirming(false) });
        }}
      />
    </>
  );
}
