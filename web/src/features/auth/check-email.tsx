import { Link, useSearch } from '@tanstack/react-router';
import { useEffect, useState } from 'react';
import { useSendMagicLink } from '@/api/hooks';
import { Mail } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { AppTile } from '@/components/ui/tile';
import { describeError } from '@/lib/errors';
import { COOLDOWN_SECONDS } from './cooldown';
import { AuthLayout } from './layout';
import { InlineError } from '@/components/ui/states';

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
      tile={<AppTile tone="blue" icon={Mail} size="lg" />}
      title="Check your email"
      description={
        email ? (
          <>
            If <span className="font-medium text-fg [overflow-wrap:anywhere]">{email}</span> can sign in, a link is on its way. {sent}
          </>
        ) : (
          `If that address can sign in, a link is on its way. ${sent}`
        )
      }
      footer={
        <Link to={aup ? '/sign-up' : '/sign-in'} search={{ next }} className="inline-flex h-9 items-center rounded-full px-3 font-medium text-muted hover:bg-hover hover:text-fg">
          Use a different email
        </Link>
      }
    >
      <Button
        size="lg"
        className="h-11 w-full off:text-muted"
        loading={send.isPending}
        disabled={wait > 0 || !email}
        // Resending keeps the policy acceptance, or a new person would get the "no account" mail.
        onClick={() => email && send.mutate({ email, next, aup_accepted: aup }, { onSuccess: () => setWait(COOLDOWN_SECONDS) })}
      >
        {wait > 0 ? <span className="tabular">Resend in {wait} s</span> : 'Resend the link'}
      </Button>
      {send.isError ? <InlineError className="mt-3">{describeError(send.error)}</InlineError> : null}
      {send.isSuccess && wait > 0 ? (
        <p role="status" className="mt-3 text-center text-secondary text-ok">
          A new link is on its way.
        </p>
      ) : null}
    </AuthLayout>
  );
}
