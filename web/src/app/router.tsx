import type { QueryClient } from '@tanstack/react-query';
import type { RouterHistory } from '@tanstack/react-router';
import {
  createRootRouteWithContext,
  createRoute,
  createRouter,
  lazyRouteComponent,
  notFound,
  Outlet,
  redirect,
  useParams,
} from '@tanstack/react-router';
import { sessionQuery } from '@/api/hooks';
import { NotFound } from '@/components/not-found';
import { AppShell } from '@/components/shell/app-shell';
import { ErrorState } from '@/components/ui/states';
import { apiMode } from '@/config';
import type { ConnectStage } from '@/features/email/words';
import { safeNext } from '@/lib/format';
import { brandSearch, profileSearch } from '@/features/brand/search';
import { checkSearch, checksSearch } from '@/features/checks/search';
import { homeSearch } from '@/features/home/search';
import { needsYouSearch } from '@/features/needs-you/search';
import { nodeSearch, nodesSearch } from '@/features/nodes/search';
import { runSearch, runsSearch } from '@/features/runs/search';
import { canonicalUrl, toInternalPath, toPublicPath } from './paths';

interface RouterContext {
  queryClient: QueryClient;
}

const rootRoute = createRootRouteWithContext<RouterContext>()({
  component: Outlet,
  notFoundComponent: () => (
    <div className="flex min-h-dvh items-center justify-center">
      <NotFound />
    </div>
  ),
  errorComponent: ({ error }) => <ErrorState error={error} onRetry={() => location.reload()} />,
});

// A machine's tabs and params live with its screen (features/nodes/search.ts); these names stay here too.
export { NODE_TABS } from '@/features/nodes/search';
export type { NodeSearch, NodeTab } from '@/features/nodes/search';

const text = (value: unknown) => (typeof value === 'string' && value ? value : undefined);

async function currentSession(client: QueryClient) {
  try {
    return await client.ensureQueryData(sessionQuery);
  } catch {
    return null;
  }
}

const indexRoute = createRoute({
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

const rootNeedsYouRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/needs-you',
  beforeLoad: async ({ context, location }) => {
    const org = await firstOrg(context, location.href);
    throw redirect({ to: '/$org/needs-you', params: { org }, search: true, replace: true });
  },
});

const rootApprovalsRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/approvals',
  beforeLoad: async ({ context, location }) => {
    const org = await firstOrg(context, location.href);
    throw redirect({ to: '/$org/needs-you', params: { org }, search: true, replace: true });
  },
});

const rootReviewRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/review/$id',
  beforeLoad: async ({ context, location, params }) => {
    const org = await firstOrg(context, location.href);
    throw redirect({ to: '/$org/review/$id', params: { org, id: params.id }, search: true, replace: true });
  },
});

const rootLanesRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/lanes/$ws/$name',
  beforeLoad: async ({ context, location, params }) => {
    const org = await firstOrg(context, location.href);
    throw redirect({ to: '/$org/lanes/$ws/$name', params: { org, ws: params.ws, name: params.name }, replace: true });
  },
});

const rootActivityRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/activity',
  beforeLoad: async ({ context, location }) => {
    const org = await firstOrg(context, location.href);
    throw redirect({ to: '/$org/activity', params: { org }, search: true, replace: true });
  },
});

// Auth

const signInRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/sign-in',
  validateSearch: (search: Record<string, unknown>): { next?: string; error?: string; reason?: string } => ({
    next: safeNext(search.next),
    error: text(search.error),
    reason: text(search.reason),
  }),
  component: lazyRouteComponent(() => import('@/features/auth/sign-in'), 'SignIn'),
});

// `agentos panel login` prints this page's address with the code after the #, so it never reaches
// a server log.
const loginRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/login',
  component: lazyRouteComponent(() => import('@/features/auth/code-login'), 'CodeLogin'),
});

const signUpRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/sign-up',
  validateSearch: (search: Record<string, unknown>): { next?: string } => ({ next: safeNext(search.next) }),
  component: lazyRouteComponent(() => import('@/features/auth/sign-up'), 'SignUp'),
});

const checkEmailRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/check-email',
  validateSearch: (search: Record<string, unknown>): { email?: string; next?: string; aup?: boolean } => ({
    email: text(search.email),
    next: safeNext(search.next),
    aup: search.aup === true || search.aup === 'true' ? true : undefined,
  }),
  component: lazyRouteComponent(() => import('@/features/auth/check-email'), 'CheckEmail'),
});

const verifyRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/auth/verify',
  validateSearch: (search: Record<string, unknown>): { token?: string; next?: string; step_up?: boolean } => ({
    token: text(search.token),
    next: safeNext(search.next),
    step_up: search.step_up === 1 || search.step_up === '1' || search.step_up === true ? true : undefined,
  }),
  component: lazyRouteComponent(() => import('@/features/auth/verify'), 'Verify'),
});

const legalRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/legal/acceptable-use',
  component: lazyRouteComponent(() => import('@/features/auth/acceptable-use'), 'AcceptableUse'),
});

/** A read-only link from Proof. Opened with no sign-in: the token in the fragment is the only credential. */
const sharedRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/shared',
  component: lazyRouteComponent(() => import('@/features/shared/shared-page'), 'SharedPage'),
});

const inviteRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/invite/$token',
  component: lazyRouteComponent(() => import('@/features/auth/invite'), 'InviteAccept'),
});

// Onboarding

const onboardingRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/onboarding',
  beforeLoad: requireSession,
  component: lazyRouteComponent(() => import('@/features/onboarding/onboarding'), 'CreateOrgStep'),
});

const onboardingStepRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/onboarding/$org/$step',
  beforeLoad: requireSession,
  component: lazyRouteComponent(() => import('@/features/onboarding/onboarding'), 'OnboardingStep'),
});

// The phone view has no shell: one decision at a time, full height.

const approveRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/$org/approve',
  beforeLoad: requireSession,
  component: lazyRouteComponent(() => import('@/features/needs-you/approve'), 'Approve'),
});

/** Concept stage: how a yes looks in a chat. A stand-alone mock, so a live build has no such page. */
const yesInChatRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/$org/concept/yes-in-chat',
  beforeLoad: (context) => {
    if (apiMode !== 'mock') throw notFound();
    return requireSession(context);
  },
  component: lazyRouteComponent(() => import('@/features/confirmations/yes-in-chat'), 'YesInChat'),
});

/** The first screen after an invite is accepted: what you will be asked. No shell, like the phone view. */
const welcomeRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/$org/welcome',
  beforeLoad: requireSession,
  validateSearch: (search: Record<string, unknown>): { from?: string } => ({ from: text(search.from)?.slice(0, 40) }),
  component: lazyRouteComponent(() => import('@/features/auth/welcome'), 'Welcome'),
});

async function requireSession({ context, location }: { context: RouterContext; location: { href: string } }) {
  const session = await currentSession(context.queryClient);
  if (!session) throw redirect({ to: '/sign-in', search: { next: location.href } });
}

// The app

function Shell() {
  const { org } = useParams({ from: '/$org' });
  return <AppShell org={org} />;
}

const orgRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/$org',
  beforeLoad: requireSession,
  component: Shell,
});

const homeRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/',
  // An approver's place is Needs you: their menu has no Home (structure.md, startup.md B5).
  beforeLoad: async ({ context, params }) => {
    const session = await currentSession(context.queryClient);
    if (session?.orgs.find((item) => item.slug === params.org)?.role === 'approver') {
      throw redirect({ to: '/$org/needs-you', params: { org: params.org } });
    }
  },
  validateSearch: homeSearch,
  component: lazyRouteComponent(() => import('@/features/home/home'), 'Home'),
});

const needsYouRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/needs-you',
  validateSearch: needsYouSearch,
  component: lazyRouteComponent(() => import('@/features/needs-you/needs-you'), 'NeedsYou'),
});

/** A Review link from a push, an email or a webhook. `e` and `s` are its expiry and signature. */
const reviewRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/review/$id',
  validateSearch: (search: Record<string, unknown>): { e?: string; s?: string; from?: 'chat' } => ({
    e: typeof search.e === 'number' ? String(search.e) : text(search.e),
    s: text(search.s),
    ...(search.from === 'chat' ? { from: 'chat' as const } : {}),
  }),
  component: lazyRouteComponent(() => import('@/features/needs-you/review'), 'Review'),
});

const flowsRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/flows',
  validateSearch: (search: Record<string, unknown>): { tab?: 'schedules' } => (search.tab === 'schedules' ? { tab: 'schedules' } : {}),
  component: lazyRouteComponent(() => import('@/features/flows/flows'), 'Flows'),
});

const flowDetailRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/flows/$flow',
  component: lazyRouteComponent(() => import('@/features/flows/flow-detail'), 'FlowDetail'),
});

const newRoutineRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/flows/new',
  validateSearch: (search: Record<string, unknown>): { workspace?: string } => ({ workspace: text(search.workspace) }),
  component: lazyRouteComponent(() => import('@/features/flows/new-routine'), 'NewRoutine'),
});

const newRoutineBuildRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/flows/new/$template',
  validateSearch: (search: Record<string, unknown>): { workspace?: string } => ({ workspace: text(search.workspace) }),
  component: lazyRouteComponent(() => import('@/features/flows/builder'), 'NewRoutineBuilder'),
});

const editRoutineRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/flows/$flow/edit',
  component: lazyRouteComponent(() => import('@/features/flows/builder'), 'EditRoutine'),
});

export type SiteDoor = 'data' | 'code';
export type TeachStep = 'account' | 'page' | 'found' | 'columns' | 'try' | 'activate' | 'done';
const TEACH_STEPS: readonly string[] = ['account', 'page', 'found', 'columns', 'try', 'activate', 'done'];

const siteApisRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/site-apis',
  component: lazyRouteComponent(() => import('@/features/site-apis/site-apis'), 'SiteApis'),
});

const teachRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/site-apis/new',
  validateSearch: (search: Record<string, unknown>): { step?: TeachStep; lane?: string } => ({
    step: typeof search.step === 'string' && TEACH_STEPS.includes(search.step) ? (search.step as TeachStep) : undefined,
    lane: text(search.lane),
  }),
  component: lazyRouteComponent(() => import('@/features/site-apis/teach'), 'Teach'),
});

const siteApiRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/site-apis/$api',
  validateSearch: (search: Record<string, unknown>): { endpoint?: string; door?: SiteDoor } => ({
    endpoint: text(search.endpoint),
    door: search.door === 'code' ? 'code' : search.door === 'data' ? 'data' : undefined,
  }),
  component: lazyRouteComponent(() => import('@/features/site-apis/site-api-detail'), 'SiteApiDetail'),
});

const CONNECT_STAGES: readonly string[] = ['address', 'found', 'password', 'hand-over', 'waiting', 'checks', 'done', 'failed'];

const emailRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/email',
  component: lazyRouteComponent(() => import('@/features/email/mailboxes'), 'Mailboxes'),
});

const emailConnectRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/email/connect',
  validateSearch: (search: Record<string, unknown>): { address?: string; stage?: ConnectStage; ending?: string; computer?: string } => ({
    address: text(search.address),
    stage: typeof search.stage === 'string' && CONNECT_STAGES.includes(search.stage) ? (search.stage as ConnectStage) : undefined,
    ending: text(search.ending),
    computer: text(search.computer),
  }),
  component: lazyRouteComponent(() => import('@/features/email/connect'), 'Connect'),
});

const emailInboxRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/email/new-mail',
  validateSearch: (search: Record<string, unknown>): { mailbox?: string; message?: string } => ({ mailbox: text(search.mailbox), message: text(search.message) }),
  component: lazyRouteComponent(() => import('@/features/email/reading'), 'Reading'),
});

const emailLeadsRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/email/leads',
  component: lazyRouteComponent(() => import('@/features/email/leads'), 'Leads'),
});

const emailBatchRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/email/leads/$batch',
  component: lazyRouteComponent(() => import('@/features/email/batch'), 'Batch'),
});

const emailListRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/email/lists/$list',
  component: lazyRouteComponent(() => import('@/features/email/list'), 'MailListPage'),
});

const emailRoutineRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/email/routines/$routine',
  component: lazyRouteComponent(() => import('@/features/email/routine'), 'MailRoutinePage'),
});

/**
 * The same, for a key to an AI service. Under the same reserved slug, so no org name is taken. Both
 * concept drawings exist in mock mode only: a live build leaves them out (the lazy import goes with the
 * route, and build/guard.ts checks the output for their words).
 */
const computerKeyFormRoute = import.meta.env.VITE_API_MODE !== 'mock' ? null : createRoute({
  getParentRoute: () => rootRoute,
  path: '/computer-form/ai-key',
  validateSearch: (search: Record<string, unknown>): { service?: string; computer?: string; state?: string } => ({
    service: text(search.service),
    computer: text(search.computer),
    state: text(search.state),
  }),
  component: lazyRouteComponent(() => import('@/features/workers/key-form'), 'KeyForm'),
});

/** What a computer would serve itself, drawn as a concept. It sits outside the panel on purpose. Mock mode only. */
const computerFormRoute = import.meta.env.VITE_API_MODE !== 'mock' ? null : createRoute({
  getParentRoute: () => rootRoute,
  path: '/computer-form',
  validateSearch: (search: Record<string, unknown>): { address?: string; computer?: string; state?: string } => ({
    address: text(search.address),
    computer: text(search.computer),
    state: text(search.state),
  }),
  component: lazyRouteComponent(() => import('@/features/email/computer-form'), 'ComputerForm'),
});

/**
 * Machines. Inside the app the routes keep the engine's name, `nodes`; the address bar says
 * `/machines` (app/paths.ts), and `/computers` and `/nodes` addresses still open the same pages.
 */
const nodesRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/nodes',
  validateSearch: nodesSearch,
  component: lazyRouteComponent(() => import('@/features/nodes/nodes'), 'Nodes'),
});

const nodeRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/nodes/$id',
  validateSearch: nodeSearch,
  component: lazyRouteComponent(() => import('@/features/nodes/node-detail'), 'NodeDetailScreen'),
});

// Runs, Checks and Brand: each screen is a lazy component in its own folder, with its params in that
// folder's search.ts, so the people building them never need to edit this file.

// Environments: the cloud computers agents rent for one chat (docs/design/environments.md).
const environmentsRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/environments',
  component: lazyRouteComponent(() => import('@/features/environments/environments-page'), 'EnvironmentsPage'),
});

const environmentRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/environments/$id',
  component: lazyRouteComponent(() => import('@/features/environments/environment-page'), 'EnvironmentPage'),
});

const runsRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/runs',
  validateSearch: runsSearch,
  component: lazyRouteComponent(() => import('@/features/runs/runs-page'), 'RunsPage'),
});

const runRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/runs/$id',
  validateSearch: runSearch,
  component: lazyRouteComponent(() => import('@/features/runs/run-page'), 'RunPage'),
});

const checksRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/checks',
  validateSearch: checksSearch,
  component: lazyRouteComponent(() => import('@/features/checks/checks-page'), 'ChecksPage'),
});

const checkRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/checks/$id',
  validateSearch: checkSearch,
  component: lazyRouteComponent(() => import('@/features/checks/check-page'), 'CheckPage'),
});

const brandRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/brand',
  validateSearch: brandSearch,
  component: lazyRouteComponent(() => import('@/features/brand/brand-page'), 'BrandPage'),
});

const profileRoute = createRoute({
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

const activityRoute = createRoute({
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
const activityProofRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/activity/proof',
  validateSearch: (search: Record<string, unknown>): { lane?: string; day?: string } => ({
    lane: text(search.lane),
    day: text(search.day),
  }),
  component: lazyRouteComponent(() => import('@/features/activity/proof/proof-page'), 'ProofPage'),
});

/** One business: its accounts, and for the owner what is still to log in. */
const workspaceRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/workspaces/$ws',
  component: lazyRouteComponent(() => import('@/features/home/workspace-page'), 'WorkspacePage'),
});

/** Every account, by business: the everyday list of what used to be lanes. */
const accountsRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/accounts',
  validateSearch: (search: Record<string, unknown>): { workspace?: string } => ({ workspace: text(search.workspace) }),
  component: lazyRouteComponent(() => import('@/features/home/accounts'), 'Accounts'),
});

/** Engagement on an account: what its plan did, the plan, and what it is connected to (features/engage). */
const laneEngagementRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/lanes/$ws/$name/engagement',
  validateSearch: (search: Record<string, unknown>): { tab?: 'today' | 'plan' | 'connected'; day?: string } => {
    const tab = text(search.tab);
    return { tab: tab === 'plan' || tab === 'connected' ? tab : undefined, day: text(search.day) };
  },
  component: lazyRouteComponent(() => import('@/features/engage/account'), 'AccountEngagement'),
});

/** Every account's day, across the accounts the person may see. */
const accountsEngagementRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/accounts/engagement',
  validateSearch: (search: Record<string, unknown>): { day?: string } => ({ day: text(search.day) }),
  component: lazyRouteComponent(() => import('@/features/engage/day'), 'EngagementDay'),
});

const loginsRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/logins',
  validateSearch: (search: Record<string, unknown>): { lane?: string } => ({ lane: text(search.lane) }),
  component: lazyRouteComponent(() => import('@/features/lane/logins'), 'Logins'),
});

const laneRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/lanes/$ws/$name',
  component: lazyRouteComponent(() => import('@/features/lane/lane'), 'LaneWindow'),
});

// Settings

const settingsRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/settings',
  component: lazyRouteComponent(() => import('@/features/settings/layout'), 'SettingsLayout'),
});

const settingsIndexRoute = createRoute({
  getParentRoute: () => settingsRoute,
  path: '/',
  component: lazyRouteComponent(() => import('@/features/settings/layout'), 'SettingsIndex'),
});

function settingsPage<Path extends string>(path: Path, load: Parameters<typeof lazyRouteComponent>[0], name: string) {
  return createRoute({
    getParentRoute: () => settingsRoute,
    path,
    component: lazyRouteComponent(load as never, name as never),
  });
}

/** Which AI service and which computer. A key is never part of this, or of any address. */
const addServiceRoute = createRoute({
  getParentRoute: () => settingsRoute,
  path: '/brains/add',
  validateSearch: (search: Record<string, unknown>): { service?: string; computer?: string } => ({
    service: text(search.service),
    computer: text(search.computer),
  }),
  component: lazyRouteComponent(() => import('@/features/workers/add-service'), 'AddService'),
});

const settingsRoutes = [
  settingsPage('/general', () => import('@/features/settings/general'), 'GeneralSettings'),
  settingsPage('/members', () => import('@/features/settings/members'), 'MembersSettings'),
  settingsPage('/invites', () => import('@/features/settings/members'), 'InvitesSettings'),
  settingsPage('/workspaces', () => import('@/features/settings/workspaces'), 'WorkspacesSettings'),
  settingsPage('/access', () => import('@/features/settings/members'), 'WorkspaceAccessSettings'),
  settingsPage('/grants', () => import('@/features/settings/grants'), 'GrantsSettings'),
  settingsPage('/notifications', () => import('@/features/settings/notifications'), 'NotificationSettingsScreen'),
  settingsPage('/webhooks', () => import('@/features/settings/webhooks'), 'WebhooksSettings'),
  settingsPage('/retention', () => import('@/features/settings/retention'), 'RetentionSettings'),
  settingsPage('/memory', () => import('@/features/memory/memory-page'), 'MemorySettings'),
  settingsPage('/billing', () => import('@/features/settings/billing'), 'BillingSettings'),
  settingsPage('/audit', () => import('@/features/settings/audit'), 'AuditSettings'),
  settingsPage('/danger', () => import('@/features/settings/general'), 'DangerSettings'),
  settingsPage('/yes', () => import('@/features/confirmations/what-needs-a-yes'), 'WhatNeedsAYes'),
  settingsPage('/say-yes', () => import('@/features/confirmations/where-you-say-yes'), 'WhereYouSayYes'),
  settingsPage('/brains', () => import('@/features/settings/brains'), 'BrainsSettings'),
  addServiceRoute,
  settingsPage('/workers', () => import('@/features/workers/workers-page'), 'WorkersSettings'),
  settingsPage('/vault', () => import('@/features/settings/vault'), 'VaultSettings'),
  settingsPage('/account', () => import('@/features/settings/account'), 'ProfileSettings'),
  settingsPage('/passkeys', () => import('@/features/settings/account'), 'PasskeySettings'),
  settingsPage('/devices', () => import('@/features/settings/account'), 'DeviceSettings'),
] as const;

const standingRoute = createRoute({
  getParentRoute: () => settingsRoute,
  path: '/standing',
  validateSearch: (search: Record<string, unknown>): { show?: 'all' } => (search.show === 'all' ? { show: 'all' } : {}),
  component: lazyRouteComponent(() => import('@/features/standing/standing'), 'StandingSettings'),
});

/** The design specimen: every primitive in every state. Dev only, never linked from the app. */
const designRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/design',
  component: lazyRouteComponent(() => import('@/components/ui/specimen/specimen'), 'Specimen'),
});

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
    laneEngagementRoute,
    accountsRoute,
    accountsEngagementRoute,
    loginsRoute,
    settingsRoute.addChildren([settingsIndexRoute, ...settingsRoutes, standingRoute]),
    ...(import.meta.env.DEV ? [designRoute] : []),
  ]),
]);

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
