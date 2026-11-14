/**
 * The step up, as one moment: what you are saying yes to, why it takes more than a tap, and the
 * proof itself. It replaces "Are you sure?" for anything that needs one, because proving it is you
 * is the deliberate step. Yes goes through only after the check; a failed check decides nothing.
 */
import { Check, Fingerprint, Laptop, X } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useProveYes, useProviders } from '@/api/hooks';
import type { Approval } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Dialog } from '@/components/ui/dialog';
import { whyProof } from '@/lib/confirm';
import './confirmations.css';

/** Long enough that reading comes first, and a double click can't pass through. */
const ARM_MS = 700;
/** The check on the ring stays a moment, so the proof is seen to land. */
const PROVED_MS = 420;

type Stage = 'ready' | 'checking' | 'proved' | 'failed' | 'elsewhere';

interface Props {
  org: string;
  approval: Approval;
  question: string;
  /** "Pay €412.50". */
  yes: string;
  /** "Nothing is paid." */
  noMeans: string;
  changed?: boolean;
  large?: boolean;
  onClose: () => void;
  onConfirm: () => void;
}

export function ProveYes({ org, approval, question, yes, noMeans, changed, large, onClose, onConfirm }: Props) {
  const prove = useProveYes(org);
  // The passkey check runs on the computer that holds the account. The cloud cannot run it, so it says so.
  const providers = useProviders().data;
  const hosted = providers !== undefined && providers.code !== true;
  const [stage, setStage] = useState<Stage>('ready');
  const [armed, setArmed] = useState(false);

  useEffect(() => {
    const timer = setTimeout(() => setArmed(true), ARM_MS);
    return () => clearTimeout(timer);
  }, []);

  useEffect(() => {
    if (stage !== 'proved') return;
    const timer = setTimeout(onConfirm, PROVED_MS);
    return () => clearTimeout(timer);
  }, [stage, onConfirm]);

  const check = () => {
    setStage('checking');
    prove.mutate(approval.id, { onSuccess: () => setStage('proved'), onError: () => setStage('failed') });
  };

  const mac = approval.methods?.includes('mac');
  const size = large ? 'lg' : 'md';
  const wide = large ? 'h-12 flex-1' : undefined;
  const busy = stage === 'checking' || stage === 'proved';
  return (
    <Dialog
      open
      onOpenChange={(open) => !open && !busy && onClose()}
      title="Prove it's you"
      description={question}
      footer={
        hosted ? (
          <Button size={size} className={wide} onClick={onClose}>
            Back
          </Button>
        ) : stage === 'elsewhere' ? (
          <Button size={size} className={wide} onClick={() => setStage('ready')}>
            Back
          </Button>
        ) : (
          <>
            <Button size={size} className={wide} disabled={busy} onClick={onClose}>
              Go back
            </Button>
            <Button variant="primary" size={size} className={wide} disabled={!armed || busy} icon={<Fingerprint className="size-4" />} onClick={check}>
              {stage === 'failed' ? 'Try again' : 'Use your passkey'}
            </Button>
          </>
        )
      }
    >
      {hosted ? (
        <div className="flex items-start gap-3">
          <Laptop aria-hidden="true" className="mt-px size-4 shrink-0 text-faint" />
          <p className="text-14 text-pretty text-muted">This needs a passkey check. That check runs on your computer, so say yes there.</p>
        </div>
      ) : stage === 'elsewhere' ? (
        <div className="flex items-start gap-3">
          <Laptop aria-hidden="true" className="mt-px size-4 shrink-0 text-faint" />
          <p className="text-14 text-pretty text-muted">It is waiting in Needs you {mac ? 'on your phone and in the Mac app' : 'on your phone'}. Say yes there, and this page follows by itself.</p>
        </div>
      ) : (
        <div className="grid justify-items-center gap-4 pt-2 text-center">
          <div className="yes-prove-ring" data-checking={stage === 'checking' || undefined} data-proved={stage === 'proved' || undefined} data-failed={stage === 'failed' || undefined}>
            {stage === 'proved' ? <Check aria-hidden="true" className="size-8" strokeWidth={2} /> : stage === 'failed' ? <X aria-hidden="true" className="size-8" strokeWidth={2} /> : <Fingerprint aria-hidden="true" className="size-8" strokeWidth={1.25} />}
          </div>
          <p role="status" className="max-w-[40ch] text-14 text-pretty text-muted">
            {stage === 'failed'
              ? `That didn't work. ${noMeans}`
              : stage === 'proved'
                ? "That's you."
                : stage === 'checking'
                  ? 'Your browser is asking for your finger, your face or your PIN.'
                  : `${whyProof(approval)} This counts once, for exactly "${yes}".`}
          </p>
          {changed ? <p className="text-13 text-pretty text-faint">It changed after you first saw it. Say yes only if the version on screen is what you want.</p> : null}
          {busy ? null : (
            <button type="button" className="link text-13" onClick={() => setStage('elsewhere')}>
              {mac ? 'Use my phone or the Mac app instead' : 'Use my phone instead'}
            </button>
          )}
        </div>
      )}
    </Dialog>
  );
}
