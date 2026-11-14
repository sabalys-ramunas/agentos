/**
 * The one "needs you" count, with its parts: what to decide (approvals and questions), what did not go
 * through after a yes, runs that stopped halfway, login codes, and accounts that need a
 * hand (an agent's login request among them), and brand changes for the owner. Needs you's header and All clear, the rail's badge and the
 * status pill all read it, so the numbers agree. The node's `/status` (`needs_you`, routes_core.py) counts
 * the same parts, for anything that reads the status alone.
 *
 * `workspace` narrows every part to one business, as the workspace switcher does.
 */
import { useAccess, useApprovals, useBrandProposals, useCodes, useFailedApprovals, useQuestions, useRunsWaiting, useWorkspaces } from '@/api/hooks';
import type { Approval, BrandProposal, CodeRequest, Question, Role, Run, Workspace } from '@/api/types';
import { plural } from '@/lib/format';
import { needsHand } from '@/lib/lanes';

/** Who is shown login codes: the owner alone. No other role is asked for one, so none is read. */
export const canSeeCodes = (role: Role) => role === 'owner';

export interface NeedsYouParts {
  /** Approvals and questions, waiting for a yes or a no or an answer. */
  decide: number;
  /** Approved actions whose run did not go through, and nobody has said "Got it" yet. */
  failed: number;
  /** Runs that stopped halfway: the owner says whether the step went through. */
  stopped: number;
  /** Login codes a site asked for. */
  codes: number;
  /** Accounts that need a hand, an agent's login request among them. */
  hands: number;
  /** Brand changes an agent proposed, for the owner's yes. */
  brand: number;
}

export interface NeedsYouCount {
  /** Every list it depends on has answered: only then may anything say nothing is waiting. */
  loaded: boolean;
  /** One of the lists did not load. */
  failedToLoad: boolean;
  total: number;
  parts: NeedsYouParts;
}

/** Whether a lane (or a routine's lane) is inside the chosen business. No business means all of them. */
export const inWorkspace = (workspace: string | null | undefined) => (lane: string | null | undefined) =>
  !workspace || (lane ?? '').startsWith(`${workspace}/`);

/** The lists the count is made from, as they came back. `undefined` is a list not read (yet, or not for this role). */
export interface CountLists {
  approvals?: Approval[];
  questions?: Question[];
  failed?: Approval[];
  stopped?: Run[];
  codes?: CodeRequest[];
  workspaces?: Workspace[];
  brand?: BrandProposal[];
}

/**
 * The parts of the count, from the lists, for one business (or all, with no business). The same rule the node
 * applies in `/status` (routes_core.py): an approval that a stopped run also is counts once, as a stopped run.
 */
export function countParts(lists: CountLists, workspace: string | null | undefined, now = Date.now()): NeedsYouParts {
  const mine = inWorkspace(workspace);
  const stopped = (lists.stopped ?? []).filter((run) => mine(run.lane));
  const stoppedApprovals = new Set(stopped.map((run) => run.approval_id).filter((id): id is string => Boolean(id)));
  return {
    decide: (lists.approvals ?? []).filter((item) => item.kind !== 'question' && mine(item.lane)).length + (lists.questions ?? []).filter((item) => mine(item.lane)).length,
    failed: (lists.failed ?? []).filter((item) => mine(item.lane) && !stoppedApprovals.has(item.id)).length,
    stopped: stopped.length,
    codes: (lists.codes ?? []).filter((item) => item.state === 'open' && Date.parse(item.expires_at) > now && mine(item.lane)).length,
    hands: (lists.workspaces ?? [])
      .filter((item) => !workspace || item.id === workspace)
      .flatMap((item) => item.lanes)
      .filter(needsHand).length,
    brand: (lists.brand ?? []).filter((item) => !workspace || item.workspace === workspace).length,
  };
}

export function useNeedsYouCount(org: string, workspace: string | null | undefined): NeedsYouCount {
  const access = useAccess(org);
  const owner = access.role === 'owner';
  const approvals = useApprovals(org);
  const questions = useQuestions(org);
  const failed = useFailedApprovals(org);
  const workspaces = useWorkspaces(org);
  const codes = useCodes(org, canSeeCodes(access.role));
  const runs = useRunsWaiting(org, owner);
  // Only the owner decides a brand change, so only the owner's count reads them.
  const brand = useBrandProposals(org, owner);

  // A query that is off for this role is not one it depends on.
  const needed = [
    approvals,
    questions,
    failed,
    workspaces,
    ...(canSeeCodes(access.role) ? [codes] : []),
    ...(owner ? [runs, brand] : []),
  ];
  const loaded = needed.every((query) => query.isSuccess);
  const failedToLoad = needed.some((query) => query.isError);

  const lists: CountLists = {
    approvals: approvals.data,
    questions: questions.data,
    failed: failed.data,
    stopped: runs.data,
    codes: codes.data,
    workspaces: workspaces.data,
    brand: brand.data,
  };
  const parts = countParts(lists, workspace);
  const total = Object.values(parts).reduce((sum, value) => sum + value, 0);
  return { loaded, failedToLoad, total, parts };
}

/**
 * "2 to decide, 1 that did not go through, 1 account that needs a hand": the parts that are not zero. Login codes
 * are something to decide (a person types the code), so they are said in "to decide".
 */
export function partsWords(parts: NeedsYouParts): string[] {
  const toDecide = parts.decide + parts.codes;
  return [
    toDecide ? `${toDecide} to decide` : null,
    parts.failed ? `${parts.failed} that did not go through` : null,
    parts.stopped ? `${plural(parts.stopped, 'run')} stopped halfway` : null,
    parts.brand ? `${plural(parts.brand, 'brand change')} to approve` : null,
    parts.hands ? `${plural(parts.hands, 'account')} that need${parts.hands === 1 ? 's' : ''} a hand` : null,
  ].filter((part): part is string => part !== null);
}

/**
 * The number the rail, the tab bar and the bell show: the same count as Needs you's header, once every list
 * has answered. Until then (or when a list failed) it is the node's own `needs_you`, which counts the same parts.
 */
export function useNeedsYouTotal(org: string, status: { needs_you?: number } | undefined): number {
  const count = useNeedsYouCount(org, null);
  return count.loaded ? count.total : (status?.needs_you ?? 0);
}
