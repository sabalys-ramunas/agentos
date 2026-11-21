/**
 * The invite link (startup.md section B). For a new person the link is the sign-in: it went to
 * their address, so one button creates the account, accepts and signs them in, then opens "here
 * is what you will be asked". Someone with an account, or an owner or admin invite, signs in as
 * usual and accepts.
 */
import { Link, useNavigate, useParams } from '@tanstack/react-router';
import { Fingerprint } from 'lucide-react';
import { useState } from 'react';
import { useAcceptInvite, useInvitePreview, useJoinInvite, useJoinInviteWithPasskey, useSendMagicLink, useSession } from '@/api/hooks';
import { ApiError, isCancelled } from '@/api/http';
import type { InvitePreview, Role } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Field, Input } from '@/components/ui/field';
import { InlineError, Skeleton } from '@/components/ui/states';
import { describeError } from '@/lib/errors';
import { passkeyError } from '@/lib/webauthn';
import { AuthLayout, GlassAsk, PromisePicture } from './layout';
import { PolicyLine } from './sign-up';

const DEAD: Record<string, { title: string; description: string }> = {
  expired: { title: 'This invite has expired', description: 'Invites last 7 days. Ask the person who invited you to send a new one.' },
  already_used: { title: 'This invite was already used', description: 'Each invite works once. If you accepted it, sign in to continue.' },
};

/** What each role will do, after "<first> uses openajar so AI agents can build, check and run work for <business>." */
const ROLE_WORDS: Record<Role, (first: string) => string> = {
  approver: (first) => `Most of it runs on its own. Payments, and anything set to ask first, wait for a person's yes. ${first} would like that to be you.`,
  viewer: () => 'You will see what it does, with proof, and change nothing.',
  auditor: () => 'You will read the evidence of what it does, and change nothing.',
  member: () => 'You will watch its accounts, start its routines and say yes to what it asks.',
  admin: () => 'You will set things up, invite people and change settings.',
  owner: (first) => `You will be one of its owners, with every right ${first} has.`,
};

export function inviteWords(preview: InvitePreview) {
  const first = preview.inviter_first_name ?? preview.inviter.split(' ')[0] ?? preview.inviter;
  const business = preview.business ?? preview.org_name;
  const title =
    preview.role === 'approver'
      ? `${first} asked you to help with ${business}`
      : preview.role === 'viewer'
        ? `${first} invited you to see ${business}`
        : `${first} invited you to ${business}`;
  const intro = `${first} uses openajar so AI agents can build, check and run work for ${business}. ${ROLE_WORDS[preview.role](first)}`;
  return { first, business, title, intro };
}

/** The first screen after joining: approvers and members learn what they will be asked. */
function landing(org: string, role: Role, from: string) {
  return role === 'approver' || role === 'member' ? { to: '/$org/welcome' as const, params: { org }, search: { from } } : { to: '/$org' as const, params: { org } };
}

/**
 * An existing account, or an owner or admin invite: a sign-in link to the invited address, which
 * comes back here. The policy rides along, so an address with no account yet can join too.
 */
function EmailToJoin({ hint, next }: { hint?: string; next: string }) {
  const send = useSendMagicLink();
  const navigate = useNavigate();
  const [email, setEmail] = useState('');
  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    const address = email.trim();
    send.mutate({ email: address, aup_accepted: true, next }, { onSuccess: () => void navigate({ to: '/check-email', search: { email: address, next, aup: true } }) });
  };
  return (
    <form onSubmit={submit} className="grid gap-4">
      <Field label="Your email" hint={hint ? `The address this invite went to: ${hint}` : 'The address this invite went to.'} error={send.isError ? describeError(send.error) : null}>
        <Input type="email" autoComplete="email" required className="h-10" value={email} onChange={(event) => setEmail(event.target.value)} />
      </Field>
      <Button type="submit" variant="primary" size="lg" loading={send.isPending}>
        Email me a link to join
      </Button>
      <PolicyLine />
    </form>
  );
}

export function InviteAccept() {
  const { token } = useParams({ from: '/invite/$token' });
  const preview = useInvitePreview(token);
  const session = useSession();
  const accept = useAcceptInvite();
  const join = useJoinInvite();
  const joinByPasskey = useJoinInviteWithPasskey();
  const navigate = useNavigate();

  if (preview.isPending) {
    return (
      <AuthLayout title="Checking your invite">
        <Skeleton className="h-10" />
      </AuthLayout>
    );
  }

  if (preview.isError) {
    const code = preview.error instanceof ApiError ? preview.error.code : '';
    // An invite whose sender lost the right to invite at that role also answers `expired`, with a
    // sentence of its own that says why (security review 25). That one is shown as written.
    const lapsed = code === 'expired' && preview.error instanceof ApiError && /can no longer invite/.test(preview.error.message);
    const copy = lapsed
      ? { title: 'This invite no longer works', description: (preview.error as ApiError).message }
      : (DEAD[code] ?? { title: 'This invite does not work', description: 'Ask the person who invited you to send a new one.' });
    return (
      <AuthLayout title={copy.title} description={copy.description}>
        <Button size="lg" className="h-11 w-full" asChild>
          <Link to="/sign-in" search={{}}>
            Go to sign in
          </Link>
        </Button>
      </AuthLayout>
    );
  }

  const data = preview.data;
  const words = inviteWords(data);
  const signedIn = Boolean(session.data);
  const here = `/invite/${token}`;
  // A computer has no email, so its invite is accepted with a passkey made on this device.
  const byPasskey = data.join_by === 'passkey';
  // The join refused because this address already has an account: sign in instead.
  const needsSignIn = !signedIn && !byPasskey && (data.one_click === false || (join.error instanceof ApiError && join.error.code === 'sign_in_required'));
  const shows = data.role === 'approver' || data.role === 'member';
  const picture = shows ? (
    <div aria-hidden="true" className="grid w-full max-w-[380px] gap-3">
      <p className="text-body text-(--sidebar-text) [text-shadow:0_1px_8px_rgb(4_6_24/0.4)]">This is what it looks like when you are asked</p>
      <GlassAsk business={words.business} />
    </div>
  ) : (
    <PromisePicture />
  );

  const passkeyFailed = joinByPasskey.isError && !isCancelled(joinByPasskey.error);
  const error = accept.isError
    ? describeError(accept.error)
    : join.isError && !needsSignIn
      ? describeError(join.error)
      : passkeyFailed
        ? (passkeyError(joinByPasskey.error) ?? describeError(joinByPasskey.error))
        : null;

  return (
    <AuthLayout title={words.title} description={words.intro} aside={picture}>
      {error ? (
        <InlineError className="mb-4">
          {error}
        </InlineError>
      ) : null}
      {byPasskey ? (
        <>
          <Button
            variant="primary"
            size="lg"
            className="h-11 w-full"
            icon={<Fingerprint className="size-4" />}
            loading={joinByPasskey.isPending}
            onClick={() =>
              joinByPasskey.mutate(
                { token },
                { onSuccess: (joined) => void navigate(landing(joined.orgs[0]?.slug ?? '', data.role, words.first)) },
              )
            }
          >
            Join {words.business}
          </Button>
          <PolicyLine extra="No password. Your device makes a passkey, and this button signs you in." />
        </>
      ) : signedIn ? (
        <Button
          variant="primary"
          size="lg"
          className="h-11 w-full"
          loading={accept.isPending}
          onClick={() => accept.mutate(token, { onSuccess: ({ org }) => void navigate(landing(org, data.role, words.first)) })}
        >
          Join {words.business}
        </Button>
      ) : needsSignIn ? (
        <EmailToJoin hint={data.email_hint} next={here} />
      ) : (
        <>
          <Button
            variant="primary"
            size="lg"
            className="h-11 w-full"
            loading={join.isPending}
            onClick={() => join.mutate({ token }, { onSuccess: ({ org }) => void navigate(landing(org, data.role, words.first)) })}
          >
            Join {words.business}
          </Button>
          <PolicyLine extra="No password. This button signs you in." />
        </>
      )}
      {!signedIn && !needsSignIn ? (
        <p className="mt-6 text-secondary text-muted">
          Already have an account?{' '}
          <Link to="/sign-in" search={{ next: here }} className="link">
            Sign in
          </Link>
        </p>
      ) : null}
    </AuthLayout>
  );
}
