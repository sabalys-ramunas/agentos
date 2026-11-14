import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { fleet } from '../client';
import { served } from '../http';
import { keys } from '../keys';
import type {
  ControlRequest,
  LaneSites,
  Limits,
  Pace,
} from '../types';

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
