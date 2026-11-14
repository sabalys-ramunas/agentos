/** TanStack Query hooks. Screens import from here and never touch fetch or the mock. */
import { keepPreviousData, queryOptions, useInfiniteQuery, useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useState } from 'react';
import { accountFiles, auth, brainTasks, brand, connectors, dayPlan, engage, environments, fleet, invites, jobs, memory, orgs, overrides, proof, studio, surfaces, vms } from './client';
import type { ActivityFilters } from './client';
import { flowAccountSet, flowSet } from '@/lib/ask-first';
import { createPasskey } from '@/lib/webauthn';
import { isContentChanged, staysPending } from '@/lib/errors';
import { can } from '@/lib/permissions';
import type { Action } from '@/lib/permissions';
import { ApiError, isNotServed, reported, served, setCsrfToken } from './http';
import { keys } from './keys';
import { dropCode, patchDecided } from './live';
import { getPasskey } from '@/lib/webauthn';
import type {
  AccountFiles,
  EnvDetail,
  EnvState,
  EnvSummary,
  JobFollow,
  StudioMissing,
  DayPlan,
  PlanStatus,
  MemoryDetail,
  MemoryList,
  MemoryWorkspaces,
  EngageItems,
  EngageKind,
  NewSurface,
  RoutineDefinition,
  SiteAction,
  MailFormLinkBody,
  MailList,
  SiteApi,
  SiteApiDetail,
  SiteKeyInput,
  ActivityRange,
  HistoryShow,
  Approval,
  FreezeScope,
  WorkerAccount,
  WorkerJob,
  WorkerLimits,
  WorkerOverview,
  ConfirmationOverlay,
  ConfirmationSetting,
  ConfirmationView,
  BatchResult,
  DecisionReport,
  RefusedDecision,
  ControlRequest,
  Flow,
  Lane,
  LaneAskFirst,
  LaneSites,
  Limits,
  Pace,
  Member,
  NewGrant,
  NodeRange,
  NewStandingApproval,
  NotificationSettings,
  PushDevice,
  FormKind,
  Webhook,
  WebhookEvent,
  TopologyWindow,
  Question,
  Readiness,
  Role,
  NodeSchedule,
  Schedule,
  Session,
  StandingApproval,
  Status,
  Workspace,
  BrainTaskStatus,
  ProofShareInput,
} from './types';

// Session and auth

export const sessionQuery = queryOptions({
  queryKey: keys.session,
  queryFn: async (): Promise<Session> => {
    const session = await auth.session();
    setCsrfToken(session.csrf);
    return session;
  },
  retry: false,
  staleTime: 60_000,
  // A role can change from another room. Coming back to the tab picks that up.
  refetchOnWindowFocus: true,
});

export const useSession = () => useQuery(sessionQuery);

/** What this person may do in this org, for showing and hiding controls. */
export function useAccess(org: string) {
  const role = useSession().data?.orgs.find((item) => item.slug === org)?.role ?? 'viewer';
  return { role, can: (action: Action) => can(role, action) };
}

export const useProviders = () =>
  useQuery({ queryKey: keys.providers, queryFn: auth.providers, staleTime: Infinity });
export const usePasskeys = () => useQuery({ queryKey: keys.passkeys, queryFn: auth.passkeys });
export const useDevices = () => useQuery({ queryKey: keys.devices, queryFn: auth.devices });
export const useInvitePreview = (token: string) =>
  useQuery({ queryKey: keys.invite(token), queryFn: () => invites.preview(token), retry: false });

export function useSendMagicLink() {
  return useMutation({ mutationFn: auth.sendMagicLink, meta: { inline: true } });
}

export function useVerifyMagicLink() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: auth.verifyMagicLink,
    meta: { inline: true },
    onSuccess: (session) => {
      setCsrfToken(session.csrf);
      client.setQueryData(keys.session, session);
    },
  });
}

export function useSignOut() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: auth.signOut,
    onSuccess: () => {
      setCsrfToken(null);
      client.clear();
    },
  });
}

export function useUpdateMe() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: auth.updateMe,
    onSuccess: () => client.invalidateQueries({ queryKey: keys.session }),
  });
}

export function useRevokeDevice() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: auth.revokeDevice,
    onSuccess: () => client.invalidateQueries({ queryKey: keys.devices }),
  });
}

export function useDeletePasskey() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: auth.deletePasskey,
    onSuccess: () => client.invalidateQueries({ queryKey: keys.passkeys }),
  });
}

export function useAcceptInvite() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: invites.accept,
    meta: { inline: true },
    // Returned, so the caller only navigates once the new org is in the session.
    onSuccess: () => client.invalidateQueries({ queryKey: keys.session }),
  });
}

/** A new person's one click on an invite: account, membership and session together. */
export function useJoinInvite() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ token, name }: { token: string; name?: string }) => invites.join(token, { aup_accepted: true, name }),
    meta: { inline: true },
    onSuccess: (session) => {
      setCsrfToken(session.csrf);
      client.setQueryData(keys.session, session);
    },
  });
}

/** A computer's invite: a passkey made on this device, then the account, membership and session together. */
export function useJoinInviteWithPasskey() {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: async ({ token }: { token: string }) => {
      const options = await invites.passkeyOptions(token);
      const credential = await createPasskey(options);
      return invites.passkeyJoin(token, credential, Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC');
    },
    onSuccess: (session) => {
      setCsrfToken(session.csrf);
      client.setQueryData(keys.session, session);
    },
  });
}

// Org

export const useOrg = (org: string) =>
  useQuery({ queryKey: keys.org(org), queryFn: () => orgs.get(org) });
export const useStatus = (org: string) =>
  useQuery({ queryKey: keys.status(org), queryFn: () => orgs.status(org) });
export const useMembers = (org: string) =>
  useQuery({ queryKey: keys.members(org), queryFn: () => orgs.members(org) });
/** One member, with their devices. Read only when the screen shows them (an admin). */
export const useMember = (org: string, id: string, enabled = true) =>
  useQuery({ queryKey: keys.member(org, id), queryFn: () => orgs.member(org, id), enabled });
/** Ending a device signs that person out there. The members list and their devices both change. */
export function useEndMemberDevice(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ member, device }: { member: string; device: string }) => orgs.endMemberDevice(org, member, device),
    meta: { inline: true },
    onSuccess: (_result, { member }) => {
      void client.invalidateQueries({ queryKey: keys.member(org, member) });
      void client.invalidateQueries({ queryKey: keys.members(org) });
    },
  });
}
export const useInvites = (org: string) =>
  useQuery({ queryKey: keys.invites(org), queryFn: () => orgs.invites(org) });

export function useAudit(org: string) {
  return useInfiniteQuery({
    queryKey: keys.audit(org),
    queryFn: ({ pageParam }) => orgs.audit(org, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_before ?? undefined,
  });
}

export function useCreateOrg() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: orgs.create,
    meta: { inline: true },
    onSuccess: () => client.invalidateQueries({ queryKey: keys.session }),
  });
}

export function useUpdateOrg(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (body: { name?: string; slug?: string }) => orgs.update(org, body),
    onSuccess: (_updated, body) => {
      // After a change of address the old key answers 404, so the caller moves first.
      if (body.slug) return;
      void client.invalidateQueries({ queryKey: keys.org(org) });
      void client.invalidateQueries({ queryKey: keys.session });
    },
  });
}

export function useDeleteOrg(org: string) {
  return useMutation({ mutationFn: (confirm: string) => orgs.remove(org, confirm) });
}

export function useUpdateMember(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...body }: { id: string; role?: Role; workspaces?: string[] | null; workspace?: string }) =>
      orgs.updateMember(org, id, body),
    onMutate: async ({ id, ...change }) => {
      await client.cancelQueries({ queryKey: keys.members(org) });
      const before = client.getQueryData<Member[]>(keys.members(org));
      client.setQueryData<Member[]>(keys.members(org), (current) =>
        current?.map((item) => {
          if (item.id !== id) return item;
          // A role for one business changes that business only. The person's own top role is what the node settles.
          if (change.workspace !== undefined) {
            const rest = (item.roles_by_workspace ?? []).filter((row) => row.workspace !== change.workspace);
            return change.role ? { ...item, roles_by_workspace: [...rest, { workspace: change.workspace, role: change.role }] } : item;
          }
          return { ...item, ...change };
        }),
      );
      return { before };
    },
    onError: (_error, _vars, context) => client.setQueryData(keys.members(org), context?.before),
    onSettled: () => client.invalidateQueries({ queryKey: keys.members(org) }),
  });
}

export function useRemoveMember(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => orgs.removeMember(org, id),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.members(org) }),
  });
}

export function useCreateInvite(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: { email: string; role: Role; workspaces?: string[] | null }) =>
      orgs.invite(org, body),
    meta: { inline: true },
    onSuccess: () => client.invalidateQueries({ queryKey: keys.invites(org) }),
  });
}

export function useRevokeInvite(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => orgs.revokeInvite(org, id),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.invites(org) }),
  });
}

// Workspaces and lanes

export const useWorkspaces = (org: string) =>
  useQuery({ queryKey: keys.workspaces(org), queryFn: () => fleet.workspaces(org) });

export const useLane = (org: string, ws: string, name: string) =>
  useQuery({ queryKey: keys.lane(org, `${ws}/${name}`), queryFn: () => fleet.lane(org, ws, name) });

/** Frees a lane id its node no longer reports, so another node may report it. Admin only. */
export function useReleaseLane(org: string, ws: string, name: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: () => fleet.releaseLane(org, ws, name),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.lane(org, `${ws}/${name}`) });
      void client.invalidateQueries({ queryKey: keys.workspaces(org) });
    },
  });
}

/** Reads a lane again, for when the relay says its control changed before the event arrived. */
export function useRefreshLane(org: string, ws: string, name: string) {
  const client = useQueryClient();
  return () => void client.invalidateQueries({ queryKey: keys.lane(org, `${ws}/${name}`) });
}

export function useLaneAction(org: string, ws: string, name: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (action: 'open' | 'close') =>
      action === 'open' ? fleet.openLane(org, ws, name) : fleet.closeLane(org, ws, name),
    onSuccess: (lane: Lane) => client.setQueryData(keys.lane(org, lane.id), lane),
  });
}

/** A preview with no side effects. Served routes only: it never creates anything. */
export function usePlanTemplate(org: string) {
  return useMutation({
    meta: { inline: true },
    mutationFn: (body: { template: string; workspaces: string[] }) => served(fleet.planTemplate(org, body)),
  });
}

/**
 * Creates the workspaces, then applies. Reports each step, and answers null when lanes are not
 * served here yet, so the caller can say what really happened.
 */
export function useApplyTemplate(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: async (body: { names: { id: string; name: string }[]; template: string; onStep?: (done: number, total: number) => void }) => {
      const existing = new Set((client.getQueryData<Workspace[]>(keys.workspaces(org)) ?? []).map((item) => item.id));
      const fresh = body.names.filter((item) => !existing.has(item.id));
      for (const [index, workspace] of fresh.entries()) {
        body.onStep?.(index, fresh.length);
        await fleet.createWorkspace(org, workspace);
      }
      body.onStep?.(fresh.length, fresh.length);
      return served(fleet.applyTemplate(org, { template: body.template, workspaces: body.names.map((item) => item.id) })).catch(
        (error: unknown) => {
          if (isNotServed(error)) return null;
          throw error;
        },
      );
    },
    onSettled: () => {
      void client.invalidateQueries({ queryKey: keys.workspaces(org) });
      void client.invalidateQueries({ queryKey: keys.status(org) });
      void client.invalidateQueries({ queryKey: keys.queue(org) });
    },
  });
}

export function useCreateWorkspace(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (body: { id?: string; name: string }) => fleet.createWorkspace(org, body),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.workspaces(org) });
      void client.invalidateQueries({ queryKey: keys.status(org) });
    },
  });
}

export function useUpdateWorkspace(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...body }: { id: string; name?: string }) => fleet.updateWorkspace(org, id, body),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.workspaces(org) }),
  });
}

export function useDeleteWorkspace(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => fleet.deleteWorkspace(org, id),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.workspaces(org) });
      void client.invalidateQueries({ queryKey: keys.status(org) });
    },
  });
}

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
      void client.invalidateQueries({ queryKey: ['org', org, 'activity'] });
    },
  });
}

/** Engagement plans an agent drafted that the owner has not approved yet, from the digest. Owner only; not served reads as none. */
export const useEnginePlans = (org: string, enabled = true) =>
  useQuery({
    queryKey: keys.enginePlans(org),
    queryFn: async () => {
      try {
        return (await engage.digest(org)).surfaces.filter((row) => row.program !== null && !row.program.approved);
      } catch (error) {
        if (isNotServed(error)) return [];
        throw error;
      }
    },
    enabled,
    retry: false,
    refetchInterval: 60_000,
  });

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

// An account's page (features/lane/account-*). Reads say when a node has no such route, so a screen can
// say so. Every change refreshes what it changes, and the live `meter` event does the same for the rest.

const accountPart = (org: string, lane: string, part: string) => [...keys.laneAccount(org, lane), part] as const;

function useAccountRead<T>(org: string, ws: string, name: string, part: string, load: () => Promise<T>, poll = 0) {
  return useQuery({
    queryKey: accountPart(org, `${ws}/${name}`, part),
    queryFn: () => served(load()),
    refetchInterval: poll || false,
  });
}

export const useLanePace = (org: string, ws: string, name: string) =>
  useAccountRead(org, ws, name, 'pace', () => fleet.lanePace(org, ws, name), 60_000);

/** The owner's change. Shows its own refusal, so it never raises a toast. */
export function useSetLanePace(org: string, ws: string, name: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (body: { pace: Pace | null } | { preset: true }) => fleet.setLanePace(org, ws, name, body),
    onSuccess: (pace) => {
      client.setQueryData(accountPart(org, `${ws}/${name}`, 'pace'), pace);
      void client.invalidateQueries({ queryKey: keys.meters(org) });
    },
  });
}

export const useWorkspaceZone = (org: string, ws: string) =>
  useQuery({ queryKey: keys.workspaceZone(org, ws), queryFn: () => served(fleet.workspaceZone(org, ws)) });

export function useSetWorkspaceZone(org: string, ws: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (timezone: string | null) => fleet.setWorkspaceZone(org, ws, timezone),
    onSuccess: () => void client.invalidateQueries({ queryKey: keys.account(org) }),
  });
}

export const useLaneMeter = (org: string, ws: string, name: string) =>
  useAccountRead(org, ws, name, 'meter', () => fleet.laneMeter(org, ws, name), 60_000);

export function useSetLaneLimits(org: string, ws: string, name: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (limits: Limits | null) => fleet.setLaneLimits(org, ws, name, limits),
    onSuccess: (meter) => {
      client.setQueryData(accountPart(org, `${ws}/${name}`, 'meter'), meter);
      void client.invalidateQueries({ queryKey: keys.meters(org) });
    },
  });
}

/** Every lane's and grant's meter, the same as the limits the node holds each one to. */
export const useMeters = (org: string) => useQuery({ queryKey: keys.meters(org), queryFn: () => served(fleet.meters(org)) });

/** Who holds the account and who waits for it. Polled, since it changes with every call. */
export const useLaneQueue = (org: string, ws: string, name: string) =>
  useAccountRead(org, ws, name, 'queue', () => fleet.laneQueue(org, ws, name), 5_000);

export const useLaneTaint = (org: string, ws: string, name: string) =>
  useAccountRead(org, ws, name, 'taint', () => fleet.laneTaint(org, ws, name));

/** The owner's clear of a grant's taint. A refusal says why, so it is inline and never a toast. */
export function useClearTaint(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (grant: string) => fleet.clearTaint(org, grant),
    onSuccess: () => void client.invalidateQueries({ queryKey: keys.account(org) }),
  });
}

/** The brief every account's health comes from. It runs no probe, so it is cheap to read. */
export const useLaneChecks = (org: string) =>
  useQuery({ queryKey: keys.laneChecks(org), queryFn: () => served(fleet.laneChecks(org)), staleTime: 60_000 });

/** The full checklist, run now. Only read when the brief says something is wrong, so it stays off otherwise. */
export const useLaneCheck = (org: string, ws: string, name: string, enabled = true) =>
  useQuery({
    queryKey: accountPart(org, `${ws}/${name}`, 'check'),
    queryFn: () => served(fleet.laneCheck(org, ws, name)),
    enabled,
  });

export const useLaneKeepalive = (org: string, ws: string, name: string) =>
  useAccountRead(org, ws, name, 'keepalive', () => fleet.laneKeepalive(org, ws, name), 60_000);

export const useLaneSites = (org: string, ws: string, name: string) =>
  useAccountRead(org, ws, name, 'sites', () => fleet.laneSites(org, ws, name));

export function useSetLaneSites(org: string, ws: string, name: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (body: { op: 'add' | 'remove' | 'set'; sites: string[] }) => fleet.setLaneSites(org, ws, name, body),
    onSuccess: (change) => {
      client.setQueryData<LaneSites>(accountPart(org, `${ws}/${name}`, 'sites'), { lane: change.lane, sites: change.sites, strict: change.strict });
    },
  });
}

/** A new watch link each time it is asked for. The page shows it once and never keeps it. */
export function useLaneViewLink(org: string, ws: string, name: string) {
  return useMutation({
    meta: { inline: true },
    mutationFn: () => fleet.laneViewLink(org, ws, name),
  });
}

// Lane control. Never optimistic: a screen must not say "you have control" before it is true.

export function useLaneControl(org: string, ws: string, name: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (request: ControlRequest) => fleet.laneControl(org, ws, name, request),
    onSuccess: (lane) => {
      client.setQueryData(keys.lane(org, lane.id), lane);
      void client.invalidateQueries({ queryKey: keys.workspaces(org) });
    },
  });
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

// Freeze (break glass). Never optimistic: the screen says it is frozen once the node has done it.

/** What is frozen here. A computer without the freeze service answers `available: false`. */
export const useFreezeStatus = (org: string) =>
  useQuery({ queryKey: keys.freeze(org), queryFn: () => served(fleet.freeze(org)) });

export function useFreezeNow(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (body: { scope: Exclude<FreezeScope, 'mesh'>; target?: string; reason: string }) => fleet.freezeNow(org, body),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.freeze(org) });
      void client.invalidateQueries({ queryKey: keys.status(org) });
    },
  });
}

/** Lifting restores nothing. The owner asks for it in person, so this is only ever called from an owner's screen. */
export function useUnfreeze(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (body: { scope: FreezeScope; target?: string }) => fleet.unfreeze(org, body),
    onSuccess: () => void client.invalidateQueries({ queryKey: keys.freeze(org) }),
  });
}

/** A poll that stops once the answer is "not served": asking again will not make the route appear. */
const pollUnlessNotServed = (ms: number) => (query: { state: { error: Error | null } }) => (isNotServed(query.state.error) ? false : ms);

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

// Logins and templates

/** One business and its accounts. */
export const useWorkspace = (org: string, id: string) =>
  useQuery({ queryKey: keys.workspace(org, id), queryFn: () => fleet.workspace(org, id) });
/** What is left to log in in one business. Only the owner reads it: the screen passes `enabled` for them alone. */
export const useWorkspaceQueue = (org: string, id: string, enabled = true) =>
  useQuery({ queryKey: keys.workspaceQueue(org, id), queryFn: () => fleet.workspaceQueue(org, id), enabled });

export const useLoginQueue = (org: string) =>
  useQuery({ queryKey: keys.queue(org), queryFn: () => served(fleet.loginQueue(org)) });

export function useSetReadiness(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...body }: { id: string; state: Readiness; note?: string }) => {
      const [ws = '', name = ''] = id.split('/');
      return fleet.setReadiness(org, ws, name, body);
    },
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.queue(org) });
      void client.invalidateQueries({ queryKey: keys.workspaces(org) });
    },
  });
}

export const useTemplates = (org: string) =>
  useQuery({ queryKey: keys.templates(org), queryFn: () => served(fleet.templates(org)) });

// Metrics. All served() until the routes land, so charts can say so calmly.

export const useActivityMetrics = (org: string, range: ActivityRange, scope: { workspace?: string; lane?: string } = {}) =>
  useQuery({
    queryKey: keys.activityMetrics(org, range, scope),
    queryFn: () => served(fleet.activityMetrics(org, range, scope)),
    // A new range keeps the old chart on screen until the new one lands. No skeleton flash.
    placeholderData: keepPreviousData,
  });

/** One activity series per workspace, for summaries that list them side by side. */
export const useWorkspaceActivity = (org: string, range: ActivityRange, workspaces: string[]) =>
  useQueries({
    queries: workspaces.map((workspace) => ({
      queryKey: keys.activityMetrics(org, range, { workspace }),
      queryFn: () => served(fleet.activityMetrics(org, range, { workspace })),
      staleTime: 60_000,
    })),
  });

export const useNodeMetrics = (org: string, id: string, range: NodeRange) =>
  useQuery({
    queryKey: keys.nodeMetrics(org, id, range),
    queryFn: () => served(fleet.nodeMetrics(org, id, range)),
    placeholderData: keepPreviousData,
  });

export const useLaneMetrics = (org: string, ws: string, name: string) =>
  useQuery({
    queryKey: keys.laneMetrics(org, `${ws}/${name}`, '7d'),
    queryFn: () => served(fleet.laneMetrics(org, ws, name)),
  });

// Activity and runs

export function useActivity(org: string, filters: ActivityFilters) {
  return useInfiniteQuery({
    queryKey: keys.activity(org, filters),
    queryFn: ({ pageParam }) => served(fleet.activity(org, filters, pageParam)),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_before ?? undefined,
  });
}

/**
 * History as stories, a few days a page. Asked again every half minute while it is on screen, so
 * new things arrive without a reload; the first page also carries what is working now.
 */
export function useHistory(org: string, scope: { workspace?: string; show?: HistoryShow }, days = 3) {
  return useInfiniteQuery({
    queryKey: keys.history(org, scope),
    queryFn: ({ pageParam }) => served(fleet.history(org, { ...scope, before: pageParam, days })),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_before ?? undefined,
    refetchInterval: 30_000,
  });
}

export const useRun = (org: string, id: string | null) =>
  useQuery({
    queryKey: keys.run(org, id ?? ''),
    queryFn: () => fleet.run(org, id ?? ''),
    enabled: id !== null,
  });

// Flows and schedules

export const useFlows = (org: string) =>
  useQuery({ queryKey: keys.flows(org), queryFn: () => served(fleet.flows(org)) });
/** Every account's Ask me first, in one call: a screen picks its own by lane id. */
export const useConfirmation = (org: string) => useQuery({ queryKey: keys.confirmation(org), queryFn: () => fleet.confirmation(org) });
export const useConfirmMethods = (org: string) => useQuery({ queryKey: keys.confirmMethods(org), queryFn: () => fleet.confirmMethods(org) });

/** Moves at once, and only the last saved choice comes back if the computer says no. */
export function useSetConfirmation(org: string) {
  const client = useQueryClient();
  const key = keys.confirmation(org);
  return useMutation({
    meta: { inline: true },
    mutationFn: (body: { setting: ConfirmationSetting } | { business: string; overlay: ConfirmationOverlay }) => fleet.setConfirmation(org, body),
    onMutate: async (body) => {
      await client.cancelQueries({ queryKey: key });
      const before = client.getQueryData<ConfirmationView>(key);
      client.setQueryData<ConfirmationView>(key, (view) => {
        if (!view) return view;
        if ('setting' in body) return { ...view, setting: body.setting };
        return { ...view, overlays: { ...view.overlays, [body.business]: body.overlay } };
      });
      return { before };
    },
    onError: (_error, _body, context) => {
      if (context?.before) client.setQueryData(key, context.before);
    },
    onSuccess: (view) => {
      client.setQueryData(key, view);
      void client.invalidateQueries({ queryKey: keys.confirmMethods(org) });
      void client.invalidateQueries({ queryKey: keys.approvals(org) });
    },
  });
}

/** The fresh check a heavy yes spends. It proves a person, here, for this one approval: their passkey signs the node's challenge. */
export function useProveYes(org: string) {
  return useMutation({
    meta: { inline: true },
    mutationFn: async (id: string) => fleet.proveYes(org, id, await getPasskey(await fleet.proveYesOptions(org, id))),
  });
}

export const useLaneAskFirst = (org: string) =>
  useQuery({ queryKey: keys.askFirst(org), queryFn: () => served(fleet.laneAskFirst(org)) });

export const useSchedules = (org: string) =>
  useQuery({ queryKey: keys.schedules(org), queryFn: () => served(fleet.schedules(org)) });

export function useCancelRun(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => fleet.cancelRun(org, id),
    onSuccess: (run) => {
      client.setQueryData(keys.run(org, run.id), run);
      void client.invalidateQueries({ queryKey: ['org', org, 'activity'] });
      void client.invalidateQueries({ queryKey: ['org', org, 'history'] });
    },
  });
}

/** Every node page's schedule report in this org. */
const nodeSchedulesOf = (org: string) => (query: { queryKey: readonly unknown[] }) => {
  const [scope, slug, kind, , report, part] = query.queryKey;
  return scope === 'org' && slug === org && kind === 'node' && report === 'report' && part === 'schedules';
};

/** A pause is a small reversible toggle, so it answers at once and rolls back if refused. */
export function useSetScheduleEnabled(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) => fleet.setScheduleEnabled(org, id, enabled),
    onMutate: async ({ id, enabled }) => {
      await client.cancelQueries({ queryKey: keys.schedules(org) });
      await client.cancelQueries({ predicate: nodeSchedulesOf(org) });
      const before = client.getQueryData<Schedule[]>(keys.schedules(org));
      const nodes = client.getQueriesData<NodeSchedule[] | null>({ predicate: nodeSchedulesOf(org) });
      const flip = <T extends Schedule>(current: T[] | null | undefined) =>
        current?.map((item) => (item.id === id ? { ...item, enabled, next_fire_at: enabled ? item.next_fire_at : null } : item));
      client.setQueryData<Schedule[]>(keys.schedules(org), flip);
      // A node page shows the same schedule, so it flips there too, and rolls back with it.
      client.setQueriesData<NodeSchedule[] | null>({ predicate: nodeSchedulesOf(org) }, (current) => flip(current) ?? current);
      return { before, nodes };
    },
    onError: (_error, _vars, context) => {
      client.setQueryData(keys.schedules(org), context?.before);
      for (const [key, data] of context?.nodes ?? []) client.setQueryData(key, data);
    },
    onSettled: () => {
      void client.invalidateQueries({ predicate: nodeSchedulesOf(org) });
      return client.invalidateQueries({ queryKey: keys.schedules(org) });
    },
  });
}

/**
 * A routine's Ask me first answers at once, in every view of it, and only that routine rolls back
 * on a refusal, so a second switch flipped meanwhile keeps its place. The control says the refusal
 * itself, in the words of what came back.
 */
export function useSetFlowAskFirst(org: string) {
  const client = useQueryClient();
  const key = keys.flows(org);
  return useMutation({
    meta: { inline: true },
    mutationFn: ({ id, on }: { id: string; on: boolean }) => fleet.setFlowAskFirst(org, id, on),
    onMutate: async ({ id, on }) => {
      await client.cancelQueries({ queryKey: key });
      const before = client.getQueryData<Flow[]>(key)?.find((item) => item.id === id);
      client.setQueryData<Flow[]>(key, (items) => items?.map((item) => (item.id === id ? flowSet(item, on) : item)));
      return { before };
    },
    onError: (_error, { id }, context) => {
      if (context?.before) client.setQueryData<Flow[]>(key, (items) => items?.map((item) => (item.id === id ? context.before! : item)));
    },
    onSuccess: (data) => client.setQueryData<Flow[]>(key, (items) => items?.map((item) => (item.id === data.id ? { ...item, ...data } : item))),
  });
}

/** An account's Ask me first also decides whether its routines ask, so they move with it. */
export function useSetLaneAskFirst(org: string) {
  const client = useQueryClient();
  const key = keys.askFirst(org);
  const flowsKey = keys.flows(org);
  return useMutation({
    meta: { inline: true },
    mutationFn: ({ lane, on }: { lane: string; on: boolean }) => fleet.setLaneAskFirst(org, lane, on),
    onMutate: async ({ lane, on }) => {
      await client.cancelQueries({ queryKey: key });
      await client.cancelQueries({ queryKey: flowsKey });
      const before = client.getQueryData<LaneAskFirst[]>(key)?.find((item) => item.lane === lane);
      const flows = client.getQueryData<Flow[]>(flowsKey)?.filter((item) => item.lane === lane) ?? [];
      client.setQueryData<LaneAskFirst[]>(key, (items) => items?.map((item) => (item.lane === lane ? { ...item, ask_first: on, asks_first: on } : item)));
      client.setQueryData<Flow[]>(flowsKey, (items) => items?.map((item) => (item.lane === lane ? flowAccountSet(item, on) : item)));
      return { before, flows };
    },
    onError: (_error, { lane }, context) => {
      if (context?.before) client.setQueryData<LaneAskFirst[]>(key, (items) => items?.map((item) => (item.lane === lane ? context.before! : item)));
      client.setQueryData<Flow[]>(flowsKey, (items) => items?.map((item) => context?.flows.find((flow) => flow.id === item.id) ?? item));
    },
    onSuccess: (data) => {
      client.setQueryData<LaneAskFirst[]>(key, (items) => items?.map((item) => (item.lane === data.lane ? data : item)));
      return client.invalidateQueries({ queryKey: flowsKey });
    },
  });
}

export const useRoutineDay = (org: string) => useQuery({ queryKey: keys.routineDay(org), queryFn: () => fleet.routineDay(org), refetchInterval: pollUnlessNotServed(60_000) });
export const useRoutineCatalog = (org: string) => useQuery({ queryKey: keys.routineCatalog(org), queryFn: () => fleet.routineCatalog(org), staleTime: 300_000 });
export const useRoutineRuns = (org: string, id: string) => useQuery({ queryKey: keys.routineRuns(org, id), queryFn: () => fleet.routineRuns(org, id) });

export function useRetryRoutineRun(org: string, id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (run: string) => fleet.retryRoutineRun(org, id, run),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.routineRuns(org, id) });
      void client.invalidateQueries({ queryKey: keys.flows(org) });
    },
  });
}

export const useMailboxes = (org: string) => useQuery({ queryKey: keys.mailboxes(org), queryFn: () => fleet.mailboxes(org), refetchInterval: pollUnlessNotServed(60_000) });
export const useMailMessages = (org: string) => useQuery({ queryKey: keys.mailMessages(org), queryFn: () => fleet.mailMessages(org) });
/** One message in full. The list carries only a clip, so the reader asks for the body when it opens. */
export const useMailMessage = (org: string, id: string) =>
  useQuery({ queryKey: [...keys.mailMessages(org), id], queryFn: () => fleet.mailMessage(org, id), enabled: Boolean(id) });
export const useLeadBatches = (org: string) => useQuery({ queryKey: keys.leadBatches(org), queryFn: () => fleet.leadBatches(org) });
export const useLeadBatch = (org: string, id: string) => useQuery({ queryKey: keys.leadBatch(org, id), queryFn: () => fleet.leadBatch(org, id) });
export const useMailLists = (org: string) => useQuery({ queryKey: keys.mailLists(org), queryFn: () => fleet.mailLists(org) });
export const useMailList = (org: string, id: string) => useQuery({ queryKey: keys.mailList(org, id), queryFn: () => fleet.mailList(org, id) });
export const useMailRoutine = (org: string, id: string) => useQuery({ queryKey: keys.mailRoutine(org, id), queryFn: () => fleet.mailRoutine(org, id) });
export const useMailDiscover = (org: string) => useMutation({ mutationFn: (address: string) => fleet.mailDiscover(org, address), meta: { inline: true } });

export function useMailMark(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, read }: { id: string; read: boolean }) => fleet.mailMark(org, id, read),
    onSuccess: () => void client.invalidateQueries({ queryKey: keys.mailMessages(org) }),
  });
}

/** The link to the node's own form for a new mailbox. Only the link comes back, never a field. */
export function useMailFormLink(org: string) {
  return useMutation({ mutationFn: (body: MailFormLinkBody) => fleet.mailFormLink(org, body), meta: { inline: true } });
}

/** Whether the node's form is still open, asked every two seconds until it has ended. */
export function useMailFormState(org: string, token: string | null) {
  return useQuery({
    queryKey: keys.mailForm(org, token ?? ''),
    queryFn: () => fleet.mailFormState(org, token ?? ''),
    enabled: Boolean(token),
    refetchInterval: (query) => (query.state.data?.state === 'open' ? 2_000 : false),
  });
}

/** Cancels the node's form for a link: it ends at once, and the password it would have taken is never asked for. */
export function useEndMailForm(org: string) {
  return useMutation({ mutationFn: (token: string) => fleet.mailFormEnd(org, token), meta: { inline: true } });
}

export function useMailSend(org: string) {
  return useMutation({ mutationFn: (body: { mailbox: string; to: string; subject: string; body: string; reply_to?: string }) => fleet.mailSend(org, body), meta: { inline: true } });
}

/** Keeps the ones a person left in, or none. Either way the batch is decided, once. */
export function useDecideBatch(org: string, id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (keep: string[]) => fleet.decideLeadBatch(org, id, keep),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.leadBatches(org) });
      void client.invalidateQueries({ queryKey: keys.mailLists(org) });
    },
  });
}

export function useUpdateMailList(org: string, id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: { state?: MailList['state']; pace?: number }) => fleet.updateMailList(org, id, body),
    onSuccess: (list) => {
      client.setQueryData(keys.mailList(org, id), list);
      void client.invalidateQueries({ queryKey: keys.mailLists(org) });
    },
  });
}

export const useSiteApis = (org: string) => useQuery({ queryKey: keys.siteApis(org), queryFn: () => fleet.siteApis(org), refetchInterval: pollUnlessNotServed(60_000) });
export const useSiteApi = (org: string, api: string) => useQuery({ queryKey: keys.siteApi(org, api), queryFn: () => fleet.siteApi(org, api) });
export const useSiteUsage = (org: string, api: string) => useQuery({ queryKey: keys.siteUsage(org, api), queryFn: () => fleet.siteUsage(org, api) });
export const useSiteKeys = (org: string) => useQuery({ queryKey: keys.siteKeys(org), queryFn: () => fleet.siteKeys(org) });
export const useSiteDiscovery = (org: string, lane: string | null) =>
  useQuery({ queryKey: keys.siteDiscovery(org, lane ?? ''), queryFn: () => fleet.siteDiscovery(org, lane ?? ''), enabled: Boolean(lane), staleTime: Infinity });

/** Pause and resume move at once on both the page and the list, and roll back together. */
export function useSiteAction(org: string, api: string) {
  const client = useQueryClient();
  const detail = keys.siteApi(org, api);
  const list = keys.siteApis(org);
  return useMutation({
    mutationFn: (action: SiteAction) => fleet.siteAction(org, api, action),
    onMutate: async (action) => {
      if (action.kind !== 'pause' && action.kind !== 'resume') return null;
      await client.cancelQueries({ queryKey: detail });
      await client.cancelQueries({ queryKey: list });
      const before = { detail: client.getQueryData<SiteApiDetail>(detail), list: client.getQueryData<SiteApi[]>(list) };
      const next = action.kind === 'pause' ? ({ stage: 'paused', health: 'paused' } as const) : ({ stage: 'active', health: 'good', problem: null } as const);
      client.setQueryData<SiteApiDetail>(detail, (item) => (item ? { ...item, ...next } : item));
      client.setQueryData<SiteApi[]>(list, (items) => items?.map((item) => (item.key === api ? { ...item, ...next } : item)));
      return before;
    },
    onSuccess: (data) => client.setQueryData(detail, data),
    // The rollback is here, not in onError: a mutation with its own onError is never told by the app's cache, and
    // a failed action must say why it did not happen (the cache shows the message).
    onSettled: (_data, error, _action, before) => {
      if (error && before) {
        client.setQueryData(detail, before.detail);
        client.setQueryData(list, before.list);
      }
      return client.invalidateQueries({ queryKey: list });
    },
  });
}

export function useSiteTry(org: string, api: string) {
  return useMutation({
    meta: { inline: true },
    mutationFn: (body: { endpoint: string; params: Record<string, string>; fresh: boolean }) => fleet.siteTry(org, api, body),
  });
}

export function useCreateSiteKey(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (body: SiteKeyInput) => fleet.createSiteKey(org, body),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.siteKeys(org) }),
  });
}

export function useRevokeSiteKey(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => fleet.revokeSiteKey(org, id),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.siteKeys(org) }),
  });
}

export function useSaveRoutine(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: ({ id, body }: { id: string | null; body: RoutineDefinition }) => (id ? fleet.saveRoutine(org, id, body) : fleet.createRoutine(org, body)),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.flows(org) }),
  });
}

export function useRunFlow(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, params }: { id: string; params: Record<string, unknown> }) =>
      fleet.runFlow(org, id, params),
    // The list shows what each routine last did, and the routine's own page lists its runs.
    onSuccess: (_run, { id }) => {
      void client.invalidateQueries({ queryKey: keys.routineRuns(org, id) });
      return client.invalidateQueries({ queryKey: keys.flows(org) });
    },
  });
}

/** Turning a saved routine on. The node answers whether it is on, so the list is read again rather than guessed at. */
export function useEnableRoutine(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (id: string) => fleet.enableRoutine(org, id),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.flows(org) }),
  });
}

export function useCreateSchedule(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: Omit<Schedule, 'id' | 'next_run'>) => fleet.createSchedule(org, body),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.schedules(org) }),
  });
}

export function useDeleteSchedule(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => fleet.deleteSchedule(org, id),
    // A routine's own schedule switches its routine off, so the routines are read again with the schedules.
    onSuccess: () => Promise.all([client.invalidateQueries({ queryKey: keys.schedules(org) }), client.invalidateQueries({ queryKey: keys.flows(org) })]),
  });
}

// Nodes

export const useNodes = (org: string) =>
  useQuery({ queryKey: keys.nodes(org), queryFn: () => served(fleet.nodes(org)) });
export const useNode = (org: string, id: string) =>
  useQuery({ queryKey: keys.node(org, id), queryFn: () => fleet.node(org, id) });

export function useCreateJoinToken(org: string) {
  return useMutation({
    mutationFn: (body: { name?: string }) => fleet.createJoinToken(org, body),
  });
}

// Node depth. `null` means the node does not report this yet, which is not the same as empty.

const useNodeReport = <T,>(org: string, id: string, part: string, load: () => Promise<T | null>, enabled = true) =>
  useQuery({
    queryKey: keys.nodeReport(org, id, part),
    queryFn: () => served(reported(load())),
    enabled,
    // What a node reports changes slowly, and the live event stream covers what does not.
    staleTime: 30_000,
  });

export const useNodeSystem = (org: string, id: string) =>
  useNodeReport(org, id, 'system', () => fleet.nodeSystem(org, id));
export const useNodePrograms = (org: string, id: string) =>
  useNodeReport(org, id, 'programs', () => fleet.nodePrograms(org, id));
export const useNodeServices = (org: string, id: string) =>
  useNodeReport(org, id, 'services', () => fleet.nodeServices(org, id));
export const useNodeTools = (org: string, id: string) =>
  useNodeReport(org, id, 'tools', () => fleet.nodeTools(org, id));
export const useNodeHealth = (org: string, id: string, enabled = true) =>
  useNodeReport(org, id, 'health', () => fleet.nodeHealth(org, id), enabled);
export const useNodeSchedules = (org: string, id: string) =>
  useNodeReport(org, id, 'schedules', () => fleet.nodeSchedules(org, id));
export const useNodeConnections = (org: string, id: string, range = '30d', enabled = true) =>
  useNodeReport(org, id, `connections-${range}`, () => fleet.nodeConnections(org, id, range), enabled);

// This computer's health, backups, version, environments, identity and settings (features/nodes).
export const useNodeHealthRounds = (org: string, id: string) =>
  useNodeReport(org, id, 'health-rounds', () => fleet.nodeHealthRounds(org, id));
export const useNodeUpgrade = (org: string, id: string) =>
  useNodeReport(org, id, 'upgrade', () => fleet.nodeUpgrade(org, id));
export const useNodeEnvironments = (org: string, id: string) =>
  useNodeReport(org, id, 'environments', () => fleet.nodeEnvironments(org, id));
export const useNodeIdentity = (org: string, id: string) =>
  useNodeReport(org, id, 'identity', () => fleet.nodeIdentity(org, id));
export const useNodeSettings = (org: string, id: string) =>
  useNodeReport(org, id, 'settings', () => fleet.nodeSettings(org, id));

/** The backups list. It polls while a backup or a check runs, and stops when it has ended. */
export const useNodeBackups = (org: string, id: string) =>
  useQuery({
    queryKey: keys.nodeReport(org, id, 'backups'),
    queryFn: () => fleet.nodeBackups(org, id),
    refetchInterval: (query) => (query.state.data?.job?.state === 'running' ? 2000 : false),
  });

/** A full check. Its answer is kept by the screen, so the last run stays on screen. */
export function useRunHealthCheck(org: string, id: string) {
  return useMutation({
    mutationFn: () => fleet.nodeHealthRun(org, id),
    meta: { inline: true },
  });
}

/** Starts a backup. The screen opens the form it answers with, where the passphrase is typed. */
export function useStartBackup(org: string, id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => fleet.startBackup(org, id),
    meta: { inline: true },
    onSettled: () => client.invalidateQueries({ queryKey: keys.nodeReport(org, id, 'backups') }),
  });
}

export function useCheckBackup(org: string, id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (name: string) => fleet.checkBackup(org, id, name),
    meta: { inline: true },
    onSettled: () => client.invalidateQueries({ queryKey: keys.nodeReport(org, id, 'backups') }),
  });
}

export function useServiceAction(org: string, id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ lane, action }: { lane: string; action: 'start' | 'stop' }) => fleet.serviceAction(org, id, lane, action),
    meta: { inline: true },
    onSettled: () => client.invalidateQueries({ queryKey: keys.nodeReport(org, id, 'environments') }),
  });
}

/** One service's last lines. Read on demand, never kept: the screen asks again when it is opened. */
export const useServiceLogs = (org: string, id: string, lane: string | null) =>
  useQuery({
    queryKey: ['org', org, 'node', id, 'service-logs', lane] as const,
    queryFn: () => fleet.serviceLogs(org, id, lane ?? ''),
    enabled: lane !== null,
    staleTime: 0,
    gcTime: 0,
  });

export function useAddSite(org: string, id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ lane, origin, reason }: { lane: string; origin: string; reason: string }) =>
      fleet.addSite(org, id, lane, { origin, reason }),
    meta: { inline: true },
    onSettled: () => client.invalidateQueries({ queryKey: keys.nodeReport(org, id, 'identity') }),
  });
}

export function useRemoveSite(org: string, id: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ lane, origin }: { lane: string; origin: string }) => fleet.removeSite(org, id, lane, origin),
    meta: { inline: true },
    onSettled: () => client.invalidateQueries({ queryKey: keys.nodeReport(org, id, 'identity') }),
  });
}

// The fleet map

export const useTopology = (org: string, window: TopologyWindow = '1h') =>
  useQuery({
    queryKey: keys.topology(org, window),
    queryFn: () => served(fleet.topology(org, window)),
    placeholderData: keepPreviousData,
    // The map folds live events in itself, so a refetch only corrects drift.
    staleTime: 60_000,
    refetchInterval: 120_000,
  });

export const useTopologyEvents = (org: string, from: string, to: string, enabled: boolean) =>
  useQuery({
    queryKey: keys.topologyEvents(org, from, to),
    queryFn: () => served(fleet.topologyEvents(org, from, to)),
    enabled,
    placeholderData: keepPreviousData,
    staleTime: 60_000,
  });

export function useRemoveNode(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => fleet.removeNode(org, id),
    onSuccess: () => {
      client.invalidateQueries({ queryKey: keys.nodes(org) });
      client.invalidateQueries({ queryKey: keys.status(org) });
    },
  });
}

/** Statuses a task still has to finish: the list is polled while one of these is in it. */
export const TASK_ACTIVE: BrainTaskStatus[] = ['queued', 'starting', 'running', 'waiting_approval'];

/**
 * What agents handed to an AI model. Its state is live: while any task is still going, the list is
 * read again every few seconds, and stops once they have all ended.
 */
export const useBrainTasks = (org: string, enabled = true) =>
  useQuery({
    queryKey: keys.brainTasks(org),
    queryFn: () => brainTasks.list(org),
    enabled,
    refetchInterval: (query) => (query.state.data?.some((task) => TASK_ACTIVE.includes(task.status)) ? 3_000 : false),
  });
export const useBrainTask = (org: string, id: string, enabled = true) =>
  useQuery({
    queryKey: keys.brainTask(org, id),
    queryFn: () => brainTasks.get(org, id),
    enabled,
    refetchInterval: (query) => (query.state.data && TASK_ACTIVE.includes(query.state.data.status) ? 3_000 : false),
  });
export function useCancelBrainTask(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => brainTasks.cancel(org, id),
    meta: { inline: true },
    onSuccess: () => client.invalidateQueries({ queryKey: keys.brainTasks(org) }),
  });
}
export function useResumeBrainTask(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => brainTasks.resume(org, id),
    meta: { inline: true },
    onSuccess: () => client.invalidateQueries({ queryKey: keys.brainTasks(org) }),
  });
}

/** Safety checks a person turned off. Read only when the screen can show them (Technical details, the owner). */
export const useOverrides = (org: string, enabled = true) =>
  useQuery({ queryKey: keys.overrides(org), queryFn: () => overrides.list(org), enabled });

// Proof (History): the action log, checked and shared (docs/API.md, "Audit").
/** A day's accounts. With no day, the node's today, in its own timezone. */
export const useProofDay = (org: string, day?: string) =>
  useQuery({ queryKey: keys.proofDay(org, day), queryFn: () => proof.dayIndex(org, day) });
export const useProofReplay = (org: string, lane: string, day: string) =>
  useQuery({ queryKey: keys.proofReplay(org, lane, day), queryFn: () => proof.replay(org, lane, day), enabled: lane !== '' && day !== '' });
/** Whether the whole log is intact. The owner alone: the screen passes `enabled` only for the owner. */
export const useProofVerify = (org: string, enabled = true) =>
  useQuery({ queryKey: keys.proofVerify(org), queryFn: () => proof.verify(org), staleTime: 60_000, enabled });
export const useProofShares = (org: string, enabled = true) =>
  useQuery({ queryKey: keys.proofShares(org), queryFn: () => proof.shares(org), enabled });
export function useCreateShare(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: ProofShareInput) => proof.createShare(org, body),
    meta: { inline: true },
    onSuccess: () => client.invalidateQueries({ queryKey: keys.proofShares(org) }),
  });
}
export function useRevokeShare(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => proof.revokeShare(org, id),
    meta: { inline: true },
    onSuccess: () => client.invalidateQueries({ queryKey: keys.proofShares(org) }),
  });
}
/** A read-only link, opened with no sign-in. A bad or ended link answers once; it is not retried. */
export const useProofShared = (token: string) =>
  useQuery({ queryKey: keys.proofShared(token), queryFn: () => proof.shared(token), enabled: token !== '', retry: false });

// Brains, vault, grants

export const useBrains = (org: string) =>
  useQuery({ queryKey: keys.brains(org), queryFn: () => served(fleet.brains(org)) });
export const useVault = (org: string) =>
  useQuery({ queryKey: keys.vault(org), queryFn: () => served(fleet.vault(org)) });
export const useGrants = (org: string) =>
  useQuery({ queryKey: keys.grants(org), queryFn: () => served(fleet.grants(org)) });
/** Only the owner may read the vault's state on the computer, so anyone else passes `enabled: false`. */
export const useVaultStatus = (org: string, enabled = true) =>
  useQuery({ queryKey: keys.vaultStatus(org), queryFn: () => served(fleet.vaultStatus(org)), enabled });

/** Seal the vault. Unlocking is a form on the computer (useStartForm), so the passphrase never reaches the app. */
export function useLockVault(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: () => fleet.lockVault(org),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.vaultStatus(org) });
      void client.invalidateQueries({ queryKey: keys.vault(org) });
    },
  });
}

export function useRemoveVaultEntry(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (id: string) => fleet.removeVaultEntry(org, id),
    onSuccess: () => void client.invalidateQueries({ queryKey: keys.vault(org) }),
  });
}

/** Opens one form on the computer. The app gets the trip back and opens its `form_url` itself. */
export const useStartForm = (org: string) =>
  useMutation({ meta: { inline: true }, mutationFn: (body: { kind: Exclude<FormKind, 'login'>; fields?: Record<string, string> }) => fleet.startForm(org, body) });

/** How far a form got. Asks once a second while it is open; when it is done, the data it changed is read again. */
export function useFormTrip(org: string, id: string | undefined) {
  const client = useQueryClient();
  const query = useQuery({
    queryKey: id ? keys.formTrip(org, id) : ['org', org, 'forms', 'none'],
    queryFn: () => fleet.formTrip(org, id ?? ''),
    enabled: Boolean(id),
    refetchInterval: (state) => (state.state.data?.stage === 'waiting' ? 1_000 : false),
  });
  const done = query.data?.stage === 'done';
  useEffect(() => {
    if (!done) return;
    for (const key of [keys.vault(org), keys.vaultStatus(org), keys.browserDevices(org), keys.proxies(org), keys.workspaces(org)]) {
      void client.invalidateQueries({ queryKey: key });
    }
  }, [client, done, org]);
  return query;
}

export const useBrowserDevices = (org: string) =>
  useQuery({ queryKey: keys.browserDevices(org), queryFn: () => served(fleet.browserDevices(org)) });

export function useCreateBrowserDevice(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (body: Parameters<typeof fleet.createBrowserDevice>[1]) => fleet.createBrowserDevice(org, body),
    onSuccess: () => void client.invalidateQueries({ queryKey: keys.browserDevices(org) }),
  });
}

export function useRemoveBrowserDevice(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (name: string) => fleet.removeBrowserDevice(org, name),
    onSuccess: () => void client.invalidateQueries({ queryKey: keys.browserDevices(org) }),
  });
}

/** What moving a lane to a device changes. Asked only when the person picks a device to move to. */
export const useDeviceMovePlan = (org: string, ws: string, name: string, to: string | null) =>
  useQuery({
    queryKey: keys.deviceMove(org, ws, name, to ?? 'none'),
    queryFn: () => fleet.deviceMovePlan(org, ws, name, to),
    enabled: to !== undefined,
  });

export function useMoveLaneDevice(org: string, ws: string, name: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (body: { device: string | null; confirm?: boolean }) => fleet.moveLaneDevice(org, ws, name, body),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.browserDevices(org) });
      void client.invalidateQueries({ queryKey: ['org', org, 'lanes'] });
    },
  });
}

export const useProxies = (org: string) =>
  useQuery({ queryKey: keys.proxies(org), queryFn: () => served(fleet.proxies(org)) });

export function useCreateProxy(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (body: Parameters<typeof fleet.createProxy>[1]) => fleet.createProxy(org, body),
    onSuccess: () => void client.invalidateQueries({ queryKey: keys.proxies(org) }),
  });
}

const LOGIN_OVER: string[] = ['signed_in', 'cancelled', 'done', 'none'];

/** A lane's sign-in. Asks once a second while one is running, and stops once it is over. */
export const useLaneLogin = (org: string, ws: string, name: string) =>
  useQuery({
    queryKey: keys.laneLogin(org, ws, name),
    queryFn: () => served(fleet.laneLogin(org, ws, name)),
    refetchInterval: (state) => (state.state.data && !LOGIN_OVER.includes(state.state.data.state) ? 1_000 : false),
  });

/** Starts the sign-in on the lane and a form on the computer for it. The app opens the form's `form_url`. */
export function useStartLaneLogin(org: string, ws: string, name: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: () => fleet.startLaneLogin(org, ws, name),
    onSuccess: (data) => client.setQueryData(keys.laneLogin(org, ws, name), data.login),
  });
}

/** Signs the lane in from its saved login. No form and no question: the vault's entry is the owner's permission. */
export function useVaultSignIn(org: string, ws: string, name: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: () => fleet.laneLoginVault(org, ws, name),
    onSuccess: (data) => {
      client.setQueryData(keys.laneLogin(org, ws, name), data);
      void client.invalidateQueries({ queryKey: keys.vault(org) });
    },
  });
}

export function useLaneLoginAction(org: string, ws: string, name: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (action: 'refresh' | 'take' | 'resume' | 'cancel') => fleet.laneLoginAction(org, ws, name, action),
    onSuccess: (data) => client.setQueryData(keys.laneLogin(org, ws, name), data),
  });
}

/** Concept stage. It refreshes often, because the picture on the page follows a thing being done. */
export const useWorkers = (org: string) => useQuery({ queryKey: keys.workers(org), queryFn: () => served(fleet.workers(org)), refetchInterval: pollUnlessNotServed(3_000) });
export const useWorkersOnComputer = (org: string, node: string) => useQuery({ queryKey: keys.workersOnComputer(org, node), queryFn: () => reported(fleet.workersOnComputer(org, node)), refetchInterval: 5_000 });
/** A computer without workers says nothing, and the account page then shows nothing. */
export const useWorkerOnAccount = (org: string, lane: string) => useQuery({ queryKey: keys.workerOnAccount(org, lane), queryFn: () => reported(fleet.workerOnAccount(org, lane)), refetchInterval: 5_000 });
/** Null on an account no worker reaches: the account page then has no row, and Ask me first does not name it. */
export const useWorkerAccount = (org: string, lane: string) => useQuery({ queryKey: keys.workerAccount(org, lane), queryFn: () => reported(fleet.workerAccount(org, lane)) });

/** A person hands the worker a job on one account. What it does then shows where it always does. */
export function useStartWorkerThing(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (body: { lane: string; told: string }) => fleet.startWorkerThing(org, body),
    onSuccess: (thing, { lane }) => {
      client.setQueryData(keys.workerOnAccount(org, lane), thing);
      void client.invalidateQueries({ queryKey: keys.workers(org) });
    },
  });
}

/** One account's switch. Moves at once, and rolls back alone. */
export function useSetWorkerAccount(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: ({ lane, on }: { lane: string; on: boolean }) => fleet.setWorkerAccount(org, lane, on),
    onMutate: async ({ lane, on }) => {
      const key = keys.workerAccount(org, lane);
      await client.cancelQueries({ queryKey: key });
      client.setQueryData<WorkerAccount | null>(key, (current) => current && { ...current, on });
    },
    onSettled: () => client.invalidateQueries({ queryKey: ['org', org, 'workers'] }),
  });
}

/** A switch moves at once in every view of it, and rolls back alone. */
export function useSetWorkerPlace(org: string) {
  const client = useQueryClient();
  const key = keys.workers(org);
  return useMutation({
    meta: { inline: true },
    mutationFn: ({ node, on }: { node: string; on: boolean }) => fleet.setWorkerPlace(org, node, on),
    onMutate: async ({ node, on }) => {
      await client.cancelQueries({ queryKey: key });
      client.setQueryData<WorkerOverview>(key, (current) => current && { ...current, places: current.places.map((item) => (item.node === node ? { ...item, worker: on ? 'on' : 'off' } : item)) });
    },
    onSuccess: (data) => client.setQueryData(key, data),
    onError: () => client.invalidateQueries({ queryKey: key }),
    onSettled: () => client.invalidateQueries({ queryKey: ['org', org, 'workers'] }),
  });
}

export function useSetWorkerJob(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: ({ job, brain }: { job: WorkerJob; brain: string }) => fleet.setWorkerJob(org, job, brain),
    onSuccess: (data) => client.setQueryData(keys.workers(org), data),
  });
}

export function useSetWorkerLimits(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (limits: Partial<WorkerLimits>) => fleet.setWorkerLimits(org, limits),
    onSuccess: (data) => client.setQueryData(keys.workers(org), data),
  });
}

export const useStartHandover = (org: string) => useMutation({ meta: { inline: true }, mutationFn: (body: { service: string; computer: string }) => fleet.startHandover(org, body) });

/** Where a key has got to, as the computer tells it. Stops asking once it is over, and what it changed is read again. */
export function useHandover(org: string, id: string | undefined) {
  const client = useQueryClient();
  const query = useQuery({
    queryKey: ['org', org, 'brains', 'handover', id ?? ''],
    queryFn: () => fleet.handover(org, id ?? ''),
    enabled: Boolean(id),
    refetchInterval: (state) => (state.state.data?.stage === 'done' || state.state.data?.stage === 'failed' ? false : 1_000),
  });
  const done = query.data?.stage === 'done';
  useEffect(() => {
    if (!done) return;
    void client.invalidateQueries({ queryKey: keys.brains(org) });
    void client.invalidateQueries({ queryKey: keys.workers(org) });
  }, [client, done, org]);
  return query;
}

export const useHandoverFilled = (org: string) => useMutation({ meta: { inline: true }, mutationFn: (id: string) => fleet.handoverFilled(org, id) });

export function useSetBrainOrder(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (order: string[]) => fleet.setBrainOrder(org, order),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.brains(org) }),
  });
}

export function useCreateGrant(org: string, options: { inline?: boolean } = {}) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: options.inline },
    mutationFn: (body: NewGrant) => served(fleet.createGrant(org, body)),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.grants(org) }),
  });
}

export function useRevokeGrant(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => fleet.revokeGrant(org, id),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.grants(org) }),
  });
}

export const useStandingApprovals = (org: string, all = false) =>
  useQuery({ queryKey: keys.standing(org, all), queryFn: () => served(fleet.standingApprovals(org, all)) });

/** Asked before one is made, so nobody sets one up believing it will stop the questions. */
export const useStandingSupport = (org: string, workspace: string) =>
  useQuery({
    queryKey: keys.standingSupport(org, workspace),
    queryFn: () => fleet.standingSupport(org, workspace),
    enabled: workspace !== '',
    staleTime: 30_000,
  });

export function useCreateStandingApproval(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (body: NewStandingApproval) => fleet.createStandingApproval(org, body),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.standingAny(org) }),
  });
}

/** Revoking leaves the list at once and comes back if the server says no. */
export function useRevokeStandingApproval(org: string) {
  const client = useQueryClient();
  const active = keys.standing(org, false);
  return useMutation({
    mutationFn: (id: string) => fleet.deleteStandingApproval(org, id),
    onMutate: async (id) => {
      await client.cancelQueries({ queryKey: active });
      const before = client.getQueryData<StandingApproval[]>(active);
      client.setQueryData<StandingApproval[]>(active, (items) => items?.filter((item) => item.id !== id));
      return { before };
    },
    onError: (_error, _id, context) => client.setQueryData(active, context?.before),
    onSettled: () => client.invalidateQueries({ queryKey: keys.standingAny(org) }),
  });
}

// Notifications

export const useNotifications = (org: string) =>
  useQuery({ queryKey: keys.notifications(org), queryFn: () => served(fleet.notifications(org)) });

/**
 * Each change saves at once and shows at once. If the server refuses it, the screen goes back to
 * what was saved before, and the error says nothing changed.
 */
export function useSaveNotifications(org: string) {
  const client = useQueryClient();
  const key = keys.notifications(org);
  return useMutation({
    mutationFn: (body: NotificationSettings) => fleet.saveNotifications(org, body),
    onMutate: async (body) => {
      await client.cancelQueries({ queryKey: key });
      const before = client.getQueryData<NotificationSettings>(key);
      client.setQueryData(key, body);
      return { before };
    },
    onError: (_error, _body, context) => client.setQueryData(key, context?.before),
    onSuccess: (data) => client.setQueryData(key, data),
  });
}

export function useTestNotification(org: string) {
  return useMutation({ mutationFn: () => fleet.testNotification(org) });
}

// Phone push

export const usePushConfig = (org: string) =>
  useQuery({ queryKey: keys.pushConfig(org), queryFn: () => fleet.pushConfig(org), staleTime: 5 * 60_000 });

export const usePushDevices = (org: string) =>
  useQuery({ queryKey: keys.pushDevices(org), queryFn: () => fleet.pushDevices(org) });

export function useRemovePushDevice(org: string) {
  const client = useQueryClient();
  const key = keys.pushDevices(org);
  return useMutation({
    mutationFn: (id: string) => fleet.removePushDevice(org, id),
    onMutate: async (id) => {
      await client.cancelQueries({ queryKey: key });
      const before = client.getQueryData<PushDevice[]>(key);
      client.setQueryData<PushDevice[]>(key, (items) => items?.filter((item) => item.id !== id));
      return { before };
    },
    onError: (_error, _id, context) => client.setQueryData(key, context?.before),
    onSettled: () => client.invalidateQueries({ queryKey: key }),
  });
}

export function useTestPush(org: string) {
  return useMutation({ mutationFn: () => fleet.testPush(org), meta: { inline: true } });
}

/** A Review link's approval. Not retried: a 410 is the answer, not a hiccup. */
export const useReview = (org: string, id: string, e: string, s: string) =>
  useQuery({ queryKey: keys.review(org, id, e, s), queryFn: () => fleet.review(org, id, e, s), retry: false });

// Webhooks

export const useWebhooks = (org: string) =>
  useQuery({ queryKey: keys.webhooks(org), queryFn: () => served(fleet.webhooks(org)) });

export const useWebhookDeliveries = (org: string, id: string | null) =>
  useQuery({
    queryKey: keys.webhookDeliveries(org, id ?? ''),
    queryFn: () => fleet.webhookDeliveries(org, id ?? ''),
    enabled: id !== null,
    // A test lands in a moment, so the log looks again while it is open.
    refetchInterval: 4_000,
  });

export function useCreateWebhook(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (body: { url: string; events: WebhookEvent[] }) => fleet.createWebhook(org, body),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.webhooks(org) }),
  });
}

/** Turning one on or off, or changing its events, shows at once and rolls back on a refusal. */
export function useUpdateWebhook(org: string) {
  const client = useQueryClient();
  const key = keys.webhooks(org);
  return useMutation({
    mutationFn: ({ id, ...body }: { id: string; enabled?: boolean; events?: WebhookEvent[] }) => fleet.updateWebhook(org, id, body),
    onMutate: async ({ id, ...body }) => {
      await client.cancelQueries({ queryKey: key });
      const before = client.getQueryData<Webhook[]>(key);
      client.setQueryData<Webhook[]>(key, (items) => items?.map((item) => (item.id === id ? { ...item, ...body } : item)));
      return { before };
    },
    onError: (_error, _body, context) => client.setQueryData(key, context?.before),
    onSuccess: (data) => client.setQueryData<Webhook[]>(key, (items) => items?.map((item) => (item.id === data.id ? data : item))),
  });
}

export function useDeleteWebhook(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => fleet.deleteWebhook(org, id),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.webhooks(org) }),
  });
}

export function useRotateWebhook(org: string) {
  return useMutation({ meta: { inline: true }, mutationFn: (id: string) => fleet.rotateWebhook(org, id) });
}

export function useTestWebhook(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => fleet.testWebhook(org, id),
    onSuccess: (_data, id) => {
      client.invalidateQueries({ queryKey: keys.webhookDeliveries(org, id) });
      client.invalidateQueries({ queryKey: keys.webhooks(org) });
    },
  });
}

// Retention

/** The owner's export, through the usual step-up. The screen saves the file. */
export function useExportActivity(org: string) {
  return useMutation({
    meta: { inline: true },
    mutationFn: (options: { format: 'csv' | 'ndjson'; start?: string; workspace?: string }) => fleet.exportActivity(org, options),
  });
}

export const useRetention = (org: string) =>
  useQuery({ queryKey: keys.retention(org), queryFn: () => served(fleet.retention(org)), staleTime: 5 * 60_000 });

// Memory and the day plan (the owner in person; see client.ts `memory`). Changes are never optimistic: the
// screen says what changed once the node has done it, and a refusal is shown as the node sent it.

export const useMemoryWorkspaces = (org: string) =>
  useQuery({ queryKey: keys.memoryWorkspaces(org), queryFn: (): Promise<MemoryWorkspaces> => memory.workspaces(org) });

export const useMemoryList = (org: string, filters: { workspace?: string; q?: string; type?: string; trust?: string; unreviewed?: boolean }) =>
  useQuery({
    queryKey: keys.memoryList(org, JSON.stringify(filters)),
    queryFn: (): Promise<MemoryList> => memory.list(org, filters),
    placeholderData: keepPreviousData,
  });

export const useMemoryEntry = (org: string, id: string | null, workspace: string) =>
  useQuery({
    queryKey: keys.memoryEntry(org, id ?? ''),
    queryFn: (): Promise<MemoryDetail> => memory.entry(org, id ?? '', workspace),
    enabled: Boolean(id),
  });

/** After any change to memory: the entry, every list and the counts are read again. */
function useMemoryChanged(org: string) {
  const client = useQueryClient();
  return () => {
    void client.invalidateQueries({ queryKey: ['org', org, 'memory'] });
  };
}

export function useConfirmMemory(org: string) {
  const changed = useMemoryChanged(org);
  return useMutation({
    mutationFn: (body: { id: string; workspace: string; version: number; title?: string; body?: string }) =>
      memory.confirm(org, body.id, { workspace: body.workspace, version: body.version, title: body.title, body: body.body }),
    meta: { inline: true },
    onSuccess: changed,
  });
}

export function useReviewMemory(org: string) {
  const changed = useMemoryChanged(org);
  return useMutation({
    mutationFn: ({ id, workspace }: { id: string; workspace: string }) => memory.review(org, id, workspace),
    meta: { inline: true },
    onSuccess: changed,
  });
}

export function useForgetMemory(org: string) {
  const changed = useMemoryChanged(org);
  return useMutation({
    mutationFn: ({ id, workspace, reason }: { id: string; workspace: string; reason: string }) => memory.forget(org, id, workspace, reason),
    meta: { inline: true },
    onSuccess: changed,
  });
}

/** Today's draft and the state behind it, for one workspace or for all of them. Read, never changed here. */
export const useDayPlan = (org: string, workspace: string) =>
  useQuery({ queryKey: keys.dayPlan(org, workspace), queryFn: (): Promise<DayPlan> => dayPlan.draft(org, workspace || undefined), staleTime: 60_000 });

export const useDayPlanStatus = (org: string, workspace: string) =>
  useQuery({ queryKey: keys.dayPlanStatus(org, workspace), queryFn: (): Promise<PlanStatus> => dayPlan.status(org, workspace || undefined), staleTime: 60_000 });

export const useAccountFiles = (org: string, ws: string, name: string) =>
  useQuery({ queryKey: keys.laneFiles(org, `${ws}/${name}`), queryFn: (): Promise<AccountFiles> => accountFiles.list(org, ws, name) });

export function useUploadAccountFile(org: string, ws: string, name: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ file, fileName }: { file: Blob; fileName: string }) => accountFiles.upload(org, ws, name, file, fileName),
    meta: { inline: true },
    onSuccess: () => void client.invalidateQueries({ queryKey: keys.laneFiles(org, `${ws}/${name}`) }),
  });
}

export function useDeleteAccountFile(org: string, ws: string, name: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => accountFiles.remove(org, ws, name, id),
    meta: { inline: true },
    onSuccess: () => void client.invalidateQueries({ queryKey: keys.laneFiles(org, `${ws}/${name}`) }),
  });
}

/** One file, as the bytes it is, for the person to save. Nothing in the app opens it. */
export function useDownloadAccountFile(org: string, ws: string, name: string) {
  return useMutation({
    mutationFn: (id: string) => accountFiles.download(org, ws, name, id),
    meta: { inline: true },
  });
}

/** The memory as one plain file (JSON lines or Markdown), every workspace or one. Nothing in it is rendered. */
export function useExportMemory(org: string) {
  return useMutation({
    mutationFn: (options: { workspace?: string; format: 'jsonl' | 'md' }) => memory.exportFile(org, options.workspace, options.format),
    meta: { inline: true },
  });
}

// Engagement, surfaces and connectors

/** The engagement day: every account the person may see, on one day (yesterday when `day` is left out). */
export const useEngageDay = (org: string, day?: string) =>
  useQuery({ queryKey: keys.engageDay(org, day), queryFn: () => served(engage.digest(org, day)) });

export const useEngageAccountDay = (org: string, ws: string, name: string, day?: string) =>
  useQuery({
    queryKey: keys.engageAccount(org, ws, name, day),
    queryFn: () => served(engage.account(org, ws, name, day)),
    enabled: ws !== '' && name !== '',
  });

export const useEngageProgram = (org: string, ws: string, name: string) =>
  useQuery({
    queryKey: keys.engageProgram(org, ws, name),
    queryFn: () => served(engage.program(org, ws, name)),
    enabled: ws !== '' && name !== '',
  });

export const useEngageItems = (org: string, ws: string, name: string) =>
  useQuery({
    queryKey: keys.engageItems(org, ws, name),
    queryFn: () => served(engage.items(org, ws, name)),
    enabled: ws !== '' && name !== '',
  });

/** Approving a program changes what runs, so every engagement view refreshes. */
export function useApproveProgram(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: ({ id, version }: { id: string; version?: number }) => engage.approveProgram(org, id, version),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.engage(org) }),
  });
}

/** A daily limit: the screen shows the node's own refusal (above the ceiling) inline. */
export function useSetCap(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (change: { ws: string; name: string; kind: EngageKind; value: number | null }) =>
      engage.setCap(org, change.ws, change.name, change.kind, change.value),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.engage(org) }),
  });
}

export function useClearHolds(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (target: { ws: string; name: string; kind?: EngageKind | null; id?: number | null }) =>
      engage.clearHolds(org, target.ws, target.name, { kind: target.kind ?? null, id: target.id ?? null }),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.engage(org) }),
  });
}

/** Taking a queued item back leaves the list at once and comes back if the node says no. */
export function useWithdrawItem(org: string, ws: string, name: string) {
  const client = useQueryClient();
  const list = keys.engageItems(org, ws, name);
  return useMutation({
    meta: { inline: true },
    mutationFn: (id: string) => engage.withdrawItem(org, id),
    onMutate: async (id) => {
      await client.cancelQueries({ queryKey: list });
      const before = client.getQueryData<EngageItems>(list);
      client.setQueryData<EngageItems>(list, (items) =>
        items ? { ...items, items: items.items.map((item) => (item.id === id ? { ...item, status: 'withdrawn' } : item)) } : items,
      );
      return { before };
    },
    onError: (_error, _id, context) => client.setQueryData(list, context?.before),
    onSettled: () => client.invalidateQueries({ queryKey: keys.engage(org) }),
  });
}

export const useSurfaces = (org: string) =>
  useQuery({ queryKey: keys.surfaces(org), queryFn: () => served(surfaces.list(org)) });

/** One identity an account acts as. A 404 is not "not served": it means nothing is connected to the account yet. */
export const useSurface = (org: string, ws: string, name: string) =>
  useQuery({
    queryKey: keys.surface(org, ws, name),
    queryFn: () => surfaces.get(org, ws, name),
    enabled: ws !== '' && name !== '',
  });

/** Connecting an identity: only the owner, in person, may (the node refuses a token and a member). */
export function useAddSurface(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (body: NewSurface) => surfaces.add(org, body),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.surfaces(org) }),
  });
}

/** Disconnecting one: the node refuses while a schedule still posts through it, and says which. */
export function useRemoveSurface(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: ({ ws, name }: { ws: string; name: string }) => surfaces.remove(org, ws, name),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.surfaces(org) }),
  });
}

export const useConnectors = (org: string) =>
  useQuery({ queryKey: keys.connectors(org), queryFn: () => served(connectors.list(org)) });

export function useRunConnector(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (run: { operation: string; on: string; params?: Record<string, unknown>; idempotency_key?: string }) =>
      connectors.run(org, run.operation, { on: run.on, params: run.params, idempotency_key: run.idempotency_key }),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.connectors(org) }),
  });
}

// ---- Runs, Checks, Brand and VMs (web/src/api/NEW-HOOKS.md lists them with their mock scenarios) ----

/** How often a running run is read again. The node has no run events yet, so its pages poll. */
export const JOB_POLL_MS = 2_000;
export const JOB_IDLE_POLL_MS = 15_000;
/** The most of each stream a page keeps while it follows a run; older output is let go from the top. */
export const JOB_KEEP_CHARS = 2_000_000;

/** A list route that answered "studio is not installed here", with the command that adds it. */
export function isStudioMissing(data: unknown): data is StudioMissing {
  return !!data && typeof data === 'object' && (data as { available?: unknown }).available === false;
}

/** One studio run, image or diff asked of a computer without studio: the node's `unavailable` error. */
export function isUnavailable(error: unknown): boolean {
  return error instanceof ApiError && error.code === 'unavailable';
}

export const useJobs = (org: string, options: { lane?: string; state?: string; limit?: number } = {}, enabled = true) =>
  useQuery({
    queryKey: keys.jobs(org, options.lane, options.state),
    queryFn: () => jobs.list(org, options),
    enabled,
    // No run events on the stream yet: every 2 s while one runs, and a quiet look every 15 s otherwise, so a
    // run an agent starts shows up (the rail counts them). A refused or missing route is not asked again.
    refetchInterval: (query) =>
      query.state.error ? false : query.state.data?.jobs.some((job) => job.state === 'running') ? JOB_POLL_MS : query.state.data?.available === false ? false : JOB_IDLE_POLL_MS,
  });

/** One run, with the first part of its output. Read again while it runs. */
export const useJob = (org: string, id: string, enabled = true) =>
  useQuery({
    queryKey: keys.job(org, id),
    queryFn: () => jobs.get(org, id),
    enabled,
    refetchInterval: (query) => (query.state.data?.state === 'running' ? JOB_POLL_MS : false),
  });

function kept(text: string): [string, boolean] {
  return text.length > JOB_KEEP_CHARS ? [text.slice(text.length - JOB_KEEP_CHARS), true] : [text, false];
}

/**
 * A run's output followed from its start: each read asks for what came after the offsets already held,
 * at once while the computer has more, every JOB_POLL_MS while it runs, and stops when it has ended and
 * all of it is here. `trimmed` says the page let go of the oldest output.
 */
export function useJobFollow(org: string, id: string, enabled = true): JobFollow & { trimmed: boolean } {
  const client = useQueryClient();
  const [state, setState] = useState<JobFollow & { trimmed: boolean }>({
    job: null, stdout: '', stderr: '', done: false, error: null, loading: enabled, trimmed: false,
  });
  useEffect(() => {
    if (!enabled) return undefined;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let out = 0;
    let err = 0;
    let stdout = '';
    let stderr = '';
    let trimmed = false;
    setState({ job: null, stdout: '', stderr: '', done: false, error: null, loading: true, trimmed: false });
    const step = async () => {
      try {
        const { output, ...job } = await jobs.get(org, id, { stdout_offset: out, stderr_offset: err });
        if (stopped) return;
        let cut: boolean;
        [stdout, cut] = kept(stdout + output.stdout);
        trimmed ||= cut;
        [stderr, cut] = kept(stderr + output.stderr);
        trimmed ||= cut;
        out = output.next_stdout_offset;
        err = output.next_stderr_offset;
        const done = job.state !== 'running' && !output.more;
        setState({ job, stdout, stderr, done, error: null, loading: false, trimmed });
        if (done) {
          client.setQueryData(keys.job(org, id), { ...job, output });
          void client.invalidateQueries({ queryKey: keys.jobsAll(org) });
          return;
        }
        timer = setTimeout(step, output.more ? 0 : JOB_POLL_MS);
      } catch (error) {
        if (!stopped) setState((current) => ({ ...current, error, loading: false }));
      }
    };
    void step();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  }, [client, org, id, enabled]);
  return state;
}

export function useCancelJob(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => jobs.cancel(org, id),
    meta: { inline: true },
    onSuccess: (job) => {
      void client.invalidateQueries({ queryKey: keys.jobsAll(org) });
      void client.invalidateQueries({ queryKey: keys.job(org, job.id) });
    },
  });
}

export const useStudioRuns = (org: string, options: { url?: string; profile?: string; limit?: number } = {}, enabled = true) =>
  useQuery({ queryKey: keys.studioRuns(org, options.url, options.profile), queryFn: () => studio.runs(org, options), enabled });

export const useStudioRun = (org: string, id: string, enabled = true) =>
  useQuery({ queryKey: keys.studioRun(org, id), queryFn: () => studio.run(org, id), enabled, retry: (count, error) => !isUnavailable(error) && count < 2 });

export const useStudioBaseline = (org: string, url: string, profile?: string, enabled = true) =>
  useQuery({
    queryKey: keys.studioBaseline(org, url, profile),
    queryFn: () => studio.baseline(org, url, profile),
    enabled: enabled && !!url,
    retry: (count, error) => !isUnavailable(error) && count < 2,
  });

/** Two stored captures compared. A diff of two kept captures never changes, so it is kept as long as the page lives. */
export const useStudioDiff = (org: string, before: string, after: string, enabled = true) =>
  useQuery({
    queryKey: keys.studioDiff(org, before, after),
    queryFn: () => studio.diff(org, before, after),
    enabled: enabled && !!before && !!after,
    staleTime: Infinity,
    retry: (count, error) => !isUnavailable(error) && count < 2,
  });

/** `ref` is `workspace/profile`. */
export const useBrandComponents = (org: string, ref: string, enabled = true) =>
  useQuery({
    queryKey: keys.brandComponents(org, ref),
    queryFn: () => {
      const [ws = '', id = ''] = ref.split('/');
      return studio.components(org, ws, id);
    },
    enabled: enabled && ref.includes('/'),
  });

export const useBrandProfiles = (org: string, workspace?: string, enabled = true) =>
  useQuery({ queryKey: keys.brandProfiles(org, workspace), queryFn: () => brand.list(org, workspace), enabled });

/** `ref` is `workspace/profile`. */
export const useBrandProfile = (org: string, ref: string, enabled = true) =>
  useQuery({
    queryKey: keys.brandProfile(org, ref),
    queryFn: () => {
      const [ws = '', id = ''] = ref.split('/');
      return brand.get(org, ws, id);
    },
    enabled: enabled && ref.includes('/'),
  });

/**
 * Brand drafts waiting for the owner, for Needs you. The node refuses anyone else (only the owner decides a
 * draft), and that refusal is read as none waiting for them, so Needs you shows nothing rather than an error.
 */
export const useBrandProposals = (org: string, enabled = true) =>
  useQuery({
    queryKey: keys.brandProposals(org),
    queryFn: async () => {
      try {
        return await brand.proposals(org);
      } catch (error) {
        if (error instanceof ApiError && error.status === 403) return [];
        throw error;
      }
    },
    enabled,
  });

export function useDecideBrand(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ ref, version, approve, note }: { ref: string; version: number; approve: boolean; note?: string }) => {
      const [ws = '', id = ''] = ref.split('/');
      return approve ? brand.approve(org, ws, id, version) : brand.reject(org, ws, id, version, note);
    },
    meta: { inline: true },
    onSuccess: () => client.invalidateQueries({ queryKey: keys.brandAll(org) }),
  });
}

export const useVms = (org: string, enabled = true) => useQuery({ queryKey: keys.vms(org), queryFn: () => vms.list(org), enabled });

/** An environment still has a machine: it is not closed and did not fail. */
export const isEnvOpen = (state: EnvState) => state !== 'closed' && state !== 'failed';
/** States that change on their own within seconds, so a page reads again soon. */
const ENV_MOVING = new Set<EnvState>(['starting', 'closing']);
export const ENV_FAST_POLL_MS = 3_000;
export const ENV_POLL_MS = 15_000;

/**
 * The environments the caller may see (`open` or `all`). No events carry them: the list is read every 3 s
 * while one is starting or closing, and every 15 s otherwise. A refused or missing route is not asked again.
 */
export const useEnvironments = (org: string, state: 'open' | 'all' = 'all', enabled = true) =>
  useQuery({
    queryKey: keys.environments(org, state),
    queryFn: () => environments.list(org, state),
    enabled,
    refetchInterval: (query) =>
      query.state.error ? false : query.state.data?.environments.some((env) => ENV_MOVING.has(env.state)) ? ENV_FAST_POLL_MS : ENV_POLL_MS,
  });

/** One environment, read every 3 s while it has a machine (its calls and commands move), and not after. */
export const useEnvironment = (org: string, id: string, enabled = true) =>
  useQuery({
    queryKey: keys.environment(org, id),
    queryFn: () => environments.get(org, id),
    enabled,
    refetchInterval: (query) => (query.state.error || !query.state.data || !isEnvOpen(query.state.data.state) ? false : ENV_FAST_POLL_MS),
  });

function useEnvSettled(org: string) {
  const client = useQueryClient();
  return (env: EnvSummary) => {
    client.setQueryData<EnvDetail>(keys.environment(org, env.id), (held) => (held ? { ...held, ...env } : held));
    void client.invalidateQueries({ queryKey: keys.environmentsAll(org) });
  };
}

/** Close an environment: the node deletes its machine and browser. 409 `unsaved_work` unless `discard`. */
export function useCloseEnvironment(org: string) {
  const settled = useEnvSettled(org);
  return useMutation({
    mutationFn: ({ id, discard = false }: { id: string; discard?: boolean }) => environments.close(org, id, discard),
    meta: { inline: true },
    onSuccess: settled,
  });
}

/** Give an environment more time before the node closes it. */
export function useExtendEnvironment(org: string) {
  const settled = useEnvSettled(org);
  return useMutation({
    mutationFn: ({ id, minutes }: { id: string; minutes: number }) => environments.extend(org, id, minutes),
    meta: { inline: true },
    onSuccess: settled,
  });
}
