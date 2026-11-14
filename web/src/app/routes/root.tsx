/** The root route and what every other route file shares: the router's context and the session reads. */
import type { QueryClient } from '@tanstack/react-query';
import {
  createRootRouteWithContext,
  Outlet,
  redirect,
} from '@tanstack/react-router';
import { sessionQuery } from '@/api/hooks';
import { NotFound } from '@/components/not-found';
import { ErrorState } from '@/components/ui/states';

export interface RouterContext {
  queryClient: QueryClient;
}

export const rootRoute = createRootRouteWithContext<RouterContext>()({
  component: Outlet,
  notFoundComponent: () => (
    <div className="flex min-h-dvh items-center justify-center">
      <NotFound />
    </div>
  ),
  errorComponent: ({ error }) => <ErrorState error={error} onRetry={() => location.reload()} />,
});

export const text = (value: unknown) => (typeof value === 'string' && value ? value : undefined);

export async function currentSession(client: QueryClient) {
  try {
    return await client.ensureQueryData(sessionQuery);
  } catch {
    return null;
  }
}

export async function requireSession({ context, location }: { context: RouterContext; location: { href: string } }) {
  const session = await currentSession(context.queryClient);
  if (!session) throw redirect({ to: '/sign-in', search: { next: location.href } });
}
