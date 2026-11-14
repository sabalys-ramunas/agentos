import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { brand, studio } from '../client';
import { ApiError } from '../http';
import { keys } from '../keys';

/** `ref` is `workspace/profile`. */
export const useBrandComponents = (org: string, ref: string, enabled = true) =>
  useQuery({
    queryKey: keys.brandComponents(org, ref),
    queryFn: () => {
      const [ws = '', id = ''] = ref.split('/');
      return studio.components(org, ws, id);
    },
    enabled: enabled && ref.includes('/'),
  });

export const useBrandProfiles = (org: string, workspace?: string, enabled = true) =>
  useQuery({ queryKey: keys.brandProfiles(org, workspace), queryFn: () => brand.list(org, workspace), enabled });

/** `ref` is `workspace/profile`. */
export const useBrandProfile = (org: string, ref: string, enabled = true) =>
  useQuery({
    queryKey: keys.brandProfile(org, ref),
    queryFn: () => {
      const [ws = '', id = ''] = ref.split('/');
      return brand.get(org, ws, id);
    },
    enabled: enabled && ref.includes('/'),
  });

/**
 * Brand drafts waiting for the owner, for Needs you. The node refuses anyone else (only the owner decides a
 * draft), and that refusal is read as none waiting for them, so Needs you shows nothing rather than an error.
 */
export const useBrandProposals = (org: string, enabled = true) =>
  useQuery({
    queryKey: keys.brandProposals(org),
    queryFn: async () => {
      try {
        return await brand.proposals(org);
      } catch (error) {
        if (error instanceof ApiError && error.status === 403) return [];
        throw error;
      }
    },
    enabled,
  });

export function useDecideBrand(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ ref, version, approve, note }: { ref: string; version: number; approve: boolean; note?: string }) => {
      const [ws = '', id = ''] = ref.split('/');
      return approve ? brand.approve(org, ws, id, version) : brand.reject(org, ws, id, version, note);
    },
    meta: { inline: true },
    onSuccess: () => client.invalidateQueries({ queryKey: keys.brandAll(org) }),
  });
}
