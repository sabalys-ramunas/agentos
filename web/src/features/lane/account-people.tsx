/**
 * Who holds this account now and who waits behind it, and whether an agent read outside text on it, so its
 * next post asks first. Both are shown only when they have something to say. Clearing the taint is the
 * owner's, typed and confirmed, and the node asks for a fresh sign-in besides.
 */
import { useState } from 'react';
import { useAccess, useClearTaint, useLaneQueue, useLaneTaint } from '@/api/hooks';
import type { Lane } from '@/api/types';
import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/dialog';
import { describeError } from '@/lib/errors';
import { AccountSection, Answer } from './account-section';
import { queueSentence, taintSentence } from './account-words';
import { InlineError } from '@/components/ui/states';

/** Who holds the account, and who is queued behind it. Polled, since it moves with every call. */
export function WaitingSection({ org, lane }: { org: string; lane: Lane }) {
  const queue = useLaneQueue(org, lane.workspace, lane.name).data;
  if (!queue || (!queue.claim && !queue.queue.length)) return null;
  const { lead, waiting } = queueSentence(queue);
  return (
    <AccountSection id="account-waiting" title="Who is using it">
      <Answer tone="neutral" word={queue.claim ? (queue.claim.yours ? 'You are using it' : 'In use now') : 'Waiting its turn'}>
        {lead}
      </Answer>
      {waiting ? <p className="text-secondary text-muted">{waiting}</p> : null}
    </AccountSection>
  );
}

/** What an agent read on this account, and what it changes for the next post. Shown only while it is tainted. */
export function TaintSection({ org, lane }: { org: string; lane: Lane }) {
  const taint = useLaneTaint(org, lane.workspace, lane.name).data;
  const owner = useAccess(org).can('taint.clear');
  const clear = useClearTaint(org);
  const [confirming, setConfirming] = useState(false);
  const family = taint?.families[0];
  if (!taint?.tainted || !family) return null;
  const grant = family.grant_id;
  return (
    <AccountSection id="account-taint" title="Outside text">
      <Answer tone="warn" word="Asks first for now">
        {taintSentence(family, owner)}
      </Answer>
      <p className="text-secondary text-faint">Until it is cleared, posts, payments and anything that cannot be undone wait for a person.</p>
      {owner && grant ? (
        <div className="flex flex-wrap items-center justify-between gap-3">
          <p className="text-secondary text-muted">Clearing it lets the next post go on its own again.</p>
          <Button size="sm" onClick={() => setConfirming(true)}>
            Clear it
          </Button>
        </div>
      ) : null}
      {clear.isError ? (
        <InlineError>
          {describeError(clear.error)}
        </InlineError>
      ) : null}
      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title="Clear what this account read?"
        description="The agent's next post will go without asking, as it did before it read the page. Only clear this if you have read the page and trust it."
        confirmLabel="Clear it"
        typeToConfirm="clear"
        loading={clear.isPending}
        onConfirm={() => {
          if (!grant) return;
          clear.mutate(grant, { onSuccess: () => setConfirming(false) });
        }}
      />
    </AccountSection>
  );
}
