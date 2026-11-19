/**
 * A link to watch this account for a short time, for someone who is not signed in here. It is shown once, with
 * a copy button, and never kept on the page. Shown only while the account is awake, since there is a screen to watch.
 */
import { useAccess, useLaneViewLink, useProviders } from '@/api/hooks';
import type { Lane } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/field';
import { CopyButton } from '@/components/ui/misc';
import { describeError } from '@/lib/errors';
import { timeUntil } from '@/lib/format';
import { AccountSection } from './account-section';
import { InlineError } from '@/components/ui/states';

export function WatchSection({ org, lane }: { org: string; lane: Lane }) {
  const canWatch = useAccess(org).can('lanes.watch');
  const link = useLaneViewLink(org, lane.workspace, lane.name);
  const awake = lane.state === 'warm' || lane.state === 'active';
  // A view link is made by the computer that runs the account. The cloud cannot make one.
  const onComputer = useProviders().data?.code === true;
  if (!canWatch || !awake || !onComputer) return null;
  const made = link.data;
  const absolute = made ? `${window.location.origin}${made.url}` : '';
  return (
    <AccountSection id="account-watch" title="Watch link">
      {made ? (
        <div className="grid gap-2">
          <p className="text-secondary text-muted">
            Anyone with this link can watch {lane.name} for {timeUntil(made.expires_at) || 'a short time'}, read only. It is shown once, so copy it now.
          </p>
          <div className="flex items-center gap-2">
            <Input mono readOnly aria-label="Watch link" value={absolute} onFocus={(event) => event.currentTarget.select()} className="min-w-0 flex-1" />
            <CopyButton text={absolute} label="Copy the watch link" size="md" />
          </div>
          <Button size="sm" className="justify-self-start" onClick={() => link.reset()}>
            Done
          </Button>
        </div>
      ) : (
        <div className="grid gap-2">
          <p className="text-secondary text-muted">Show someone what this account is doing, for a short time. They do not need to sign in.</p>
          <Button size="sm" className="justify-self-start" loading={link.isPending} onClick={() => link.mutate()}>
            Make a watch link
          </Button>
          {link.isError ? (
            <InlineError>
              {describeError(link.error)}
            </InlineError>
          ) : null}
        </div>
      )}
    </AccountSection>
  );
}
