import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { fleet } from '../client';
import { reported, served } from '../http';
import { keys } from '../keys';
import type {
  TopologyWindow,
} from '../types';

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
    queryKey: keys.serviceLogs(org, id, lane),
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
