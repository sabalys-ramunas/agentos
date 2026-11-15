import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback } from 'react';
import { fleet } from '../client';
import { isContentChanged, staysPending } from '@/lib/errors';
import { ApiError, isNotServed, served } from '../http';
import { keys } from '../keys';
import { dropCode, patchDecided } from '../live';
import type {
  Approval,
  BatchResult,
  DecisionReport,
  RefusedDecision,
  Question,
  Status,
} from '../types';
import { pollUnlessNotServed } from './shared';

// Needs you

export const useApprovals = (org: string) =>
  useQuery({ queryKey: keys.approvals(org), queryFn: () => served(fleet.approvals(org)) });

interface Decision {
  /** What was on screen when the decision was made: the id and the hash of that content. */
  items: Pick<Approval, 'id' | 'content_hash' | 'summary'>[];
  decision: 'approve' | 'deny';
}

export interface DecisionOutcome {
  decided: string[];
  refused: RefusedDecision[];
  /** The decided approvals as the API returned them, with `undo_until` and the first outcome. */
  approvals?: Approval[];
}

/**
 * A batch always answers 200, so success says nothing about any one item. Each is read on its
 * own, and an item with no clear `ok: true` counts as not decided: it must never look decided.
 */
function readBatch(items: Decision['items'], results: BatchResult[]): DecisionOutcome {
  const byId = new Map(results.map((result) => [result.id, result]));
  const outcome: DecisionOutcome = { decided: [], refused: [], approvals: [] };
  for (const item of items) {
    const result = byId.get(item.id);
    if (result?.ok === true) {
      outcome.decided.push(item.id);
      outcome.approvals?.push(result);
      continue;
    }
    outcome.refused.push({
      id: item.id,
      summary: item.summary,
      code: result?.error?.code ?? 'no_answer',
      message: result?.error?.message ?? 'No answer came back for this item, so treat it as not decided.',
    });
  }
  return outcome;
}

export function useDecide(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async ({ items, decision }: Decision): Promise<DecisionOutcome> => {
      if (items.length > 1) return readBatch(items, await fleet.decideBatch(org, items, decision));
      const [item] = items;
      if (!item) return { decided: [], refused: [] };
      const decided = await (decision === 'approve' ? fleet.approve(org, item.id, item.content_hash) : fleet.deny(org, item.id, item.content_hash));
      return { decided: [item.id], refused: [], approvals: decided && typeof decided === 'object' ? [decided] : [] };
    },
    onMutate: async ({ items }) => {
      const ids = items.map((item) => item.id);
      await client.cancelQueries({ queryKey: keys.approvals(org) });
      const approvals = client.getQueryData<Approval[]>(keys.approvals(org));
      const status = client.getQueryData<Status>(keys.status(org));
      client.setQueryData<Approval[]>(keys.approvals(org), (current) =>
        current?.filter((item) => !ids.includes(item.id)),
      );
      client.setQueryData<Status>(keys.status(org), (current) =>
        current ? { ...current, needs_you: Math.max(0, current.needs_you - ids.length) } : current,
      );
      return { approvals, status };
    },
    // Here and not in the caller: this runs even if the person has already left the screen.
    onSuccess: ({ decided, refused, approvals: done = [] }, { decision }, context) => {
      if (done.length) {
        const ids = new Set(done.map((item) => item.id));
        client.setQueryData<Approval[]>(keys.decidedApprovals(org), (current) => (current ? [...done, ...current.filter((item) => !ids.has(item.id))] : current));
        for (const item of done) client.setQueryData(keys.approval(org, item.id), item);
      }
      if (refused.length === 0) return;
      const waiting = refused.filter((item) => staysPending(item.code));
      const back = new Set(waiting.map((item) => item.id));
      client.setQueryData<Approval[]>(keys.approvals(org), (current) => [
        ...(context?.approvals ?? []).filter((item) => back.has(item.id)),
        ...(current ?? []).filter((item) => !back.has(item.id)),
      ]);
      client.setQueryData<Status>(keys.status(org), (current) =>
        current ? { ...current, needs_you: current.needs_you + waiting.length } : current,
      );
      client.setQueryData<DecisionReport[]>(keys.decisionReports(org), (current = []) => [
        { id: `${Date.now()}:${refused[0]?.id ?? ''}`, decision, decided: decided.length, refused },
        ...current,
      ]);
      void client.invalidateQueries({ queryKey: keys.approvals(org) });
      void client.invalidateQueries({ queryKey: keys.status(org) });
    },
    onError: (error, _vars, context) => {
      client.setQueryData(keys.approvals(org), context?.approvals);
      client.setQueryData(keys.status(org), context?.status);
      // Changed content means the list on screen is out of date, so it is read again.
      if (isContentChanged(error)) void client.invalidateQueries({ queryKey: keys.approvals(org) });
    },
    onSettled: () => client.invalidateQueries({ queryKey: keys.session }),
  });
}

/** One approval with its outcome. Asked again while the outcome is still on its way. */
export function useApproval(org: string, id: string | null, follow = false) {
  const client = useQueryClient();
  return useQuery({
    queryKey: keys.approval(org, id ?? ''),
    queryFn: async () => {
      const approval = await fleet.approval(org, id ?? '');
      // What it says now belongs in "You decided today" and the failed list too, even without the live feed.
      patchDecided(client, org, approval, false);
      return approval;
    },
    enabled: id !== null,
    refetchInterval: (query) => {
      if (!follow) return false;
      const state = query.state.data?.outcome?.state;
      return !state || state === 'pending' ? 1_500 : false;
    },
  });
}

/** Decided approvals whose outcome did not work and nobody has cleared: they are back in Needs you. */
export const useFailedApprovals = (org: string) =>
  useQuery({ queryKey: keys.failedApprovals(org), queryFn: () => served(fleet.failedApprovals(org)) });

/** What was decided lately, newest first, yes and no together. */
export const useDecidedApprovals = (org: string) =>
  useQuery({
    queryKey: keys.decidedApprovals(org),
    queryFn: async () => {
      const [approved, denied] = await Promise.all([served(fleet.approvals(org, 'approved', 20)), served(fleet.approvals(org, 'denied', 20))]);
      return [...approved, ...denied].sort((a, b) => (b.decided_at ?? '').localeCompare(a.decided_at ?? ''));
    },
  });

/** Takes a decision back inside its undo window. The approval is pending again, back in the list. */
/** Runs that stopped halfway and wait for the owner to say whether the step went through. Owner only; not served reads as none. */
export const useRunsWaiting = (org: string, enabled = true) =>
  useQuery({
    queryKey: keys.runsWaiting(org),
    queryFn: async () => {
      try {
        return (await fleet.runs(org, 'partial', 50)).filter((run) => run.decision_needed);
      } catch (error) {
        if (isNotServed(error)) return [];
        throw error;
      }
    },
    enabled,
    retry: false,
    refetchInterval: 30_000,
  });

/** A run that stopped short: the owner's answer, and the lists it moves (what waits, what did not go through). */
export function useDecideRun(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: 'retry' | 'skip' | 'fail' }) => fleet.decideRun(org, id, decision),
    onSettled: () => {
      void client.invalidateQueries({ queryKey: keys.runsWaiting(org) });
      void client.invalidateQueries({ queryKey: keys.failedApprovals(org) });
      void client.invalidateQueries({ queryKey: keys.status(org) });
      void client.invalidateQueries({ queryKey: keys.activityAll(org) });
    },
  });
}

export function useUndo(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (ids: string[]) => Promise.all(ids.map((id) => fleet.undo(org, id))),
    meta: { inline: true },
    onSuccess: (approvals) => {
      const back = new Set(approvals.map((item) => item.id));
      client.setQueryData<Approval[]>(keys.approvals(org), (current) =>
        current ? [...approvals.filter((item) => item.state === 'pending'), ...current.filter((item) => !back.has(item.id))] : current,
      );
      client.setQueryData<Approval[]>(keys.decidedApprovals(org), (current) => current?.filter((item) => !back.has(item.id)));
      for (const item of approvals) client.setQueryData(keys.approval(org, item.id), item);
    },
    onSettled: () => {
      void client.invalidateQueries({ queryKey: keys.approvals(org) });
      void client.invalidateQueries({ queryKey: keys.status(org) });
    },
  });
}

/** "Got it" on something that did not work: it leaves Needs you. */
export function useClearOutcome(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => fleet.clearOutcome(org, id),
    onMutate: (id) => {
      const before = client.getQueryData<Approval[]>(keys.failedApprovals(org));
      client.setQueryData<Approval[]>(keys.failedApprovals(org), (current) => current?.filter((item) => item.id !== id));
      return { before };
    },
    onError: (_error, _id, context) => client.setQueryData(keys.failedApprovals(org), context?.before),
    onSettled: () => {
      void client.invalidateQueries({ queryKey: keys.failedApprovals(org) });
      void client.invalidateQueries({ queryKey: keys.status(org) });
    },
  });
}

/** Refused decisions, held until dismissed. They outlive the screen, as they have to. */
export function useDecisionReports(org: string) {
  const client = useQueryClient();
  const { data: reports } = useQuery<DecisionReport[]>({
    queryKey: keys.decisionReports(org),
    queryFn: () => [],
    initialData: [],
    staleTime: Infinity,
    gcTime: Infinity,
  });
  const dismiss = (id: string) =>
    client.setQueryData<DecisionReport[]>(keys.decisionReports(org), (current = []) => current.filter((report) => report.id !== id));
  return { reports, dismiss };
}

// Questions

export const useQuestions = (org: string) =>
  useQuery({ queryKey: keys.questions(org), queryFn: () => served(fleet.questions(org)) });

/** Optimistic like a decision, so the queue keeps moving, and restored if it is refused. */
export function useAnswerQuestion(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: ({ id, answer }: { id: string; answer: string }) => fleet.answerQuestion(org, id, answer),
    onMutate: async ({ id }) => {
      await client.cancelQueries({ queryKey: keys.questions(org) });
      const before = client.getQueryData<Question[]>(keys.questions(org));
      client.setQueryData<Question[]>(keys.questions(org), (current) => current?.filter((item) => item.id !== id));
      client.setQueryData<Status>(keys.status(org), (current) =>
        current ? { ...current, needs_you: Math.max(0, current.needs_you - 1) } : current,
      );
      return { before };
    },
    onError: (_error, _vars, context) => {
      client.setQueryData(keys.questions(org), context?.before);
      void client.invalidateQueries({ queryKey: keys.status(org) });
    },
  });
}

// Repository cards: a question an agent's environment_open raised for a private repository. Owner only, in person.

/** The card leaves the list, and the count with it, once its repository is connected or declined. */
function dropQuestion(client: ReturnType<typeof useQueryClient>, org: string, id: string) {
  client.setQueryData<Question[]>(keys.questions(org), (current) => current?.filter((item) => item.id !== id));
  void client.invalidateQueries({ queryKey: keys.status(org) });
}

/** A connected card stays on screen to say so, and leaves the list when the person is done with it. */
export function useDropQuestion(org: string) {
  const client = useQueryClient();
  return useCallback((id: string) => dropQuestion(client, org, id), [client, org]);
}

/**
 * The link to the computer's own page for a card's token. The token is typed there, so it never reaches the app. A
 * card that closed meanwhile (404) leaves; any other failure keeps it, to try again.
 */
export function useRepoTokenForm(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (id: string) => fleet.repoTokenForm(org, id),
    onError: (error, id) => {
      if (error instanceof ApiError && error.status === 404) dropQuestion(client, org, id);
    },
  });
}

export const useRepoDeployKey = (org: string) =>
  useMutation({ meta: { inline: true }, mutationFn: (id: string) => fleet.repoDeployKey(org, id) });

export function useCheckRepo(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (id: string) => fleet.checkRepo(org, id),
    onError: (error, id) => {
      if (error instanceof ApiError && error.status === 404) dropQuestion(client, org, id);
    },
  });
}

export function useDeclineRepo(org: string) {
  const client = useQueryClient();
  return useMutation({ meta: { inline: true }, mutationFn: (id: string) => fleet.declineRepo(org, id), onSuccess: (_done, id) => dropQuestion(client, org, id) });
}

// Code cards. Owner only. The code is never a mutation's variables, so no cache keeps it.

export const useCodes = (org: string, enabled: boolean) =>
  useQuery({ queryKey: keys.codes(org), queryFn: () => served(fleet.codes(org)), enabled, refetchInterval: pollUnlessNotServed(60_000) });

/** Sends the one code to the node. A plain call, not a mutation, so nothing in the query or mutation cache holds it. */
export function useEnterCode(org: string) {
  const client = useQueryClient();
  return useCallback(
    async (id: string, code: string) => {
      try {
        await fleet.enterCode(org, id, code);
      } catch (error) {
        // Closed or expired: the card is gone, so it leaves too. Any other failure keeps it, to try again.
        if (error instanceof ApiError && error.status === 404) dropCode(client, org, id);
        throw error;
      }
      dropCode(client, org, id);
    },
    [client, org],
  );
}

export function useDeclineCode(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (id: string) => fleet.declineCode(org, id),
    onSuccess: (_card, id) => dropCode(client, org, id),
    // A request that closed in the meantime has no card left to answer.
    onError: (error, id) => {
      if (error instanceof ApiError && error.status === 404) dropCode(client, org, id);
    },
  });
}

/** A Review link's approval. Not retried: a 410 is the answer, not a hiccup. */
export const useReview = (org: string, id: string, e: string, s: string) =>
  useQuery({ queryKey: keys.review(org, id, e, s), queryFn: () => fleet.review(org, id, e, s), retry: false });
