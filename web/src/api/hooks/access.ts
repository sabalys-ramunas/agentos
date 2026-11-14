import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { fleet, overrides } from '../client';
import { served } from '../http';
import { keys } from '../keys';
import type {
  NewGrant,
  NewStandingApproval,
  StandingApproval,
} from '../types';

/** Safety checks a person turned off. Read only when the screen can show them (Technical details, the owner). */
export const useOverrides = (org: string, enabled = true) =>
  useQuery({ queryKey: keys.overrides(org), queryFn: () => overrides.list(org), enabled });

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
