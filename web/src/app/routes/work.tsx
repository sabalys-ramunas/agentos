/** Environments, runs, checks, brand, history and accounts: each screen is lazy, with its params in its folder's search.ts. */
import {
  createRoute,
  lazyRouteComponent,
} from '@tanstack/react-router';
import { brandSearch, profileSearch } from '@/features/brand/search';
import { checkSearch, checksSearch } from '@/features/checks/search';
import { runSearch, runsSearch } from '@/features/runs/search';
import { orgRoute } from './org';
import { text } from './root';

// Runs, Checks and Brand: each screen is a lazy component in its own folder, with its params in that
// folder's search.ts, so the people building them never need to edit this file.

// Environments: the cloud computers agents rent for one chat (docs/design/environments.md).
export const environmentsRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/environments',
  component: lazyRouteComponent(() => import('@/features/environments/environments-page'), 'EnvironmentsPage'),
});

// Concept: what the node keeps ready ahead of an open. A static segment, so it wins over $id.
export const preparedMachinesRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/environments/prepared',
  component: lazyRouteComponent(() => import('@/features/environments/prepared-page'), 'PreparedPage'),
});

export const environmentRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/environments/$id',
  component: lazyRouteComponent(() => import('@/features/environments/environment-page'), 'EnvironmentPage'),
});

export const runsRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/runs',
  validateSearch: runsSearch,
  component: lazyRouteComponent(() => import('@/features/runs/runs-page'), 'RunsPage'),
});

export const runRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/runs/$id',
  validateSearch: runSearch,
  component: lazyRouteComponent(() => import('@/features/runs/run-page'), 'RunPage'),
});

export const checksRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/checks',
  validateSearch: checksSearch,
  component: lazyRouteComponent(() => import('@/features/checks/checks-page'), 'ChecksPage'),
});

export const checkRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/checks/$id',
  validateSearch: checkSearch,
  component: lazyRouteComponent(() => import('@/features/checks/check-page'), 'CheckPage'),
});

export const brandRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/brand',
  validateSearch: brandSearch,
  component: lazyRouteComponent(() => import('@/features/brand/brand-page'), 'BrandPage'),
});

export const profileRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/brand/$profile',
  validateSearch: profileSearch,
  component: lazyRouteComponent(() => import('@/features/brand/profile-page'), 'ProfilePage'),
});

export interface ActivitySearch {
  workspace?: string;
  lane?: string;
  principal?: string;
  effect?: string;
  status?: string;
  run?: string;
  range?: string;
  from?: string;
  to?: string;
  /** History's kind of work: sent, money, forms, research, shop, files. */
  kind?: string;
}

export const activityRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/activity',
  validateSearch: (search: Record<string, unknown>): ActivitySearch => ({
    workspace: text(search.workspace),
    lane: text(search.lane),
    principal: text(search.principal),
    effect: text(search.effect),
    status: text(search.status),
    run: text(search.run),
    range: text(search.range),
    from: text(search.from),
    to: text(search.to),
    kind: text(search.kind),
  }),
  component: lazyRouteComponent(() => import('@/features/activity/activity'), 'Activity'),
});

/** Proof: one account's day replayed and checked, and the read-only links made from it. */
export const activityProofRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/activity/proof',
  validateSearch: (search: Record<string, unknown>): { lane?: string; day?: string } => ({
    lane: text(search.lane),
    day: text(search.day),
  }),
  component: lazyRouteComponent(() => import('@/features/activity/proof/proof-page'), 'ProofPage'),
});

/** One business: its accounts, and for the owner what is still to log in. */
export const workspaceRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/workspaces/$ws',
  component: lazyRouteComponent(() => import('@/features/home/workspace-page'), 'WorkspacePage'),
});

/** Every account, by business: the everyday list of what used to be lanes. */
export const accountsRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/accounts',
  validateSearch: (search: Record<string, unknown>): { workspace?: string } => ({ workspace: text(search.workspace) }),
  component: lazyRouteComponent(() => import('@/features/home/accounts'), 'Accounts'),
});

export const loginsRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/logins',
  validateSearch: (search: Record<string, unknown>): { lane?: string } => ({ lane: text(search.lane) }),
  component: lazyRouteComponent(() => import('@/features/lane/logins'), 'Logins'),
});

export const laneRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/lanes/$ws/$name',
  component: lazyRouteComponent(() => import('@/features/lane/lane'), 'LaneWindow'),
});
