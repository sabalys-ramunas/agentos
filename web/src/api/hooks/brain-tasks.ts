import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { brainTasks } from '../client';
import { keys } from '../keys';
import type {
  BrainTaskStatus,
} from '../types';

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
