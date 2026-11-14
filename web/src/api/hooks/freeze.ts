import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { fleet } from '../client';
import { served } from '../http';
import { keys } from '../keys';
import type {
  FreezeScope,
} from '../types';

// Freeze (break glass). Never optimistic: the screen says it is frozen once the node has done it.

/** What is frozen here. A computer without the freeze service answers `available: false`. */
export const useFreezeStatus = (org: string) =>
  useQuery({ queryKey: keys.freeze(org), queryFn: () => served(fleet.freeze(org)) });

export function useFreezeNow(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (body: { scope: Exclude<FreezeScope, 'mesh'>; target?: string; reason: string }) => fleet.freezeNow(org, body),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.freeze(org) });
      void client.invalidateQueries({ queryKey: keys.status(org) });
    },
  });
}

/** Lifting restores nothing. The owner asks for it in person, so this is only ever called from an owner's screen. */
export function useUnfreeze(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (body: { scope: FreezeScope; target?: string }) => fleet.unfreeze(org, body),
    onSuccess: () => void client.invalidateQueries({ queryKey: keys.freeze(org) }),
  });
}
