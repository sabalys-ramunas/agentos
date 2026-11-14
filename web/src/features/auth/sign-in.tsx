import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Link, useNavigate, useRouter, useSearch } from '@tanstack/react-router';
import { Fingerprint } from 'lucide-react';
import { useState } from 'react';
import { auth } from '@/api/client';
import { useProviders, useSendMagicLink } from '@/api/hooks';
import { setCsrfToken } from '@/api/http';
import { keys } from '@/api/keys';
import { SiteMark } from '@/components/brand/site-mark';
import { Button } from '@/components/ui/button';
import { Field, Input } from '@/components/ui/field';
import { describeError } from '@/lib/errors';
import { getPasskey, passkeysAvailable } from '@/lib/webauthn';
import { CodeForm } from './code-login';
import { AuthLayout, Notice, PromisePicture } from './layout';

/** The reasons the API puts on `/sign-in?error=`, in words that say what to do next. */
const GOOGLE_ERRORS: Record<string, string> = {
  google_failed: 'Google sign-in did not finish. Try again, or use your email.',
  email_unverified: 'Google has not verified that email address, so agentos cannot use it to sign in.',
  // Google never joins an existing account by email alone (security review 33).
  google_link_required: 'This email already has an account. Sign in with an email link, then link Google in Settings.',
  google_unavailable: 'Google sign-in is not set up on this computer. Sign in with a passkey, or with a code from this computer.',
};

export function SignIn() {
  const { next, error, reason } = useSearch({ from: '/sign-in' });
  const navigate = useNavigate();
  const router = useRouter();
  const client = useQueryClient();
  const providers = useProviders().data;
  const send = useSendMagicLink();
  const [email, setEmail] = useState('');

  const passkey = useMutation({
    mutationFn: async () => {
      const options = await auth.passkeyLoginOptions();
      return auth.passkeyLoginVerify(await getPasskey(options));
    },
    onSuccess: (session) => {
      setCsrfToken(session.csrf);
      client.setQueryData(keys.session, session);
      router.history.push(next ?? '/');
    },
    // A cancelled system prompt is not an error worth a toast.
    onError: () => undefined,
  });

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    send.mutate(
      { email: email.trim(), next },
      { onSuccess: () => void navigate({ to: '/check-email', search: { email: email.trim(), next } }) },
    );
  };

  const onComputer = providers?.code === true;

  return (
    <AuthLayout
      title={onComputer ? 'Sign in to this computer' : 'Sign in to agentos'}
      description={onComputer ? 'Your accounts and passwords stay here.' : 'We email you a link. No password to remember.'}
      aside={<PromisePicture />}
      footer={
        onComputer ? undefined : (
          <>
            New here?{' '}
            <Link to="/sign-up" search={{ next }} className="link">
              Create an account
            </Link>
          </>
        )
      }
    >
      {reason === 'expired' ? <Notice>Your session ended. Sign in and you will land back where you were.</Notice> : null}
      {error === 'aup_required' ? (
        <Notice>
          There is no account for that Google address yet.{' '}
          <Link to="/sign-up" search={{ next }} className="link">
            Create one
          </Link>{' '}
          first. It takes one click.
        </Notice>
      ) : error ? (
        <Notice alert>{GOOGLE_ERRORS[error] ?? GOOGLE_ERRORS.google_failed}</Notice>
      ) : null}
      {onComputer ? <CodeForm next={next} /> : null}
      {providers?.magic_link !== false ? (
      <form onSubmit={submit} className="grid gap-4">
        <Field label="Email" error={send.isError ? describeError(send.error) : null}>
          <Input autoFocus type="email" autoComplete="email webauthn" required className="h-10" value={email} onChange={(event) => setEmail(event.target.value)} />
        </Field>
        <Button type="submit" variant="primary" size="lg" loading={send.isPending}>
          Continue with email
        </Button>
      </form>
      ) : null}
      {providers?.google || (providers?.passkey && passkeysAvailable()) ? (
        <div className="mt-4 grid gap-2 border-t border-line pt-4">
          {providers.passkey && passkeysAvailable() ? (
            <Button size="lg" loading={passkey.isPending} icon={<Fingerprint className="size-4" />} onClick={() => passkey.mutate()}>
              Use a passkey
            </Button>
          ) : null}
          {providers.google ? (
            <Button size="lg" asChild>
              <a href={auth.googleStartUrl({ next })}>
                <SiteMark site="google" size={16} label={false} />
                Continue with Google
              </a>
            </Button>
          ) : null}
        </div>
      ) : null}
      {passkey.isError ? (
        <p role="alert" className="mt-2 text-13 text-red">
          {onComputer ? 'That passkey did not work. Try again, or use a sign-in code.' : 'That passkey did not work. Try again, or use your email.'}
        </p>
      ) : null}
    </AuthLayout>
  );
}
