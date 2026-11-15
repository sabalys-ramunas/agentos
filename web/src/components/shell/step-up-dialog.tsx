import { useQueryClient } from '@tanstack/react-query';
import { Fingerprint, Mail } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { auth } from '@/api/client';
import { useProviders, useSession } from '@/api/hooks';
import { ApiError, onStepUpRequired, setCsrfToken, STEP_UP_CANCELLED } from '@/api/http';
import { keys } from '@/api/keys';
import { describeError } from '@/lib/errors';
import { onStepUpAnnounced } from '@/lib/step-up-channel';
import { getPasskey, passkeysAvailable } from '@/lib/webauthn';
import { Button } from '../ui/button';
import { Dialog } from '../ui/dialog';
import { Spinner } from '../ui/spinner';

type Stage = 'choose' | 'passkey' | 'email' | 'timed-out';

const POLL_MS = 2_000;
/** The link's own lifetime. Past this it cannot arrive, so waiting would be a lie. */
const LINK_LIFETIME_MS = 15 * 60_000;

/**
 * Mounted once. Any request answered with `403 step_up_required` lands here, whichever
 * screen sent it. When re-auth succeeds the original request is retried by http.ts.
 */
export function StepUpDialog() {
  const client = useQueryClient();
  const session = useSession().data;
  const [open, setOpen] = useState(false);
  const [stage, setStage] = useState<Stage>('choose');
  const [error, setError] = useState<string | null>(null);
  const [reason, setReason] = useState<string | null>(null);
  const pending = useRef<{ resolve: () => void; reject: (reason: unknown) => void } | null>(null);

  useEffect(() => {
    onStepUpRequired(
      (why) =>
        new Promise<void>((resolve, reject) => {
          pending.current = { resolve, reject };
          setStage('choose');
          setError(null);
          setReason(why ?? null);
          setOpen(true);
        }),
    );
    // Gone with a step-up still open: cancel it, or every later one would wait on it with no dialog.
    return () => {
      onStepUpRequired(null);
      pending.current?.reject(new ApiError(403, STEP_UP_CANCELLED, 'Cancelled.'));
      pending.current = null;
    };
  }, []);

  useEffect(() => {
    document.documentElement.toggleAttribute('data-step-up', open);
  }, [open]);

  const finish = () => {
    setOpen(false);
    void client.invalidateQueries({ queryKey: keys.session });
    pending.current?.resolve();
    pending.current = null;
  };

  const cancel = () => {
    setOpen(false);
    pending.current?.reject(new ApiError(403, STEP_UP_CANCELLED, 'Cancelled.'));
    pending.current = null;
  };

  const confirmWithPasskey = async () => {
    setStage('passkey');
    setError(null);
    try {
      const options = await auth.stepUpPasskeyOptions();
      const credential = await getPasskey(options);
      await auth.stepUpPasskeyVerify(credential);
      finish();
    } catch {
      setStage('choose');
      setError(byEmail ? 'That did not work. Try again, or use an email link.' : 'That did not work. Try again.');
    }
  };

  const confirmWithEmail = async () => {
    setStage('email');
    setError(null);
    try {
      await auth.stepUpMagicLink();
    } catch (cause) {
      setStage('choose');
      setError(describeError(cause));
    }
  };

  // The link opens in another tab, which announces itself. The session check is the fallback
  // for a browser without BroadcastChannel, and it stops when the link could no longer work.
  useEffect(() => {
    if (!open || stage !== 'email') return;
    let done = false;
    const check = async () => {
      if (done) return;
      const next = await auth.session().catch(() => null);
      if (done || !next) return;
      if (next.step_up_until && new Date(next.step_up_until).getTime() > Date.now()) {
        done = true;
        setCsrfToken(next.csrf);
        finish();
      }
    };
    const timer = setInterval(() => void check(), POLL_MS);
    const deadline = setTimeout(() => setStage('timed-out'), LINK_LIFETIME_MS);
    const stop = onStepUpAnnounced(() => void check());
    const onVisible = () => {
      if (document.visibilityState === 'visible') void check();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      done = true;
      clearInterval(timer);
      clearTimeout(deadline);
      stop();
      document.removeEventListener('visibilitychange', onVisible);
    };
    // finish only touches refs and setters, so it is safe to leave out.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, stage]);

  const canUsePasskey = session?.user.has_passkey && passkeysAvailable();
  // A computer has no email, so its only way to confirm is a passkey. Without one there is nothing to
  // try here, and the dialog says how to get one rather than offering a link that cannot be sent.
  const byEmail = useProviders().data?.magic_link !== false;
  const org = session?.orgs[0]?.slug;

  return (
    <Dialog
      stepUp
      open={open}
      onOpenChange={(next) => {
        if (!next) cancel();
      }}
      title="Confirm it is you"
      description={reason ?? 'This action is sensitive, so agentos asks again. You will not be asked for the next 10 minutes.'}
    >
      {stage === 'email' && byEmail ? (
        <div className="grid gap-2">
          <div role="status" className="flex items-center gap-2 rounded-lg border border-line bg-subtle px-4 py-3 text-body text-muted">
            <Spinner className="text-faint" />
            <span>
              Check <span className="text-fg">{session?.user.email}</span> and open the link in this browser. This window
              continues by itself.
            </span>
          </div>
          {canUsePasskey ? (
            <Button icon={<Fingerprint className="size-4" />} onClick={() => void confirmWithPasskey()}>
              Use your passkey instead
            </Button>
          ) : null}
        </div>
      ) : (
        <div className="grid gap-2">
          {stage === 'timed-out' ? (
            <p role="status" className="text-body text-muted">
              That link has expired. Send a new one to carry on.
            </p>
          ) : null}
          {error ? (
            <p role="alert" className="text-secondary text-red">
              {error}
            </p>
          ) : null}
          {canUsePasskey ? (
            <Button variant="primary" size="lg" loading={stage === 'passkey'} icon={<Fingerprint className="size-4" />} onClick={() => void confirmWithPasskey()}>
              Use your passkey
            </Button>
          ) : null}
          {!byEmail && !canUsePasskey ? (
            session?.user.has_passkey ? (
              <p role="status" className="text-body text-muted">
                This browser cannot use passkeys, so this cannot be confirmed here. Open agentos in a browser that can.
              </p>
            ) : (
              <>
                <p role="status" className="text-body text-muted">
                  Confirming this takes a passkey, and this computer cannot send an email link. Add one, then try again.
                </p>
                {org ? (
                  <Button size="lg" asChild>
                    <a href={`/${org}/settings/passkeys`}>Add a passkey</a>
                  </Button>
                ) : null}
              </>
            )
          ) : null}
          {byEmail ? (
            <Button
              variant={canUsePasskey ? 'secondary' : 'primary'}
              size="lg"
              disabled={stage === 'passkey'}
              icon={<Mail className="size-4" />}
              onClick={() => void confirmWithEmail()}
            >
              Email me a link
            </Button>
          ) : null}
        </div>
      )}
    </Dialog>
  );
}
