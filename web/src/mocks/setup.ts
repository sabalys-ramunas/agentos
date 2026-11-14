/**
 * The mock of first run: Getting started worked out from the org's real state, a one-click
 * invite join, and, standing in for a real AI, a first request that arrives a few seconds after
 * the first connection is made in a new org.
 */
import { delay, http, HttpResponse } from 'msw';
import type { Approval, GettingStarted, GoalKey } from '@/api/types';
import { workOutItems } from '@/features/onboarding/setup';
import { emit } from './bus';
import { withPlace } from './rebuild';
import { scenario } from './scenario';
import type { OrgData } from './seed';
import type { Helpers } from './s3';

const GOAL_TITLES: Record<GoalKey, string> = {
  post: 'Post for a business',
  email: 'Handle email',
  invoices: 'Collect invoices and receipts',
  research: 'Research and keep a list',
  forms: 'Fill in forms on websites',
  other: 'Something else',
};

/** The goal the org was made with. The demo org is long set up with "post". */
function goalOf(data: OrgData): GoalKey | null {
  const goal = (data.org as { goal?: GoalKey | null }).goal;
  if (goal !== undefined) return goal;
  return data.org.slug === 'northwind-group' ? 'post' : null;
}

function gettingStarted(data: OrgData): GettingStarted {
  const goal = goalOf(data);
  // `?mock=setup` shows the demo org part way, for Home's Getting started.
  const partWay = scenario.has('setup');
  const items = workOutItems({
    goal,
    workspaces: data.workspaces.map((record) => ({ lanes: data.lanes.filter((lane) => lane.workspace === record.id) })),
    nodes: data.nodes,
    grants: data.grants,
    decided: data.history.length,
    phones: partWay ? 0 : data.pushDevices.length,
    invited: partWay ? 0 : data.invites.filter((item) => item.state !== 'revoked').length,
    people: partWay ? 1 : data.members.length,
  });
  // Otherwise the demo org is all set up, as months of use would leave it.
  if (data.org.slug === 'northwind-group' && !partWay) items.forEach((item) => (item.done = true));
  const done = items.filter((item) => item.done).length;
  return { goal: goal ? { key: goal, title: GOAL_TITLES[goal] } : null, items, done, total: items.length, complete: done === items.length };
}

const tried = new WeakSet<OrgData>();

/** Stands in for the person's AI doing the starter job: one real request, in this org. */
function firstTry(data: OrgData) {
  if (tried.has(data) || data.approvals.length || data.history.length) return;
  tried.add(data);
  const workspace = data.workspaces[0];
  const lane = data.lanes.find((item) => item.workspace === workspace?.id && item.kind === 'browser')?.id ?? `${workspace?.id ?? 'personal'}/instagram`;
  const name = workspace?.name ?? data.org.name;
  setTimeout(() => {
    const id = `ap_first_${Date.now().toString(36)}`;
    const approval: Approval = {
      id,
      kind: 'action',
      state: 'pending',
      effect: goalOf(data) === 'research' ? 'draft' : 'commit',
      lane,
      summary: `Post to ${name}'s ${lane.split('/')[1]}`,
      content_hash: `sha256:${id}`,
      content_digest: null,
      flow_target: null,
      tool: 'browser_act',
      reason: 'The first job from setup.',
      delivery: null,
      delivery_error: null,
      preview: {
        text: `Something new at ${name} this week. Come and see us, and tell us what you think.`,
        image: `/api/orgs/${data.org.slug}/approvals/${id}/preview.png`,
      },
      requested_by: { kind: 'agent', id: 'claude-code', thread: null },
      created_at: new Date().toISOString(),
      expires_at: new Date(Date.now() + 20 * 3_600_000).toISOString(),
    };
    data.approvals.unshift(approval);
    emit(data.org.slug, { type: 'approval', payload: withPlace(data, approval) });
  }, 8_000);
}

/**
 * First in the handler list: the side effects answer nothing, so the request goes on to the
 * handler that really makes the connection.
 */
export function setupHandlers({ org, find }: Pick<Helpers, 'org'> & { find: (slug: string) => OrgData | undefined }, join: (token: string) => Response | Promise<Response>) {
  return [
    http.get('/api/orgs/:org/getting-started', org((data) => HttpResponse.json(gettingStarted(data)))),
    http.post('/api/orgs/:org/grants', ({ params }) => {
      // Not through `org()`: that answers, and this only watches.
      const data = find(String(params.org));
      if (data) firstTry(data);
      return undefined;
    }),
    http.post('/api/invites/:token/join', async ({ params }) => {
      await delay(320);
      return join(String(params.token));
    }),
  ];
}
