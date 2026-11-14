/**
 * Freeze (break glass): one confirmation, then every agent in the scope stops at once, whatever its
 * grant. Grants that reach only the scope are revoked (every grant, for the whole computer). The browser stays as it is, so a person can take over and look. Only the
 * owner lifts a freeze, and lifting restores nothing, so the dialog says so before the press.
 *
 * The buttons are shown only to the people the node lets freeze (`freeze.account`, `freeze.computer`,
 * `freeze.lift` in lib/permissions.ts). The node refuses anyone else all the same.
 */
import { useState } from 'react';
import { toast } from 'sonner';
import { useFreezeNow, useUnfreeze } from '@/api/hooks';
import type { FreezeRecord, FreezeScope } from '@/api/types';
import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/dialog';
import { Field, Input } from '@/components/ui/field';
import { cn } from '@/lib/cn';
import { describeError } from '@/lib/errors';
import { frozenSentence, NO_REASON, reasonOf, SCOPE_WORDS } from '@/lib/freeze';
import { FREEZE_WORDS, LIFTING_A_FREEZE } from '@/lib/vocabulary';

/** Plain, short lines: what a freeze does, as a person reads it. */
function Consequences({ what, children }: { what: string; children?: React.ReactNode }) {
  return (
    <ul className="grid gap-2 text-14 text-muted">
      <li>Every agent on {what} stops at once.</li>
      <li>No agent can act on {what} until you lift the freeze. Access that reaches only {what} is cancelled for good.</li>
      <li>The browser is kept as it is, so you can take over and look. Nothing is closed or deleted.</li>
      {children}
    </ul>
  );
}

interface FreezeDialogProps {
  org: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** `lane` freezes one account, `node` the whole computer this app runs on. */
  scope: 'lane' | 'node';
  target?: string;
  /** The account or the computer, by name, for the title. */
  name: string;
}

export function FreezeDialog({ org, open, onOpenChange, scope, target, name }: FreezeDialogProps) {
  const freeze = useFreezeNow(org);
  const [reason, setReason] = useState('');
  const what = scope === 'node' ? 'this machine' : 'this account';
  const close = (next: boolean) => {
    if (!next) {
      setReason('');
      freeze.reset();
    }
    onOpenChange(next);
  };
  const send = () =>
    freeze.mutate(
      { scope, target: scope === 'lane' ? target : undefined, reason: reason.trim() || NO_REASON },
      {
        onSuccess: () => {
          toast('Frozen. Agents there are stopped.');
          close(false);
        },
      },
    );
  return (
    <ConfirmDialog
      open={open}
      onOpenChange={close}
      title={`Freeze ${name}?`}
      description="Use it when something looks wrong. You can lift it later, as the owner."
      confirmLabel="Freeze now"
      danger
      loading={freeze.isPending}
      onConfirm={send}
    >
      <div className="grid gap-6">
        <Consequences what={what}>
          <li>Schedules and routines that run there are paused, and runs in progress are cancelled.</li>
        </Consequences>
        <Field label="Why (optional)" hint="Kept with the record of the freeze.">
          <Input maxLength={300} value={reason} onChange={(event) => setReason(event.target.value)} />
        </Field>
        {freeze.isError ? (
          <p role="alert" className="text-13 text-red">
            {describeError(freeze.error)}
          </p>
        ) : null}
      </div>
    </ConfirmDialog>
  );
}

interface UnfreezeDialogProps {
  org: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  scope: FreezeScope;
  target: string;
  /** What is lifted, by name, for the title. */
  name: string;
}

/** Owner only. Nothing is restored: the dialog says what stays as it is. */
export function UnfreezeDialog({ org, open, onOpenChange, scope, target, name }: UnfreezeDialogProps) {
  const unfreeze = useUnfreeze(org);
  const close = (next: boolean) => {
    if (!next) unfreeze.reset();
    onOpenChange(next);
  };
  return (
    <ConfirmDialog
      open={open}
      onOpenChange={close}
      title={`Unfreeze ${name}?`}
      description={LIFTING_A_FREEZE}
      confirmLabel={FREEZE_WORDS.unfreeze.word}
      loading={unfreeze.isPending}
      onConfirm={() =>
        unfreeze.mutate(
          { scope, target },
          {
            onSuccess: (result) => {
              if (result.still_frozen_by) {
                toast(`Unfrozen, but still frozen by ${SCOPE_WORDS[result.still_frozen_by.scope]}, so agents stay stopped there.`);
              } else {
                toast('Unfrozen. Grant agents again if they need access.');
              }
              close(false);
            },
          },
        )
      }
    >
      <p className="text-13 text-muted">
        To lift it, confirm it is you first. With a passkey, that is one tap. With no passkey, sign in again with a code, and do it in the last five minutes.
      </p>
      {unfreeze.isError ? (
        <p role="alert" className="mt-3 text-13 text-red">
          {describeError(unfreeze.error)}
        </p>
      ) : null}
    </ConfirmDialog>
  );
}

/** The button that starts a freeze. It is the danger style: a red label on a hairline, never a fill. */
export function FreezeButton({ org, scope, target, name }: Omit<FreezeDialogProps, 'open' | 'onOpenChange'>) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button variant="danger" onClick={() => setOpen(true)}>
        {FREEZE_WORDS.name.word}
      </Button>
      <FreezeDialog org={org} open={open} onOpenChange={setOpen} scope={scope} target={target} name={name} />
    </>
  );
}

/**
 * Says that a freeze stops this, who started it and why, and offers the owner the way out. It sits
 * above the page's live view, so it is read before anyone takes over.
 */
export function FrozenNotice({
  org,
  record,
  name,
  canLift,
  className,
}: {
  org: string;
  record: FreezeRecord;
  /** What the freeze lifts, by name: the account, the workspace or the computer. */
  name: string;
  canLift: boolean;
  className?: string;
}) {
  const [lifting, setLifting] = useState(false);
  return (
    <div role="status" className={cn('flex flex-wrap items-start gap-x-6 gap-y-3 rounded-xl border border-line px-4 py-3', className)}>
      <div className="min-w-0 flex-1">
        <p className="text-14 font-medium">Agents are stopped on {SCOPE_WORDS[record.scope]}.</p>
        <p className="text-13 text-muted">
          {frozenSentence(record)} Their grants stay revoked until someone grants them again.
        </p>
        <p className="mt-1 text-13 text-faint break-words">Reason: {reasonOf(record)}</p>
      </div>
      {canLift ? (
        <>
          <Button onClick={() => setLifting(true)}>{FREEZE_WORDS.unfreeze.word}</Button>
          <UnfreezeDialog org={org} open={lifting} onOpenChange={setLifting} scope={record.scope} target={record.target} name={name} />
        </>
      ) : null}
    </div>
  );
}
