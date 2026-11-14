import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { fleet } from '../client';
import type { ActivityFilters } from '../client';
import { served } from '../http';
import { keys } from '../keys';
import type {
  HistoryShow,
} from '../types';

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
