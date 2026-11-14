import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { fleet } from '../client';
import { keys } from '../keys';
import type {
  SiteAction,
  SiteApi,
  SiteApiDetail,
  SiteKeyInput,
} from '../types';
import { pollUnlessNotServed } from './shared';

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
