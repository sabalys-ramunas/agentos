import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Link, useRouter, useSearch } from '@tanstack/react-router';
import { useEffect, useRef, useState } from 'react';
import { auth } from '@/api/client';
import { useVerifyMagicLink } from '@/api/hooks';
import { ApiError, setCsrfToken } from '@/api/http';
import { keys } from '@/api/keys';
import { Button } from '@/components/ui/button';
import { Spinner } from '@/components/ui/spinner';
import { safeNext } from '@/lib/format';
import { announceStepUp } from '@/lib/step-up-channel';
import { AuthLayout } from './layout';

/** Every bad link answers the same on purpose, so the copy cannot say which kind it was. */
function DeadLink() {
  return (
    <AuthLayout title="This link is no longer valid" description="Sign-in links work once and expire after 15 minutes. Ask for a new one.">
      <Button variant="primary" size="lg" className="w-full" asChild>
        <Link to="/sign-in" search={{}}>
          Get a new link
        </Link>
      </Button>
    </AuthLayout>
  );
}

function Working({ title }: { title: string }) {
  return (
    <AuthLayout title={title}>
      <Spinner className="size-6 text-faint" />
    </AuthLayout>
  );
}

function SignInLink({ token, next }: { token: string; next?: string }) {
  const router = useRouter();
  const verify = useVerifyMagicLink();
  const sent = useRef(false);

  useEffect(() => {
    // A token works once, so a double effect in development must not burn it.
    if (sent.current) return;
    sent.current = true;
    // The server already checks `next`; the panel checks it again by the same rules.
    verify.mutate(token, { onSuccess: (session) => router.history.replace(safeNext(session.next) ?? next ?? '/') });
  }, [token, next, router, verify]);

  return verify.isError ? <DeadLink /> : <Working title="Signing you in" />;
}

function StepUpLink({ token }: { token: string }) {
  const client = useQueryClient();
  const sent = useRef(false);
  const verify = useMutation({
    mutationFn: auth.stepUpVerify,
    meta: { inline: true },
    onSuccess: (session) => {
      setCsrfToken(session.csrf);
      client.setQueryData(keys.session, session);
      announceStepUp();
    },
  });

  useEffect(() => {
    if (sent.current) return;
    sent.current = true;
    verify.mutate(token);
  }, [token, verify]);

  if (verify.isPending || verify.isIdle) return <Working title="Confirming it is you" />;

  if (verify.isSuccess) {
    return (
      <AuthLayout
        title="Confirmed"
        description="Go back to the tab where you started, and it carries on by itself. You can close this one."
      >
        <Button size="lg" className="w-full" asChild>
          <Link to="/">Open agentos</Link>
        </Button>
      </AuthLayout>
    );
  }

  const elsewhere = verify.error instanceof ApiError && verify.error.status === 401;
  return (
    <AuthLayout
      title={elsewhere ? 'Open this in the browser you started in' : 'This link is no longer valid'}
      description={
        elsewhere
          ? 'This link only works in the browser that asked for it, while you are signed in there.'
          : 'Confirmation links work once, expire after 15 minutes, and only work in the browser that asked for them. Go back and ask for a new one.'
      }
    >
      <Button size="lg" className="w-full" asChild>
        <Link to="/">Open agentos</Link>
      </Button>
    </AuthLayout>
  );
}

export function Verify() {
  const search = useSearch({ from: '/auth/verify' });
  const router = useRouter();
  // Read once, then taken out of the address bar, so the token is not left in history, a
  // screenshot or a shared screen (security review 15). It works once anyway.
  const [{ token, next, step_up }] = useState(search);
  useEffect(() => {
    if (search.token) router.history.replace(step_up ? '/auth/verify?step_up=1' : '/auth/verify');
  }, [search.token, step_up, router]);
  if (!token) return <DeadLink />;
  return step_up ? <StepUpLink token={token} /> : <SignInLink token={token} next={next} />;
}
