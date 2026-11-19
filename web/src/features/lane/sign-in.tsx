/**
 * The sign-in of one account, on the login queue. The password is typed on this computer, in a form it
 * serves, and never in the app. A login already saved in the vault signs in from it with no form and asks
 * nobody: the vault's entry is the owner's permission (agentos Cloud and the computer both hold that rule).
 */
import { Link } from '@tanstack/react-router';
import { ArrowUpRight } from 'lucide-react';
import { useState } from 'react';
import { useAccess, useLaneLogin, useLaneLoginAction, useStartLaneLogin, useVaultSignIn, useVaultStatus } from '@/api/hooks';
import type { LaneLogin, LoginState } from '@/api/types';
import { Button } from '@/components/ui/button';
import { StatusDot } from '@/components/ui/status';
import { InlineError } from '@/components/ui/states';

const WORDS: Record<LoginState, (saved: boolean) => string> = {
  none: (saved) => (saved ? 'Saved in the vault. agentos signs in from it and asks nobody.' : 'Not signed in yet. You type the password on this computer, in a form it serves.'),
  working: () => 'agentos is opening the sign-in page.',
  ask: () => 'Type the password on this computer. It goes into the lane and nowhere else.',
  waiting: () => 'Approve the sign-in on your phone, then press the button.',
  stopped: () => 'The site asks a robot check, or a step agentos cannot read. Take over the lane and do that step yourself.',
  paused: () => 'You have the lane. Hand it back to go on from the form.',
  signed_in: (saved) => (saved ? 'Signed in. The login is saved in the vault.' : 'Signed in. You can save this login to the vault from the form.'),
  cancelled: () => 'The sign-in was stopped. Nothing else was typed.',
  done: () => 'Signed in.',
};

const OVER: LoginState[] = ['none', 'cancelled', 'done'];

export function SignIn({ org, ws, name, saved, canAct }: { org: string; ws: string; name: string; saved: boolean; canAct: boolean }) {
  const login = useLaneLogin(org, ws, name);
  const start = useStartLaneLogin(org, ws, name);
  const act = useLaneLoginAction(org, ws, name);
  const vaultIn = useVaultSignIn(org, ws, name);
  const access = useAccess(org);
  // The node lets an owner or an admin sign in from a saved login, and only the owner reads the vault's state.
  const mayUseSaved = access.can('vault.manage');
  const vault = useVaultStatus(org, access.can('vault.open'));
  const [link, setLink] = useState<string | null>(null);
  const current: LaneLogin = login.data ?? { lane: `${ws}/${name}`, state: 'none' };
  const state = current.state;
  const locked = vault.data?.state === 'sealed';
  const words = saved && !mayUseSaved && OVER.includes(state) ? 'Saved in the vault. An owner or admin signs in from it, with no form.' : WORDS[state](saved);
  const busy = start.isPending || act.isPending || vaultIn.isPending;
  const failed = start.isError || act.isError || vaultIn.isError;

  /** Opens the computer's form on the click, so a blocker lets the tab through. */
  const openForm = async () => {
    const tab = window.open('', '_blank');
    try {
      const made = await start.mutateAsync();
      setLink(made.form_url);
      if (tab && made.form_url) tab.location.href = made.form_url;
      else tab?.close();
    } catch {
      tab?.close();
    }
  };

  const primary = (() => {
    if (!canAct) return null;
    if (state === 'working') return null;
    if (state === 'ask') return <Button variant="primary" loading={busy} onClick={() => void openForm()}>Open the form again</Button>;
    if (state === 'waiting') return <Button variant="primary" loading={busy} onClick={() => act.mutate('refresh')}>I approved it, read the page again</Button>;
    if (state === 'stopped') return <Button variant="primary" loading={busy} onClick={() => act.mutate('take')}>Take over the lane</Button>;
    if (state === 'paused') return <Button variant="primary" loading={busy} onClick={() => act.mutate('resume')}>Hand it back to the form</Button>;
    if (OVER.includes(state) && saved && !locked && mayUseSaved) return <Button variant="primary" loading={busy} onClick={() => vaultIn.mutate()}>Sign in from the vault</Button>;
    if (OVER.includes(state) && !saved) return <Button variant="primary" loading={busy} onClick={() => void openForm()}>Sign in</Button>;
    return null;
  })();

  const secondary = canAct && (state === 'ask' || state === 'working' || state === 'waiting') ? (
    <Button variant="ghost" loading={act.isPending} onClick={() => act.mutate('cancel')}>
      Stop the sign-in
    </Button>
  ) : null;

  return (
    <section aria-label="Sign in" className="grid gap-3 rounded-xl border border-line p-4 sm:p-6">
      <div className="flex items-start gap-3">
        <StatusDot tone={state === 'signed_in' || state === 'done' ? 'ok' : 'idle'} className="mt-2" />
        <div className="min-w-0 flex-1">
          <p className="text-label">{state === 'signed_in' || state === 'done' ? 'Signed in' : 'Sign in'}</p>
          <p className="text-secondary text-muted">{words}</p>
          {current.error ? <p className="mt-2 text-secondary text-bad">{current.error}</p> : null}
          {failed ? <InlineError className="mt-2">That did not start. Nothing was typed.</InlineError> : null}
          {saved && locked && OVER.includes(state) ? (
            <p className="mt-2 text-secondary text-muted">
              The vault is locked. <Link to="/$org/settings/vault" params={{ org }} className="underline underline-offset-2">Unlock it in Settings</Link> to sign in from it.
            </p>
          ) : null}
        </div>
      </div>
      {canAct ? (
        <div className="flex flex-wrap items-center justify-end gap-2">
          {secondary}
          {link && state !== 'none' && state !== 'cancelled' && state !== 'done' ? (
            <Button variant="ghost" asChild>
              <a href={link} target="_blank" rel="noreferrer">
                Open the page on this computer
                <ArrowUpRight aria-hidden="true" className="size-4" />
              </a>
            </Button>
          ) : null}
          {primary}
        </div>
      ) : null}
    </section>
  );
}
