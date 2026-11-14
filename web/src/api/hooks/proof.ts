import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { proof } from '../client';
import { keys } from '../keys';
import type {
  ProofShareInput,
} from '../types';

// Proof (History): the action log, checked and shared (docs/API.md, "Audit").
/** A day's accounts. With no day, the node's today, in its own timezone. */
export const useProofDay = (org: string, day?: string) =>
  useQuery({ queryKey: keys.proofDay(org, day), queryFn: () => proof.dayIndex(org, day) });
export const useProofReplay = (org: string, lane: string, day: string) =>
  useQuery({ queryKey: keys.proofReplay(org, lane, day), queryFn: () => proof.replay(org, lane, day), enabled: lane !== '' && day !== '' });
/** Whether the whole log is intact. The owner alone: the screen passes `enabled` only for the owner. */
export const useProofVerify = (org: string, enabled = true) =>
  useQuery({ queryKey: keys.proofVerify(org), queryFn: () => proof.verify(org), staleTime: 60_000, enabled });
export const useProofShares = (org: string, enabled = true) =>
  useQuery({ queryKey: keys.proofShares(org), queryFn: () => proof.shares(org), enabled });
export function useCreateShare(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: ProofShareInput) => proof.createShare(org, body),
    meta: { inline: true },
    onSuccess: () => client.invalidateQueries({ queryKey: keys.proofShares(org) }),
  });
}
export function useRevokeShare(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => proof.revokeShare(org, id),
    meta: { inline: true },
    onSuccess: () => client.invalidateQueries({ queryKey: keys.proofShares(org) }),
  });
}
/** A read-only link, opened with no sign-in. A bad or ended link answers once; it is not retried. */
export const useProofShared = (token: string) =>
  useQuery({ queryKey: keys.proofShared(token), queryFn: () => proof.shared(token), enabled: token !== '', retry: false });
