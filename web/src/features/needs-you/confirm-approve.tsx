/**
 * The second, deliberate step before approving something heavy, or something that changed after
 * it was first shown. The phone and the desk both use it, so the same item never takes one tap
 * in one place and two in the other (security review 12).
 */
import { useEffect, useState } from 'react';
import type { Approval, Effect } from '@/api/types';
import { ProveYes } from '@/features/confirmations/prove-yes';
import { needsProof } from '@/lib/confirm';
import { useShown } from './decision-card';
import { Button } from '@/components/ui/button';
import { Dialog } from '@/components/ui/dialog';

/** Long enough that reading the confirm step comes first. */
const ARM_MS = 700;

/** What a heavy approval does, in plain words. These are never approved in a batch. */
export const HEAVY: Partial<Record<Effect, { label: string; consequence: string }>> = {
  pay: { label: 'Pay', consequence: 'This spends real money, and it cannot be taken back from here.' },
  irreversible: { label: 'Do it', consequence: 'This cannot be undone.' },
  install_base: { label: 'Make the change', consequence: 'This changes what several accounts on the computer start from, all at once.' },
};

const CHANGED = {
  label: 'Yes, this version',
  consequence: 'It changed after you first saw it. Say yes only if the version on screen is what you want.',
};

export const isHeavy = (approval: Approval) => HEAVY[approval.effect] !== undefined;

/**
 * The confirm button sits where the first tap landed, so it stays disabled for a moment. A double
 * tap, or a key held from the list, can never pass through this.
 */
export function ConfirmApprove({
  approval,
  changed = false,
  large = false,
  yes,
  question,
  onConfirm,
  onClose,
}: {
  approval: Approval | null;
  changed?: boolean;
  large?: boolean;
  /** The yes button, named for the action: "Pay $40". */
  yes?: string;
  /** The plain question the item asks. */
  question?: string;
  onConfirm: () => void;
  onClose: () => void;
}) {
  const [armed, setArmed] = useState(false);
  const heavy = approval ? HEAVY[approval.effect] : undefined;
  const copy = heavy ?? CHANGED;
  const label = heavy && yes ? yes : copy.label;
  useEffect(() => {
    if (!approval) return;
    setArmed(false);
    const timer = setTimeout(() => setArmed(true), ARM_MS);
    return () => clearTimeout(timer);
  }, [approval]);
  const size = large ? 'lg' : 'md';
  return (
    <Dialog
      open={approval !== null}
      onOpenChange={(open) => !open && onClose()}
      title={heavy ? 'Are you sure?' : copy.label}
      description={question ?? approval?.summary}
      footer={
        <>
          <Button size={size} className={large ? 'h-12 flex-1' : undefined} onClick={onClose}>
            Go back
          </Button>
          <Button variant="primary" size={size} className={large ? 'h-12 flex-1' : undefined} disabled={!armed} onClick={onConfirm}>
            {label}
          </Button>
        </>
      }
    >
      <p className="text-body text-muted">{copy.consequence}</p>
      {heavy && changed ? <p className="mt-2 text-body text-muted">{CHANGED.consequence}</p> : null}
    </Dialog>
  );
}

/** The second step for money, the irreversible and changed items, worded for the action. */
export function ConfirmBox({
  org,
  approval,
  business,
  changed,
  large,
  onClose,
  onConfirm,
}: {
  org: string;
  approval: Approval | null;
  business: string | null;
  changed: boolean;
  large?: boolean;
  onClose: () => void;
  onConfirm: (approval: Approval) => void;
}) {
  return approval ? (
    <ConfirmFor org={org} approval={approval} business={business} changed={changed} large={large} onClose={onClose} onConfirm={onConfirm} />
  ) : (
    <ConfirmApprove approval={null} onClose={onClose} onConfirm={() => undefined} />
  );
}

function ConfirmFor({
  org,
  approval,
  business,
  changed,
  large,
  onClose,
  onConfirm,
}: {
  org: string;
  approval: Approval;
  business: string | null;
  changed: boolean;
  large?: boolean;
  onClose: () => void;
  onConfirm: (approval: Approval) => void;
}) {
  const shown = useShown(org, approval, business);
  if (needsProof(approval)) {
    return (
      <ProveYes
        org={org}
        approval={approval}
        question={shown.question}
        yes={shown.verbs.yes}
        noMeans={shown.verbs.noMeans}
        changed={changed}
        large={large}
        onClose={onClose}
        onConfirm={() => onConfirm(approval)}
      />
    );
  }
  return (
    <ConfirmApprove
      approval={approval}
      changed={changed}
      large={large}
      yes={shown.verbs.yes}
      question={shown.question}
      onClose={onClose}
      onConfirm={() => onConfirm(approval)}
    />
  );
}
