import { Link, useNavigate, useSearch } from '@tanstack/react-router';
import { useState } from 'react';
import { auth } from '@/api/client';
import { useProviders, useSendMagicLink } from '@/api/hooks';
import { SiteMark } from '@/components/brand/site-mark';
import { Button } from '@/components/ui/button';
import { Field, Input } from '@/components/ui/field';
import { describeError } from '@/lib/errors';
import { AuthLayout, authButton, Or, PromisePicture } from './layout';

/** Under the button, never a checkbox: continuing is accepting, and the server records it. */
export function PolicyLine({ extra }: { extra?: string }) {
  return (
    <p className="mt-4 text-secondary text-faint">
      By continuing you agree to the{' '}
      <Link to="/legal/acceptable-use" target="_blank" className="link">
        acceptable use policy
      </Link>
      : no spam, no bulk accounts.{extra ? ` ${extra}` : ''}
    </p>
  );
}

export function SignUp() {
  const { next } = useSearch({ from: '/sign-up' });
  const navigate = useNavigate();
  const google = useProviders().data?.google;
  const send = useSendMagicLink();
  const [email, setEmail] = useState('');

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    const address = email.trim();
    send.mutate(
      { email: address, aup_accepted: true, next },
      { onSuccess: () => void navigate({ to: '/check-email', search: { email: address, next, aup: true } }) },
    );
  };

  return (
    <AuthLayout
      title="Give your AI a computer of its own"
      description="Every chat gets a fresh computer in the cloud with a terminal, a real browser and your repo. Your agent works there, pushes a branch, and the computer is deleted. Money and pushes to main ask you first."
      aside={<PromisePicture />}
      caption="Agents work on computers of their own. A post set to ask, a payment or a push to main asks you first."
      footer={
        <>
          Already have an account?{' '}
          <Link to="/sign-in" search={{ next }} className="link">
            Sign in
          </Link>
        </>
      }
    >
      {google ? (
        <>
          <Button size="lg" className={authButton} asChild>
            <a href={auth.googleStartUrl({ aupAccepted: true, next })}>
              <SiteMark site="google" size={16} label={false} />
              Continue with Google
            </a>
          </Button>
          <div className="my-3">
            <Or />
          </div>
        </>
      ) : null}
      <form onSubmit={submit} className="grid gap-4">
        <Field label="Email" error={send.isError ? describeError(send.error) : null}>
          <Input autoFocus type="email" inputMode="email" autoComplete="email" required placeholder="you@company.com" value={email} onChange={(event) => setEmail(event.target.value)} />
        </Field>
        <Button type="submit" variant="primary" size="lg" className={authButton} loading={send.isPending}>
          Continue with email
        </Button>
      </form>
      <PolicyLine extra="No password to remember: we email you a link." />
    </AuthLayout>
  );
}
