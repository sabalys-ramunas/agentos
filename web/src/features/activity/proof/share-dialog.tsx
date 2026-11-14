import { useState } from 'react';
import { useCreateShare } from '@/api/hooks';
import type { ProofShareCreated, ProofShareInput } from '@/api/types';
import { Button } from '@/components/ui/button';
import { CopyButton } from '@/components/ui/misc';
import { Dialog } from '@/components/ui/dialog';
import { Checkbox } from '@/components/ui/toggle';
import { Label } from '@/components/ui/field';
import { Select } from '@/components/ui/select';
import { dateTime } from '@/lib/format';
import { laneName } from '@/lib/labels';
import { dayTitle } from './words';

/** An account's name from its id, which is `workspace/name`. */
export const accountOf = (lane: string) => lane.split('/').pop() ?? lane;

/** What a link opens: one account's day, or one step of it. */
export type ShareTarget = { kind: 'day'; lane: string; day: string } | { kind: 'step'; trace: string; summary: string };

const TTLS = [
  { value: '24h', label: '24 hours', hint: 'The default. Then the link stops working.' },
  { value: '7d', label: '7 days', hint: 'The longest a link can last.' },
];

/**
 * Making a read-only link. The owner picks how long it lasts and whether the proof screenshots go
 * with it. The token is shown once, here, so the dialog keeps it only while it is open.
 */
export function ShareDialog({ org, target, onClose }: { org: string; target: ShareTarget; onClose: () => void }) {
  const create = useCreateShare(org);
  const [ttl, setTtl] = useState('24h');
  const [screenshots, setScreenshots] = useState(false);
  const [made, setMade] = useState<ProofShareCreated | null>(null);

  const what = target.kind === 'day' ? `${laneName(accountOf(target.lane))} on ${dayTitle(target.day)}` : target.summary;
  const body: ProofShareInput =
    target.kind === 'day' ? { lane: target.lane, day: target.day, ttl, screenshots } : { trace: target.trace, ttl, screenshots };

  const submit = async () => {
    const result = await create.mutateAsync(body);
    setMade(result);
  };

  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      title={made ? 'Your link' : 'Share proof'}
      description={made ? undefined : `A read-only link to ${what}. Anyone with it can read these steps, with no sign-in.`}
      footer={
        made ? (
          <Button variant="primary" onClick={onClose}>
            Done
          </Button>
        ) : (
          <>
            <Button variant="ghost" onClick={onClose}>
              Cancel
            </Button>
            <Button variant="primary" loading={create.isPending} onClick={() => void submit()}>
              Create link
            </Button>
          </>
        )
      }
    >
      {made ? (
        <div className="grid gap-4">
          <div className="flex items-center gap-2 rounded-lg border border-line bg-subtle p-3">
            <code className="mono min-w-0 flex-1 break-all text-13">{made.url}</code>
            <CopyButton text={made.url} label="Copy link" size="md" />
          </div>
          <p className="text-13 text-muted">
            Shown once, so copy it now. It works until {dateTime(made.expires_at)}, and you can stop it from the list of links.
          </p>
        </div>
      ) : (
        <div className="grid gap-5">
          <div className="grid gap-2">
            <Label htmlFor="share-ttl">Link lasts</Label>
            <Select id="share-ttl" value={ttl} onValueChange={setTtl} options={TTLS} />
          </div>
          <div className="flex items-start gap-3">
            <Checkbox id="share-shots" checked={screenshots} onCheckedChange={(next) => setScreenshots(next === true)} className="mt-1" />
            <div className="grid gap-1">
              <Label htmlFor="share-shots" className="font-normal">
                Include the proof screenshots
              </Label>
              <p className="text-13 text-muted">Off by default. A screenshot can show what was on the account&apos;s page.</p>
            </div>
          </div>
          {create.isError ? (
            <p role="alert" className="text-13 text-red">
              That link was not made. Nothing was shared. {create.error instanceof Error ? create.error.message : ''}
            </p>
          ) : null}
        </div>
      )}
    </Dialog>
  );
}
