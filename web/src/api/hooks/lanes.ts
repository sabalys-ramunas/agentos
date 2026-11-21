import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { fleet } from '../client';
import { served } from '../http';
import { keys } from '../keys';
import type {
  Lane,
  Readiness,
} from '../types';

// Workspaces and lanes

export const useWorkspaces = (org: string) =>
  useQuery({ queryKey: keys.workspaces(org), queryFn: () => fleet.workspaces(org) });

export const useLane = (org: string, ws: string, name: string, enabled = true) =>
  useQuery({ queryKey: keys.lane(org, `${ws}/${name}`), queryFn: () => fleet.lane(org, ws, name), enabled });

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

// Logins

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
