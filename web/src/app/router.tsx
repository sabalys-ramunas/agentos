import type { QueryClient } from '@tanstack/react-query';
import type { RouterHistory } from '@tanstack/react-router';
import {
  createRouter,
} from '@tanstack/react-router';
import { Spinner } from '@/components/ui/spinner';
import { canonicalUrl, toInternalPath, toPublicPath } from './paths';
import {
  computerFormRoute,
  computerKeyFormRoute,
  editRoutineRoute,
  emailBatchRoute,
  emailConnectRoute,
  emailInboxRoute,
  emailLeadsRoute,
  emailListRoute,
  emailRoute,
  emailRoutineRoute,
  flowDetailRoute,
  flowsRoute,
  homeRoute,
  needsYouRoute,
  newRoutineBuildRoute,
  newRoutineRoute,
  nodeRoute,
  nodesRoute,
  orgRoute,
  reviewRoute,
  siteApiRoute,
  siteApisRoute,
  teachRoute,
} from './routes/org';
import {
  approveRoute,
  checkEmailRoute,
  indexRoute,
  inviteRoute,
  legalRoute,
  loginRoute,
  onboardingRoute,
  onboardingStepRoute,
  rootActivityRoute,
  rootApprovalsRoute,
  rootLanesRoute,
  rootNeedsYouRoute,
  rootReviewRoute,
  sharedRoute,
  signInRoute,
  signUpRoute,
  verifyRoute,
  welcomeRoute,
  yesInChatRoute,
} from './routes/outside';
import { rootRoute } from './routes/root';
import { catalogRoute } from './routes/catalog';
import { settingsIndexRoute, settingsRoute, settingsRoutes, standingRoute } from './routes/settings';
import {
  accountsRoute,
  activityProofRoute,
  activityRoute,
  brandRoute,
  checkRoute,
  checksRoute,
  environmentRoute,
  environmentsRoute,
  laneRoute,
  loginsRoute,
  preparedMachinesRoute,
  profileRoute,
  runRoute,
  runsRoute,
  workspaceRoute,
} from './routes/work';

const routeTree = rootRoute.addChildren([
  indexRoute,
  rootNeedsYouRoute,
  rootApprovalsRoute,
  rootReviewRoute,
  rootLanesRoute,
  rootActivityRoute,
  signInRoute,
  loginRoute,
  signUpRoute,
  checkEmailRoute,
  verifyRoute,
  legalRoute,
  sharedRoute,
  inviteRoute,
  onboardingRoute,
  onboardingStepRoute,
  approveRoute,
  yesInChatRoute,
  welcomeRoute,
  ...(computerFormRoute ? [computerFormRoute] : []),
  ...(computerKeyFormRoute ? [computerKeyFormRoute] : []),
  orgRoute.addChildren([
    homeRoute,
    needsYouRoute,
    reviewRoute,
    flowsRoute,
    newRoutineRoute,
    newRoutineBuildRoute,
    editRoutineRoute,
    flowDetailRoute,
    emailRoute,
    emailConnectRoute,
    emailInboxRoute,
    emailLeadsRoute,
    emailBatchRoute,
    emailListRoute,
    emailRoutineRoute,
    siteApisRoute,
    teachRoute,
    siteApiRoute,
    nodesRoute,
    nodeRoute,
    environmentsRoute,
    preparedMachinesRoute,
    environmentRoute,
    runsRoute,
    runRoute,
    checksRoute,
    checkRoute,
    brandRoute,
    profileRoute,
    activityRoute,
    activityProofRoute,
    workspaceRoute,
    laneRoute,
    accountsRoute,
    loginsRoute,
    settingsRoute.addChildren([settingsIndexRoute, ...settingsRoutes, standingRoute]),
    // The design system's catalog, in dev only: a live build holds none of it.
    ...(import.meta.env.DEV ? [catalogRoute] : []),
  ]),
]);

/**
 * What a page shows while its code or its first read loads. Without it a fresh load of a lazy page suspended up
 * to the root, so the whole app was blank white for seconds, rail included (2026-11-15 test, F6). Each route
 * gets it, so the shell around a page stays and only the page's own area waits.
 */
function PagePending() {
  return (
    <div role="status" aria-label="Loading" className="flex min-h-48 items-center justify-center gap-2 text-14 text-muted">
      <Spinner />
      Loading
    </div>
  );
}

export function makeRouter(queryClient: QueryClient, history?: RouterHistory) {
  // An old address (an email, a push, a bookmark) opens with the everyday names in the bar.
  if (!history && typeof window !== 'undefined') {
    const canonical = canonicalUrl(window.location.pathname, window.location.search + window.location.hash);
    if (canonical) window.history.replaceState(window.history.state, '', canonical);
  }
  return createRouter({
    routeTree,
    history,
    // The routes keep their engine names; the address bar says the everyday ones (app/paths.ts).
    rewrite: {
      input: ({ url }) => {
        url.pathname = toInternalPath(url.pathname);
        return url;
      },
      output: ({ url }) => {
        url.pathname = toPublicPath(url.pathname);
        return url;
      },
    },
    context: { queryClient },
    defaultPendingComponent: PagePending,
    defaultPendingMs: 150,
    defaultPendingMinMs: 300,
    defaultPreload: 'intent',
    defaultPreloadStaleTime: 0,
    scrollRestoration: true,
  });
}

declare module '@tanstack/react-router' {
  interface Register {
    router: ReturnType<typeof makeRouter>;
  }
}
