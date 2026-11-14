/**
 * What is really set up, for the setup steps and Home's Getting started list.
 *
 * The cloud keeps the first job on the org (`goal`, `goal_note`) and works out Getting started
 * from real data on every call (`GET /api/orgs/{org}/getting-started`, api-for-rebuild.md 13).
 * What only this person's setup needs (the sites they picked, "Ask me first", the step they were
 * on, and "Hide this list") is kept in this browser. Where the cloud does not serve Getting
 * started yet, the same list is worked out here from what the panel can already read. Either way
 * an item is done because it is true, never because a step was clicked through.
 */
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useMemo, useSyncExternalStore } from 'react';
import { fleet, orgs, setup as setupApi } from '@/api/client';
import { isNotServed, served } from '@/api/http';
import { keys } from '@/api/keys';
import type { GettingStartedItem, GoalKey, SetupItemKey, Workspace } from '@/api/types';
import { computerName } from '@/lib/computers';
import type { KnownAgent } from '@/lib/principal';
import { AGENT_NAMES } from '@/lib/principal';

/** What it may do at all: the plain form of the grant ceiling. */
export interface SetupAsks {
  post: boolean;
  pay: boolean;
  delete: boolean;
}

/** Until a first job says otherwise: it may post or send, and does that on its own. */
export const DEFAULT_ASKS: SetupAsks = { post: true, pay: false, delete: false };

/** What this browser keeps for setup. The cloud has no place for these yet. */
export interface Saved {
  /** The workspace the first job is for. */
  workspace: string | null;
  /** Site keys chosen on the accounts step. */
  sites: string[] | null;
  asks: SetupAsks | null;
  /** The step they were on, so leaving and coming back lands there. */
  step: string | null;
  /** "Hide this list" on Home. */
  hidden: boolean;
  /** The AI they made a connection for. */
  agent: KnownAgent | null;
  /** Kept here too, for a cloud that does not store it yet. */
  goal: GoalKey | null;
  goal_note: string | null;
}

/** The panel's view of setup: the cloud's list and goal, with this browser's choices. */
export interface Setup extends Saved {
  items: GettingStartedItem[];
}

/** The grant in one line. A commit runs on its own; money and deleting always ask. */
export function mayWords(asks: SetupAsks): string {
  if (!asks.post) return 'It can only read and write drafts.';
  const heavy = [asks.pay && 'spends money', asks.delete && 'deletes anything'].filter(Boolean).join(' or ');
  return heavy ? `It works on its own, and always asks you before it ${heavy}.` : 'It works on its own. It cannot spend money or delete anything.';
}

const EMPTY: Saved = { workspace: null, sites: null, asks: null, step: null, hidden: false, agent: null, goal: null, goal_note: null };

const storageKey = (org: string) => `agentos.setup.${org}`;
const listeners = new Set<() => void>();
const cache = new Map<string, { raw: string | null; value: Saved }>();

function readLocal(org: string): Saved {
  let raw: string | null = null;
  try {
    raw = localStorage.getItem(storageKey(org));
  } catch {
    raw = null;
  }
  const hit = cache.get(org);
  if (hit && hit.raw === raw) return hit.value;
  let value = EMPTY;
  try {
    value = raw ? { ...EMPTY, ...(JSON.parse(raw) as Partial<Saved>) } : EMPTY;
  } catch {
    value = EMPTY;
  }
  cache.set(org, { raw, value });
  return value;
}

/** Keeps a setup choice in this browser. */
export function remember(org: string, change: Partial<Saved>) {
  const next = { ...readLocal(org), ...change };
  try {
    localStorage.setItem(storageKey(org), JSON.stringify(next));
  } catch {
    // Private mode can refuse storage: the choice then lasts for this page.
  }
  listeners.forEach((listener) => listener());
}

export function useSaved(org: string): Saved {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => readLocal(org),
    () => EMPTY,
  );
}

const ITEM: Record<SetupItemKey, { title: string; hint: string; minutes: number }> = {
  goal: { title: 'Choose a first job', hint: 'Tell agentos what your AI should do first.', minutes: 1 },
  computer: { title: 'Add a computer', hint: 'Your AI works on a computer of its own.', minutes: 5 },
  account: { title: 'Sign in to an account', hint: 'Sign in once to the site your AI will use.', minutes: 2 },
  ai: { title: 'Connect your AI', hint: 'Connect Claude or ChatGPT so it can use the computer.', minutes: 3 },
  approval: { title: 'Decide a first approval', hint: 'Say yes or no to the first thing it asks.', minutes: 1 },
  phone: { title: 'Add your phone', hint: 'Hear about what needs you, wherever you are.', minutes: 2 },
  invite: { title: 'Invite someone to approve', hint: 'Someone else can say yes too.', minutes: 1 },
};

/** All of setup, in minutes, as the cloud estimates it. */
export const TOTAL_MINUTES = Object.values(ITEM).reduce((sum, entry) => sum + entry.minutes, 0);

/** Minutes still to go. */
export const minutesLeft = (setup: Setup | null) =>
  setup ? setup.items.filter((entry) => !entry.done).reduce((sum, entry) => sum + entry.minutes, 0) : TOTAL_MINUTES;

function item(key: SetupItemKey, done: boolean, hint?: string): GettingStartedItem {
  return { key, done, ...ITEM[key], ...(hint ? { hint } : {}) };
}

interface Sources {
  goal: GoalKey | null;
  workspaces: Pick<Workspace, 'lanes'>[] | undefined;
  nodes: { id: string; state?: string }[] | undefined;
  grants: { revoked: boolean }[] | undefined;
  decided: number | undefined;
  phones: number | undefined;
  invited: number | undefined;
  people: number | undefined;
}

/**
 * The cloud's list, worked out here from what the panel can read, for a cloud that does not serve
 * it yet. The same seven lines, in the same order. Exported for the tests.
 */
export function workOutItems({ goal, workspaces, nodes, grants, decided, phones, invited, people }: Sources): GettingStartedItem[] {
  const node = nodes?.[0];
  const signedIn = (workspaces ?? []).some((workspace) => workspace.lanes.some((lane) => lane.kind === 'browser' && lane.logged_in_hint === 'ok'));
  return [
    item('goal', goal !== null),
    item('computer', Boolean(node), node ? `${computerName(node.id)} is ${node.state === 'down' ? 'added, but not connected right now' : 'connected'}.` : undefined),
    item('account', signedIn),
    // The panel cannot see an AI's first call, so a connection it can read stands in for it.
    item('ai', Boolean(grants?.some((grant) => !grant.revoked))),
    item('approval', (decided ?? 0) > 0),
    item('phone', (phones ?? 0) > 0),
    item('invite', (invited ?? 0) > 0 || (people ?? 0) > 1),
  ];
}

export interface SetupState {
  setup: Setup | null;
  /** True while the cloud does not serve Getting started and the list is worked out here. */
  local: boolean;
}

/**
 * The setup for an org. `enabled` is false for people who cannot set things up, so they never
 * ask for it.
 */
export function useSetup(org: string, enabled = true): SetupState {
  const remote = useQuery({ queryKey: keys.setup(org), queryFn: () => served(setupApi.gettingStarted(org)), enabled, retry: false, staleTime: 0 });
  const local = enabled && isNotServed(remote.error);
  const saved = useSaved(org);
  const on = { enabled: local };
  const workspaces = useQuery({ queryKey: keys.workspaces(org), queryFn: () => fleet.workspaces(org), ...on });
  const nodes = useQuery({ queryKey: keys.nodes(org), queryFn: () => served(fleet.nodes(org)), ...on });
  const grants = useQuery({ queryKey: keys.grants(org), queryFn: () => served(fleet.grants(org)), ...on });
  const phones = useQuery({ queryKey: keys.pushDevices(org), queryFn: () => fleet.pushDevices(org), ...on });
  const invites = useQuery({ queryKey: keys.invites(org), queryFn: () => orgs.invites(org), ...on });
  const members = useQuery({ queryKey: keys.members(org), queryFn: () => orgs.members(org), ...on });
  const decided = useQuery({
    queryKey: [...keys.decidedApprovals(org), 'any'],
    queryFn: async () => {
      const [approved, denied] = await Promise.all([served(fleet.approvals(org, 'approved', 1)), served(fleet.approvals(org, 'denied', 1))]);
      return approved.length + denied.length;
    },
    ...on,
  });

  const setup = useMemo<Setup | null>(() => {
    if (remote.data) return { ...saved, goal: remote.data.goal?.key ?? saved.goal, items: remote.data.items };
    if (!local) return null;
    return {
      ...saved,
      items: workOutItems({
        goal: saved.goal,
        workspaces: workspaces.data,
        nodes: nodes.data,
        grants: grants.data,
        decided: decided.data,
        phones: phones.data?.length,
        invited: invites.data?.filter((invite) => invite.state !== 'revoked').length,
        people: members.data?.length,
      }),
    };
  }, [remote.data, local, saved, workspaces.data, nodes.data, grants.data, decided.data, phones.data, invites.data, members.data]);

  return { setup, local };
}

/** Asks Getting started again, after something on this screen made a line true. */
export function useRefreshSetup(org: string) {
  const client = useQueryClient();
  return () => void client.invalidateQueries({ queryKey: keys.setup(org) });
}

/** "Claude Code", once a connection was made for it here. */
export const agentName = (setup: Setup | null) => (setup?.agent ? AGENT_NAMES[setup.agent] : null);

export const itemOf = (setup: Setup | null, key: SetupItemKey) => setup?.items.find((entry) => entry.key === key) ?? null;
