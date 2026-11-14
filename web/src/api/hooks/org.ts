import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { orgs } from '../client';
import { keys } from '../keys';
import type {
  Member,
  Role,
} from '../types';

// Org

export const useOrg = (org: string) =>
  useQuery({ queryKey: keys.org(org), queryFn: () => orgs.get(org) });
export const useStatus = (org: string) =>
  useQuery({ queryKey: keys.status(org), queryFn: () => orgs.status(org) });
export const useMembers = (org: string) =>
  useQuery({ queryKey: keys.members(org), queryFn: () => orgs.members(org) });
/** One member, with their devices. Read only when the screen shows them (an admin). */
export const useMember = (org: string, id: string, enabled = true) =>
  useQuery({ queryKey: keys.member(org, id), queryFn: () => orgs.member(org, id), enabled });
/** Ending a device signs that person out there. The members list and their devices both change. */
export function useEndMemberDevice(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ member, device }: { member: string; device: string }) => orgs.endMemberDevice(org, member, device),
    meta: { inline: true },
    onSuccess: (_result, { member }) => {
      void client.invalidateQueries({ queryKey: keys.member(org, member) });
      void client.invalidateQueries({ queryKey: keys.members(org) });
    },
  });
}
export const useInvites = (org: string) =>
  useQuery({ queryKey: keys.invites(org), queryFn: () => orgs.invites(org) });

export function useAudit(org: string) {
  return useInfiniteQuery({
    queryKey: keys.audit(org),
    queryFn: ({ pageParam }) => orgs.audit(org, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_before ?? undefined,
  });
}

export function useCreateOrg() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: orgs.create,
    meta: { inline: true },
    onSuccess: () => client.invalidateQueries({ queryKey: keys.session }),
  });
}

export function useUpdateOrg(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (body: { name?: string; slug?: string }) => orgs.update(org, body),
    onSuccess: (_updated, body) => {
      // After a change of address the old key answers 404, so the caller moves first.
      if (body.slug) return;
      void client.invalidateQueries({ queryKey: keys.org(org) });
      void client.invalidateQueries({ queryKey: keys.session });
    },
  });
}

export function useDeleteOrg(org: string) {
  return useMutation({ mutationFn: (confirm: string) => orgs.remove(org, confirm) });
}

export function useUpdateMember(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ id, ...body }: { id: string; role?: Role; workspaces?: string[] | null; workspace?: string }) =>
      orgs.updateMember(org, id, body),
    onMutate: async ({ id, ...change }) => {
      await client.cancelQueries({ queryKey: keys.members(org) });
      const before = client.getQueryData<Member[]>(keys.members(org));
      client.setQueryData<Member[]>(keys.members(org), (current) =>
        current?.map((item) => {
          if (item.id !== id) return item;
          // A role for one business changes that business only. The person's own top role is what the node settles.
          if (change.workspace !== undefined) {
            const rest = (item.roles_by_workspace ?? []).filter((row) => row.workspace !== change.workspace);
            return change.role ? { ...item, roles_by_workspace: [...rest, { workspace: change.workspace, role: change.role }] } : item;
          }
          return { ...item, ...change };
        }),
      );
      return { before };
    },
    onError: (_error, _vars, context) => client.setQueryData(keys.members(org), context?.before),
    onSettled: () => client.invalidateQueries({ queryKey: keys.members(org) }),
  });
}

export function useRemoveMember(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => orgs.removeMember(org, id),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.members(org) }),
  });
}

export function useCreateInvite(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: { email: string; role: Role; workspaces?: string[] | null }) =>
      orgs.invite(org, body),
    meta: { inline: true },
    onSuccess: () => client.invalidateQueries({ queryKey: keys.invites(org) }),
  });
}

export function useRevokeInvite(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => orgs.revokeInvite(org, id),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.invites(org) }),
  });
}
