/** The org's shell and its first places: Home, Needs you, routines, site APIs, email and computers. */
import {
  createRoute,
  lazyRouteComponent,
  redirect,
  useParams,
} from '@tanstack/react-router';
import { AppShell } from '@/components/shell/app-shell';
import type { ConnectStage } from '@/features/email/words';
import { homeSearch } from '@/features/home/search';
import { needsYouSearch } from '@/features/needs-you/search';
import { nodeSearch, nodesSearch } from '@/features/nodes/search';
import { currentSession, requireSession, rootRoute, text } from './root';

// The app

function Shell() {
  const { org } = useParams({ from: '/$org' });
  return <AppShell org={org} />;
}

export const orgRoute = createRoute({
  getParentRoute: () => rootRoute,
  path: '/$org',
  beforeLoad: requireSession,
  component: Shell,
});

export const homeRoute = createRoute({
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

export const needsYouRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/needs-you',
  validateSearch: needsYouSearch,
  component: lazyRouteComponent(() => import('@/features/needs-you/needs-you'), 'NeedsYou'),
});

/** A Review link from a push, an email or a webhook. `e` and `s` are its expiry and signature. */
export const reviewRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/review/$id',
  validateSearch: (search: Record<string, unknown>): { e?: string; s?: string; from?: 'chat' } => ({
    e: typeof search.e === 'number' ? String(search.e) : text(search.e),
    s: text(search.s),
    ...(search.from === 'chat' ? { from: 'chat' as const } : {}),
  }),
  component: lazyRouteComponent(() => import('@/features/needs-you/review'), 'Review'),
});

export const flowsRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/flows',
  validateSearch: (search: Record<string, unknown>): { tab?: 'schedules' } => (search.tab === 'schedules' ? { tab: 'schedules' } : {}),
  component: lazyRouteComponent(() => import('@/features/flows/flows'), 'Flows'),
});

export const flowDetailRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/flows/$flow',
  component: lazyRouteComponent(() => import('@/features/flows/flow-detail'), 'FlowDetail'),
});

export const newRoutineRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/flows/new',
  validateSearch: (search: Record<string, unknown>): { workspace?: string } => ({ workspace: text(search.workspace) }),
  component: lazyRouteComponent(() => import('@/features/flows/new-routine'), 'NewRoutine'),
});

export const newRoutineBuildRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/flows/new/$template',
  validateSearch: (search: Record<string, unknown>): { workspace?: string } => ({ workspace: text(search.workspace) }),
  component: lazyRouteComponent(() => import('@/features/flows/builder'), 'NewRoutineBuilder'),
});

export const editRoutineRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/flows/$flow/edit',
  component: lazyRouteComponent(() => import('@/features/flows/builder'), 'EditRoutine'),
});

export type SiteDoor = 'data' | 'code';
export type TeachStep = 'account' | 'page' | 'found' | 'columns' | 'try' | 'activate' | 'done';
const TEACH_STEPS: readonly string[] = ['account', 'page', 'found', 'columns', 'try', 'activate', 'done'];

export const siteApisRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/site-apis',
  component: lazyRouteComponent(() => import('@/features/site-apis/site-apis'), 'SiteApis'),
});

export const teachRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/site-apis/new',
  validateSearch: (search: Record<string, unknown>): { step?: TeachStep; lane?: string } => ({
    step: typeof search.step === 'string' && TEACH_STEPS.includes(search.step) ? (search.step as TeachStep) : undefined,
    lane: text(search.lane),
  }),
  component: lazyRouteComponent(() => import('@/features/site-apis/teach'), 'Teach'),
});

export const siteApiRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/site-apis/$api',
  validateSearch: (search: Record<string, unknown>): { endpoint?: string; door?: SiteDoor } => ({
    endpoint: text(search.endpoint),
    door: search.door === 'code' ? 'code' : search.door === 'data' ? 'data' : undefined,
  }),
  component: lazyRouteComponent(() => import('@/features/site-apis/site-api-detail'), 'SiteApiDetail'),
});

const CONNECT_STAGES: readonly string[] = ['address', 'found', 'password', 'hand-over', 'waiting', 'checks', 'done', 'failed'];

export const emailRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/email',
  component: lazyRouteComponent(() => import('@/features/email/mailboxes'), 'Mailboxes'),
});

export const emailConnectRoute = createRoute({
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

export const emailInboxRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/email/new-mail',
  validateSearch: (search: Record<string, unknown>): { mailbox?: string; message?: string } => ({ mailbox: text(search.mailbox), message: text(search.message) }),
  component: lazyRouteComponent(() => import('@/features/email/reading'), 'Reading'),
});

export const emailLeadsRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/email/leads',
  component: lazyRouteComponent(() => import('@/features/email/leads'), 'Leads'),
});

export const emailBatchRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/email/leads/$batch',
  component: lazyRouteComponent(() => import('@/features/email/batch'), 'Batch'),
});

export const emailListRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/email/lists/$list',
  component: lazyRouteComponent(() => import('@/features/email/list'), 'MailListPage'),
});

export const emailRoutineRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/email/routines/$routine',
  component: lazyRouteComponent(() => import('@/features/email/routine'), 'MailRoutinePage'),
});

/**
 * The same, for a key to an AI service. Under the same reserved slug, so no org name is taken. Both
 * concept drawings exist in mock mode only: a live build leaves them out (the lazy import goes with the
 * route, and build/guard.ts checks the output for their words).
 */
export const computerKeyFormRoute = import.meta.env.VITE_API_MODE !== 'mock' ? null : createRoute({
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
export const computerFormRoute = import.meta.env.VITE_API_MODE !== 'mock' ? null : createRoute({
  getParentRoute: () => rootRoute,
  path: '/computer-form',
  validateSearch: (search: Record<string, unknown>): { address?: string; computer?: string; mailbox?: string; state?: string } => ({
    address: text(search.address),
    computer: text(search.computer),
    mailbox: text(search.mailbox),
    state: text(search.state),
  }),
  component: lazyRouteComponent(() => import('@/features/email/computer-form'), 'ComputerForm'),
});

/**
 * Machines. Inside the app the routes keep the engine's name, `nodes`; the address bar says
 * `/machines` (app/paths.ts), and `/computers` and `/nodes` addresses still open the same pages.
 */
export const nodesRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/nodes',
  validateSearch: nodesSearch,
  component: lazyRouteComponent(() => import('@/features/nodes/nodes'), 'Nodes'),
});

export const nodeRoute = createRoute({
  getParentRoute: () => orgRoute,
  path: '/nodes/$id',
  validateSearch: nodeSearch,
  component: lazyRouteComponent(() => import('@/features/nodes/node-detail'), 'NodeDetailScreen'),
});
