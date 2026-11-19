import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { RouterProvider } from '@tanstack/react-router';
import { useEffect, useMemo, useRef, useSyncExternalStore } from 'react';
import { toast, Toaster } from 'sonner';
import { ApiError, isCancelled, onUnauthenticated } from '@/api/http';
import { StepUpDialog } from '@/components/shell/step-up-dialog';
import { TooltipProvider } from '@/components/ui/menu';
import { keys } from '@/api/keys';
import { describeError, isPlanLimit } from '@/lib/errors';
import { onOpenFromWorker } from '@/lib/pwa';
import { useTheme } from '@/lib/theme';
import { makeRouter } from './router';

/** Routes every member can read. A 404 or 403 here means access has changed. */
const SIGNALS_ACCESS = new Set(['status', 'members', 'workspaces']);

function makeClient(onPlanLimit: (message: string) => void) {
  const client: QueryClient = new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        // The event stream keeps the cache fresh, so focus does not need to refetch.
        refetchOnWindowFocus: false,
        retry: (count, error) => !(error instanceof ApiError && error.status < 500) && count < 2,
      },
    },
    queryCache: new QueryCache({
      onError: (error, query) => {
        const [scope, , part] = query.queryKey;
        const lost = error instanceof ApiError && (error.status === 404 || error.status === 403);
        if (lost && scope === 'org' && (part === undefined || SIGNALS_ACCESS.has(String(part)))) {
          void client.invalidateQueries({ queryKey: keys.session });
        }
      },
    }),
    mutationCache: new MutationCache({
      onError: (error, _variables, _context, mutation) => {
        // A refusal can mean the role in the session is out of date, so read it again.
        if (error instanceof ApiError && error.status === 403 && !isCancelled(error)) {
          void client.invalidateQueries({ queryKey: keys.session });
        }
        // A screen that shows its own error says so with meta.inline, so it is not said twice.
        if (mutation.options.onError || mutation.meta?.inline || isCancelled(error)) return;
        if (isPlanLimit(error)) onPlanLimit(describeError(error));
        else toast.error(describeError(error));
      },
    }),
  });
  return client;
}

const toastClasses = {
  toast:
    'flex w-full items-center gap-2 rounded-lg border border-line bg-raised px-4 py-3 text-body text-fg shadow-popover',
  title: 'font-normal',
  description: 'text-secondary text-muted',
  icon: 'text-faint',
};

const PHONE = '(max-width: 767px)';

/** On a phone the bottom of the screen is where the thumb decides, so toasts go to the top. */
function useIsPhone() {
  return useSyncExternalStore(
    (listener) => {
      const query = window.matchMedia(PHONE);
      query.addEventListener('change', listener);
      return () => query.removeEventListener('change', listener);
    },
    () => window.matchMedia(PHONE).matches,
  );
}

export function App() {
  const planLimit = useRef<(message: string) => void>(() => undefined);
  const client = useMemo(() => makeClient((message) => planLimit.current(message)), []);
  const router = useMemo(() => makeRouter(client), [client]);
  const theme = useTheme();
  const phone = useIsPhone();

  useEffect(() => {
    planLimit.current = (message) => {
      const org = router.state.location.pathname.split('/')[1] ?? '';
      toast(message, {
        action: { label: 'See plan', onClick: () => void router.navigate({ to: '/$org/settings/billing', params: { org } }) },
      });
    };
    onUnauthenticated(() => {
      const { href, pathname } = router.state.location;
      // Public pages answer 401 as a plain fact, and sign-in is already where this leads.
      if (/^\/(sign-in|sign-up|check-email|auth|invite|legal)/.test(pathname)) return;
      client.clear();
      void router.navigate({ to: '/sign-in', search: { next: href, reason: 'expired' } });
    });
    // A tap on a notification: the panel moves itself, so nothing open here reloads.
    const stopOpen = onOpenFromWorker((path) => router.history.push(path));
    return () => {
      onUnauthenticated(null);
      stopOpen();
    };
  }, [client, router]);

  return (
    <QueryClientProvider client={client}>
      <TooltipProvider delayDuration={400} skipDelayDuration={200}>
        <RouterProvider router={router} />
        <StepUpDialog />
        <Toaster theme={theme} position={phone ? 'top-center' : 'bottom-right'} toastOptions={{ unstyled: true, classNames: toastClasses, duration: 3000 }} />
      </TooltipProvider>
    </QueryClientProvider>
  );
}
