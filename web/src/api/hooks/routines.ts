import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { fleet } from '../client';
import { served } from '../http';
import { keys } from '../keys';
import type {
  RoutineDefinition,
  NodeSchedule,
  Schedule,
} from '../types';
import { pollUnlessNotServed } from './shared';

// Flows and schedules

export const useFlows = (org: string) =>
  useQuery({ queryKey: keys.flows(org), queryFn: () => served(fleet.flows(org)) });

export const useSchedules = (org: string) =>
  useQuery({ queryKey: keys.schedules(org), queryFn: () => served(fleet.schedules(org)) });

export function useCancelRun(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => fleet.cancelRun(org, id),
    onSuccess: (run) => {
      client.setQueryData(keys.run(org, run.id), run);
      void client.invalidateQueries({ queryKey: keys.activityAll(org) });
      void client.invalidateQueries({ queryKey: keys.historyAll(org) });
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
