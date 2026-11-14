import { keepPreviousData, useQueries, useQuery } from '@tanstack/react-query';
import { fleet } from '../client';
import { served } from '../http';
import { keys } from '../keys';
import type {
  ActivityRange,
  NodeRange,
} from '../types';

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
