import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect } from 'react';
import { fleet } from '../client';
import { served } from '../http';
import { keys } from '../keys';
import type {
  FormKind,
} from '../types';

// Brains, vault, grants

export const useBrains = (org: string) =>
  useQuery({ queryKey: keys.brains(org), queryFn: () => served(fleet.brains(org)) });
export const useVault = (org: string) =>
  useQuery({ queryKey: keys.vault(org), queryFn: () => served(fleet.vault(org)) });
export const useGrants = (org: string) =>
  useQuery({ queryKey: keys.grants(org), queryFn: () => served(fleet.grants(org)) });
/** Only the owner may read the vault's state on the computer, so anyone else passes `enabled: false`. */
export const useVaultStatus = (org: string, enabled = true) =>
  useQuery({ queryKey: keys.vaultStatus(org), queryFn: () => served(fleet.vaultStatus(org)), enabled });

/** Seal the vault. Unlocking is a form on the computer (useStartForm), so the passphrase never reaches the app. */
export function useLockVault(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: () => fleet.lockVault(org),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.vaultStatus(org) });
      void client.invalidateQueries({ queryKey: keys.vault(org) });
    },
  });
}

export function useRemoveVaultEntry(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (id: string) => fleet.removeVaultEntry(org, id),
    onSuccess: () => void client.invalidateQueries({ queryKey: keys.vault(org) }),
  });
}

/** Opens one form on the computer. The app gets the trip back and opens its `form_url` itself. */
export const useStartForm = (org: string) =>
  useMutation({ meta: { inline: true }, mutationFn: (body: { kind: Exclude<FormKind, 'login'>; fields?: Record<string, string> }) => fleet.startForm(org, body) });

/** How far a form got. Asks once a second while it is open; when it is done, the data it changed is read again. */
export function useFormTrip(org: string, id: string | undefined) {
  const client = useQueryClient();
  const query = useQuery({
    queryKey: id ? keys.formTrip(org, id) : ['org', org, 'forms', 'none'],
    queryFn: () => fleet.formTrip(org, id ?? ''),
    enabled: Boolean(id),
    refetchInterval: (state) => (state.state.data?.stage === 'waiting' ? 1_000 : false),
  });
  const done = query.data?.stage === 'done';
  useEffect(() => {
    if (!done) return;
    for (const key of [keys.vault(org), keys.vaultStatus(org), keys.browserDevices(org), keys.proxies(org), keys.workspaces(org)]) {
      void client.invalidateQueries({ queryKey: key });
    }
  }, [client, done, org]);
  return query;
}
