import { Link, useSearch } from '@tanstack/react-router';
import { useEffect, useState } from 'react';
import { useSendMagicLink } from '@/api/hooks';
import { Button } from '@/components/ui/button';
import { describeError } from '@/lib/errors';
import { COOLDOWN_SECONDS } from './cooldown';
import { AuthLayout } from './layout';

export function CheckEmail() {
  const { email, next, aup } = useSearch({ from: '/check-email' });
  const send = useSendMagicLink();
  const [wait, setWait] = useState(COOLDOWN_SECONDS);

  useEffect(() => {
    if (wait <= 0) return;
    const timer = setTimeout(() => setWait((value) => value - 1), 1_000);
    return () => clearTimeout(timer);
  }, [wait]);

  // The API answers the same for every address, so this cannot claim a link was sent.
  const sent = 'It works once and expires in 15 minutes.';

  return (
    <AuthLayout
      title="Check your email"
      description={
        email ? (
          <>
            If <span className="text-fg">{email}</span> can sign in, a link is on its way. {sent}
          </>
        ) : (
          `If that address can sign in, a link is on its way. ${sent}`
        )
      }
      footer={
        <Link to={aup ? '/sign-up' : '/sign-in'} search={{ next }} className="link">
          Use a different email
        </Link>
      }
    >
      <Button
        size="lg"
        className="w-full disabled:text-muted"
        loading={send.isPending}
        disabled={wait > 0 || !email}
        // Resending keeps the policy acceptance, or a new person would get the "no account" mail.
        onClick={() => email && send.mutate({ email, next, aup_accepted: aup }, { onSuccess: () => setWait(COOLDOWN_SECONDS) })}
      >
        {wait > 0 ? <span className="tabular">Resend in {wait}s</span> : 'Resend the link'}
      </Button>
      {send.isError ? (
        <p role="alert" className="mt-2 text-13 text-red">
          {describeError(send.error)}
        </p>
      ) : null}
    </AuthLayout>
  );
}
