/**
 * What Needs you lists: every read it is built from, narrowed to the business picked in the header,
 * in the order J and K walk it.
 */
import { useCallback, useMemo } from 'react';
import {
  useAccess,
  useApprovals,
  useBrandProposals,
  useCodes,
  useDecidedApprovals,
  useFailedApprovals,
  useLoginQueue,
  useQuestions,
  useRunsWaiting,
  useWorkspaces,
} from '@/api/hooks';
import type { Workspace } from '@/api/types';
import { useActiveWorkspace } from '@/lib/active-workspace';
import { needsHand } from '@/lib/lanes';
import { useRowMotion } from '@/lib/row-motion';
import { inWorkspace, useNeedsYouCount } from './count';
import { groupByStake } from './queue-rows';

export function useQueue(org: string, { hovering, decided }: { hovering: boolean; decided: ReadonlySet<string> }) {
  const approvals = useApprovals(org);
  const failedQuery = useFailedApprovals(org);
  const decidedQuery = useDecidedApprovals(org);
  const workspaces = useWorkspaces(org).data ?? [];
  const [active] = useActiveWorkspace(org);
  const access = useAccess(org);
  const owner = access.role === 'owner';
  const questionsQuery = useQuestions(org);
  const loginQueue = useLoginQueue(org);
  // Login codes are the owner's alone: no other role is asked for one, so none is read.
  const codesQuery = useCodes(org, owner);
  const stoppedQuery = useRunsWaiting(org, owner);
  // The same count as the rail, the bar and Home (count.ts), with its parts.
  const count = useNeedsYouCount(org, active);
  // Brand changes are the owner's to decide; anyone else's read is an empty list.
  const brandQuery = useBrandProposals(org, owner);

  const inScope = useCallback((lane: string | null | undefined) => inWorkspace(active)(lane), [active]);
  const visible = useMemo(() => (approvals.data ?? []).filter((item) => item.kind !== 'question' && inScope(item.lane)), [approvals.data, inScope]);
  const questions = useMemo(() => (questionsQuery.data ?? []).filter((item) => inScope(item.lane)), [questionsQuery.data, inScope]);
  const openCodes = useMemo(() => (codesQuery.data ?? []).filter((item) => item.state === 'open' && inScope(item.lane)), [codesQuery.data, inScope]);
  const stopped = useMemo(() => (stoppedQuery.data ?? []).filter((run) => inScope(run.lane)), [stoppedQuery.data, inScope]);
  // A run that stopped halfway has its own three answers, so it is not also listed as a failure.
  const stoppedApprovals = useMemo(() => new Set(stopped.map((run) => run.approval_id).filter((id): id is string => Boolean(id))), [stopped]);
  const failed = useMemo(
    () => (failedQuery.data ?? []).filter((item) => inScope(item.lane) && !stoppedApprovals.has(item.id)),
    [failedQuery.data, inScope, stoppedApprovals],
  );
  const flat = useMemo(() => groupByStake(visible).flatMap((group) => group.items), [visible]);
  const brands = useMemo(
    () => (brandQuery.data ?? []).filter((item) => (!active || item.workspace === active) && !decided.has(item.id)),
    [brandQuery.data, active, decided],
  );
  const motion = useRowMotion(flat, { hold: hovering, decided });
  const questionMotion = useRowMotion(questions, { hold: hovering, decided });
  const codeMotion = useRowMotion(openCodes, { hold: hovering, decided });
  const groups = useMemo(() => groupByStake(motion.rows), [motion.rows]);
  const waiting = motion.waiting + questionMotion.waiting + codeMotion.waiting;

  const order = useMemo(
    () => [...flat.map((item) => item.id), ...brands.map((item) => item.id), ...questions.map((item) => item.id), ...openCodes.map((item) => item.id)],
    [flat, brands, questions, openCodes],
  );
  const hands = workspaces
    .filter((item) => !active || item.id === active)
    .flatMap((item) => item.lanes)
    .filter(needsHand);

  const workspaceOf = (lane: string | null | undefined): Workspace | undefined => workspaces.find((item) => item.id === (lane ?? '').split('/')[0]);
  const businessOf = (lane: string | null | undefined) => workspaceOf(lane)?.name ?? (lane ?? '').split('/')[0] ?? '';
  const colorOf = (lane: string | null | undefined) => workspaceOf(lane)?.color ?? null;

  const toDecide = flat.length + brands.length + questions.length + openCodes.length;
  const decidedHere = (decidedQuery.data ?? []).filter((item) => inScope(item.lane));
  // The oldest thing in the queue, for the queue's head: "the oldest for 2 h".
  const oldest = [...flat.map((item) => item.created_at), ...brands.map((item) => item.created_at), ...questions.map((item) => item.created_at), ...openCodes.map((item) => item.created_at)].sort()[0] ?? null;

  return {
    access,
    owner,
    approvals,
    loginQueue,
    count,
    questions,
    openCodes,
    stopped,
    failed,
    flat,
    brands,
    groups,
    questionRows: questionMotion.rows,
    codeRows: codeMotion.rows,
    waiting,
    order,
    hands,
    businessOf,
    colorOf,
    toDecide,
    decidedHere,
    oldest,
  };
}

export type Queue = ReturnType<typeof useQueue>;
