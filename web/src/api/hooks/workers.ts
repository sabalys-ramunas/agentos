import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect } from 'react';
import { fleet } from '../client';
import { reported, served } from '../http';
import { keys } from '../keys';
import type {
  WorkerAccount,
  WorkerJob,
  WorkerLimits,
  WorkerOverview,
} from '../types';
import { pollUnlessNotServed } from './shared';

/** Concept stage. It refreshes often, because the picture on the page follows a thing being done. */
export const useWorkers = (org: string) => useQuery({ queryKey: keys.workers(org), queryFn: () => served(fleet.workers(org)), refetchInterval: pollUnlessNotServed(3_000) });
export const useWorkersOnComputer = (org: string, node: string) => useQuery({ queryKey: keys.workersOnComputer(org, node), queryFn: () => reported(fleet.workersOnComputer(org, node)), refetchInterval: 5_000 });
/** A computer without workers says nothing, and the account page then shows nothing. */
export const useWorkerOnAccount = (org: string, lane: string) => useQuery({ queryKey: keys.workerOnAccount(org, lane), queryFn: () => reported(fleet.workerOnAccount(org, lane)), refetchInterval: 5_000 });
/** Null on an account no worker reaches: the account page then has no row, and Ask me first does not name it. */
export const useWorkerAccount = (org: string, lane: string) => useQuery({ queryKey: keys.workerAccount(org, lane), queryFn: () => reported(fleet.workerAccount(org, lane)) });

/** A person hands the worker a job on one account. What it does then shows where it always does. */
export function useStartWorkerThing(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (body: { lane: string; told: string }) => fleet.startWorkerThing(org, body),
    onSuccess: (thing, { lane }) => {
      client.setQueryData(keys.workerOnAccount(org, lane), thing);
      void client.invalidateQueries({ queryKey: keys.workers(org) });
    },
  });
}

/** One account's switch. Moves at once, and rolls back alone. */
export function useSetWorkerAccount(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: ({ lane, on }: { lane: string; on: boolean }) => fleet.setWorkerAccount(org, lane, on),
    onMutate: async ({ lane, on }) => {
      const key = keys.workerAccount(org, lane);
      await client.cancelQueries({ queryKey: key });
      client.setQueryData<WorkerAccount | null>(key, (current) => current && { ...current, on });
    },
    onSettled: () => client.invalidateQueries({ queryKey: keys.workers(org) }),
  });
}

/** A switch moves at once in every view of it, and rolls back alone. */
export function useSetWorkerPlace(org: string) {
  const client = useQueryClient();
  const key = keys.workers(org);
  return useMutation({
    meta: { inline: true },
    mutationFn: ({ node, on }: { node: string; on: boolean }) => fleet.setWorkerPlace(org, node, on),
    onMutate: async ({ node, on }) => {
      await client.cancelQueries({ queryKey: key });
      client.setQueryData<WorkerOverview>(key, (current) => current && { ...current, places: current.places.map((item) => (item.node === node ? { ...item, worker: on ? 'on' : 'off' } : item)) });
    },
    onSuccess: (data) => client.setQueryData(key, data),
    onError: () => client.invalidateQueries({ queryKey: key }),
    onSettled: () => client.invalidateQueries({ queryKey: keys.workers(org) }),
  });
}

export function useSetWorkerJob(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: ({ job, brain }: { job: WorkerJob; brain: string }) => fleet.setWorkerJob(org, job, brain),
    onSuccess: (data) => client.setQueryData(keys.workers(org), data),
  });
}

export function useSetWorkerLimits(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (limits: Partial<WorkerLimits>) => fleet.setWorkerLimits(org, limits),
    onSuccess: (data) => client.setQueryData(keys.workers(org), data),
  });
}

export const useStartHandover = (org: string) => useMutation({ meta: { inline: true }, mutationFn: (body: { service: string; computer: string }) => fleet.startHandover(org, body) });

/** Where a key has got to, as the computer tells it. Stops asking once it is over, and what it changed is read again. */
export function useHandover(org: string, id: string | undefined) {
  const client = useQueryClient();
  const query = useQuery({
    queryKey: keys.handover(org, id ?? ''),
    queryFn: () => fleet.handover(org, id ?? ''),
    enabled: Boolean(id),
    refetchInterval: (state) => (state.state.data?.stage === 'done' || state.state.data?.stage === 'failed' ? false : 1_000),
  });
  const done = query.data?.stage === 'done';
  useEffect(() => {
    if (!done) return;
    void client.invalidateQueries({ queryKey: keys.brains(org) });
    void client.invalidateQueries({ queryKey: keys.workers(org) });
  }, [client, done, org]);
  return query;
}

export const useHandoverFilled = (org: string) => useMutation({ meta: { inline: true }, mutationFn: (id: string) => fleet.handoverFilled(org, id) });

export function useSetBrainOrder(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (order: string[]) => fleet.setBrainOrder(org, order),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.brains(org) }),
  });
}
