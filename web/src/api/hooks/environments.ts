import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { environments } from '../client';
import { keys } from '../keys';
import type {
  EnvDetail,
  EnvState,
  EnvSummary,
} from '../types';

/** An environment still has a machine: it is not closed and did not fail. */
export const isEnvOpen = (state: EnvState) => state !== 'closed' && state !== 'failed';
/** States that change on their own within seconds, so a page reads again soon. */
const ENV_MOVING = new Set<EnvState>(['starting', 'closing']);
export const ENV_FAST_POLL_MS = 3_000;
export const ENV_POLL_MS = 15_000;

/**
 * The environments the caller may see (`open` or `all`). No events carry them: the list is read every 3 s
 * while one is starting or closing, and every 15 s otherwise. A refused or missing route is not asked again.
 * `pictures`: the page shows each screen's picture (the list, Home), so the node is asked to keep a recent
 * still of each screen while it is read; the rail's counts and other readers leave it off.
 */
export const useEnvironments = (org: string, state: 'open' | 'all' = 'all', enabled = true, { pictures = false }: { pictures?: boolean } = {}) =>
  useQuery({
    queryKey: pictures ? keys.environmentPictures(org, state) : keys.environments(org, state),
    queryFn: () => environments.list(org, state, pictures),
    enabled,
    refetchInterval: (query) =>
      query.state.error ? false : query.state.data?.environments.some((env) => ENV_MOVING.has(env.state)) ? ENV_FAST_POLL_MS : ENV_POLL_MS,
  });

/** One environment, read every 3 s while it has a machine (its calls and commands move), and not after. */
export const useEnvironment = (org: string, id: string, enabled = true) =>
  useQuery({
    queryKey: keys.environment(org, id),
    queryFn: () => environments.get(org, id),
    enabled,
    refetchInterval: (query) => (query.state.error || !query.state.data || !isEnvOpen(query.state.data.state) ? false : ENV_FAST_POLL_MS),
  });

function useEnvSettled(org: string) {
  const client = useQueryClient();
  return (env: EnvSummary) => {
    client.setQueryData<EnvDetail>(keys.environment(org, env.id), (held) => (held ? { ...held, ...env } : held));
    void client.invalidateQueries({ queryKey: keys.environmentsAll(org) });
  };
}

/** Close an environment: the node deletes its machine and browser. 409 `unsaved_work` unless `discard`. */
export function useCloseEnvironment(org: string) {
  const settled = useEnvSettled(org);
  return useMutation({
    mutationFn: ({ id, discard = false }: { id: string; discard?: boolean }) => environments.close(org, id, discard),
    meta: { inline: true },
    onSuccess: settled,
  });
}

/** Give an environment more time before the node closes it. */
export function useExtendEnvironment(org: string) {
  const settled = useEnvSettled(org);
  return useMutation({
    mutationFn: ({ id, minutes }: { id: string; minutes: number }) => environments.extend(org, id, minutes),
    meta: { inline: true },
    onSuccess: settled,
  });
}

/** Forget a save: the node deletes its snapshot and files, and the list is read again. */
export function useForgetSave(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id }: { id: string }) => environments.forget(org, id),
    meta: { inline: true },
    onSuccess: () => void client.invalidateQueries({ queryKey: keys.environmentsAll(org) }),
  });
}

/** The replay's timeline, read once: it only grows while the environment is open, and the page reads it again on demand. */
export const useEnvReplay = (org: string, id: string, enabled = true) =>
  useQuery({ queryKey: keys.environmentReplay(org, id), queryFn: () => environments.replay(org, id), enabled, staleTime: 30_000 });

/** One checkpoint against the work tree now (the owner's). It changes as the agent works, so it is read again when it is opened again. */
export const useCheckpointDiff = (org: string, id: string, checkpoint: number | null) =>
  useQuery({
    queryKey: keys.checkpointDiff(org, id, String(checkpoint ?? '')),
    queryFn: () => environments.checkpointDiff(org, id, checkpoint ?? 0),
    enabled: checkpoint !== null,
    staleTime: 15_000,
  });

/** The prepared machines and the warm pool, the owner's: not asked again once the node refuses. */
export const usePreparedMachines = (org: string, enabled = true) =>
  useQuery({
    queryKey: keys.preparedMachines(org),
    queryFn: () => environments.prepared(org),
    enabled,
    retry: false,
    refetchInterval: (query) => (query.state.error ? false : 15_000),
  });

/** Resume a save from the app: the environment opens again, held by no chat, and waits for an agent. */
export function useResumeSave(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id }: { id: string }) => environments.resume(org, id),
    meta: { inline: true },
    onSuccess: (env) => {
      void client.invalidateQueries({ queryKey: keys.environmentsAll(org) });
      void client.invalidateQueries({ queryKey: keys.environment(org, env.id) });
    },
  });
}
