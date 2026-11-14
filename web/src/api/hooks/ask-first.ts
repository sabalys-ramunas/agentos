import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { fleet } from '../client';
import { flowAccountSet, flowSet } from '@/lib/ask-first';
import { served } from '../http';
import { keys } from '../keys';
import { getPasskey } from '@/lib/webauthn';
import type {
  ConfirmationOverlay,
  ConfirmationSetting,
  ConfirmationView,
  Flow,
  LaneAskFirst,
} from '../types';

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
