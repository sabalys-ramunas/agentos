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
 */
export const useEnvironments = (org: string, state: 'open' | 'all' = 'all', enabled = true) =>
  useQuery({
    queryKey: keys.environments(org, state),
    queryFn: () => environments.list(org, state),
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
