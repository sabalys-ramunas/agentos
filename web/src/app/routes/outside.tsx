/** Routes outside an org's shell: the short links that find an org, sign-in, onboarding and the phone's one-at-a-time view. */
import {
  createRoute,
  lazyRouteComponent,
  notFound,
  redirect,
} from '@tanstack/react-router';
import { sessionQuery } from '@/api/hooks';
import { apiMode } from '@/config';
import { safeNext } from '@/lib/format';
import type { RouterContext } from './root';
import { currentSession, requireSession, rootRoute, text } from './root';

export const indexRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/',
  beforeLoad: async ({ context }) => {
    // Fresh, not cached: this is where someone lands after deleting or losing an org. A read
    // already in flight may have started before that happened, and fetchQuery would join it.
    await context.queryClient.cancelQueries({ queryKey: sessionQuery.queryKey });
    const session = await context.queryClient.fetchQuery({ ...sessionQuery, staleTime: 0 }).catch(() => null);
    if (!session) throw redirect({ to: '/sign-in' });
    const first = session.orgs[0];
    if (!first) throw redirect({ to: '/onboarding' });
    throw redirect({ to: '/$org', params: { org: first.slug } });
  },
});

/**
 * Links from before the org was in every link: a notification's `/needs-you?approval=...`, a `/review/...` or
 * `/activity` bookmark. They open the same page in the person's first org (a node has one, `local`), keeping
 * the query, instead of reading "needs-you" as an org's name.
 */
async function firstOrg(context: RouterContext, href: string): Promise<string> {
  const session = await currentSession(context.queryClient);
  if (!session) throw redirect({ to: '/sign-in', search: { next: href } });
  const first = session.orgs[0];
  if (!first) throw redirect({ to: '/onboarding' });
  return first.slug;
}

export const rootNeedsYouRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/needs-you',
  beforeLoad: async ({ context, location }) => {
    const org = await firstOrg(context, location.href);
    throw redirect({ to: '/$org/needs-you', params: { org }, search: true, replace: true });
  },
});

export const rootApprovalsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/approvals',
  beforeLoad: async ({ context, location }) => {
    const org = await firstOrg(context, location.href);
    throw redirect({ to: '/$org/needs-you', params: { org }, search: true, replace: true });
  },
});

export const rootReviewRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/review/$id',
  beforeLoad: async ({ context, location, params }) => {
    const org = await firstOrg(context, location.href);
    throw redirect({ to: '/$org/review/$id', params: { org, id: params.id }, search: true, replace: true });
  },
});

export const rootLanesRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/lanes/$ws/$name',
  beforeLoad: async ({ context, location, params }) => {
    const org = await firstOrg(context, location.href);
    throw redirect({ to: '/$org/lanes/$ws/$name', params: { org, ws: params.ws, name: params.name }, replace: true });
  },
});

export const rootActivityRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/activity',
  beforeLoad: async ({ context, location }) => {
    const org = await firstOrg(context, location.href);
    throw redirect({ to: '/$org/activity', params: { org }, search: true, replace: true });
  },
});

// Auth

export const signInRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/sign-in',
  validateSearch: (search: Record<string, unknown>): { next?: string; error?: string; reason?: string } => ({
    next: safeNext(search.next),
    error: text(search.error),
    reason: text(search.reason),
  }),
  component: lazyRouteComponent(() => import('@/features/auth/sign-in'), 'SignIn'),
});

// `openajar panel login` prints this page's address with the code after the #, so it never reaches
// a server log.
export const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/login',
  component: lazyRouteComponent(() => import('@/features/auth/code-login'), 'CodeLogin'),
});

export const signUpRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/sign-up',
  validateSearch: (search: Record<string, unknown>): { next?: string } => ({ next: safeNext(search.next) }),
  component: lazyRouteComponent(() => import('@/features/auth/sign-up'), 'SignUp'),
});

export const checkEmailRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/check-email',
  validateSearch: (search: Record<string, unknown>): { email?: string; next?: string; aup?: boolean } => ({
    email: text(search.email),
    next: safeNext(search.next),
    aup: search.aup === true || search.aup === 'true' ? true : undefined,
  }),
  component: lazyRouteComponent(() => import('@/features/auth/check-email'), 'CheckEmail'),
});

export const verifyRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/auth/verify',
  validateSearch: (search: Record<string, unknown>): { token?: string; next?: string; step_up?: boolean } => ({
    token: text(search.token),
    next: safeNext(search.next),
    step_up: search.step_up === 1 || search.step_up === '1' || search.step_up === true ? true : undefined,
  }),
  component: lazyRouteComponent(() => import('@/features/auth/verify'), 'Verify'),
});

export const legalRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/legal/acceptable-use',
  component: lazyRouteComponent(() => import('@/features/auth/acceptable-use'), 'AcceptableUse'),
});

/** A read-only link from Proof. Opened with no sign-in: the token in the fragment is the only credential. */
export const sharedRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/shared',
  component: lazyRouteComponent(() => import('@/features/shared/shared-page'), 'SharedPage'),
});

export const inviteRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/invite/$token',
  component: lazyRouteComponent(() => import('@/features/auth/invite'), 'InviteAccept'),
});

// Onboarding

export const onboardingRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/onboarding',
  beforeLoad: requireSession,
  component: lazyRouteComponent(() => import('@/features/onboarding/onboarding'), 'CreateOrgStep'),
});

export const onboardingStepRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/onboarding/$org/$step',
  beforeLoad: requireSession,
  component: lazyRouteComponent(() => import('@/features/onboarding/onboarding'), 'OnboardingStep'),
});

// The phone view has no shell: one decision at a time, full height.

export const approveRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/$org/approve',
  beforeLoad: requireSession,
  component: lazyRouteComponent(() => import('@/features/needs-you/approve'), 'Approve'),
});

/** Concept stage: how a yes looks in a chat. A stand-alone mock, so a live build has no such page. */
export const yesInChatRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/$org/concept/yes-in-chat',
  beforeLoad: (context) => {
    if (apiMode !== 'mock') throw notFound();
    return requireSession(context);
  },
  component: lazyRouteComponent(() => import('@/features/confirmations/yes-in-chat'), 'YesInChat'),
});

/** The first screen after an invite is accepted: what you will be asked. No shell, like the phone view. */
export const welcomeRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/$org/welcome',
  beforeLoad: requireSession,
  validateSearch: (search: Record<string, unknown>): { from?: string } => ({ from: text(search.from)?.slice(0, 40) }),
  component: lazyRouteComponent(() => import('@/features/auth/welcome'), 'Welcome'),
});
