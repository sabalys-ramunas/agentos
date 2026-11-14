import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useRouter } from '@tanstack/react-router';
import { useEffect, useRef, useState } from 'react';
import { auth } from '@/api/client';
import { setCsrfToken } from '@/api/http';
import { keys } from '@/api/keys';
import { Button } from '@/components/ui/button';
import { Field, Input } from '@/components/ui/field';
import { describeError } from '@/lib/errors';
import { useTechnical } from '@/lib/technical';
import { AuthLayout, Notice, PromisePicture } from './layout';

function useCodeSignIn(next?: string) {
  const router = useRouter();
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: async (code: string) => {
      const signedIn = await auth.signInWithCode(code);
      setCsrfToken(signedIn.csrf);
      return auth.session();
    },
    onSuccess: (session) => {
      setCsrfToken(session.csrf);
      client.setQueryData(keys.session, session);
      router.history.push(next ?? '/');
    },
  });
}

/** The one-time code from `agentos panel login`, for a computer's sign-in page. */
export function CodeForm({ next }: { next?: string }) {
  const [code, setCode] = useState('');
  const [technical] = useTechnical();
  const signIn = useCodeSignIn(next);
  return (
    <form
      className="grid gap-4"
      onSubmit={(event) => {
        event.preventDefault();
        signIn.mutate(code.trim());
      }}
    >
      <Field
        label="Sign-in code"
        hint={
          <>
            The code is shown in the Terminal of this computer. Open it there and run the sign-in command.
            {technical ? <span className="mono mt-1 block text-faint">agentos panel login</span> : null}
          </>
        }
        error={signIn.isError ? describeError(signIn.error) : null}
      >
        <Input autoFocus required autoComplete="off" spellCheck={false} className="mono h-10" value={code} onChange={(event) => setCode(event.target.value)} />
      </Field>
      <Button type="submit" variant="primary" size="lg" loading={signIn.isPending}>
        Sign in
      </Button>
    </form>
  );
}

/** The address `agentos panel login` prints. The code rides after the #, and signs in by itself. */
export function CodeLogin() {
  const signIn = useCodeSignIn();
  const started = useRef(false);
  const code = new URLSearchParams(location.hash.slice(1)).get('code');

  useEffect(() => {
    if (started.current || !code) return;
    started.current = true;
    history.replaceState(null, '', location.pathname);
    signIn.mutate(code);
  }, [code, signIn]);

  return (
    <AuthLayout title="Sign in to this computer" description="Your accounts and passwords stay here." aside={<PromisePicture />}>
      {signIn.isError ? <Notice alert>{describeError(signIn.error)}</Notice> : null}
      {signIn.isError || !code ? <CodeForm /> : <p className="text-14 text-muted">Signing you in.</p>}
    </AuthLayout>
  );
}
