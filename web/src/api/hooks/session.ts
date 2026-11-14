import { queryOptions, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { auth, invites } from '../client';
import { createPasskey } from '@/lib/webauthn';
import { can } from '@/lib/permissions';
import type { Action } from '@/lib/permissions';
import { setCsrfToken } from '../http';
import { keys } from '../keys';
import type {
  Session,
} from '../types';

// Session and auth

export const sessionQuery = queryOptions({
  queryKey: keys.session,
  queryFn: async (): Promise<Session> => {
    const session = await auth.session();
    setCsrfToken(session.csrf);
    return session;
  },
  retry: false,
  staleTime: 60_000,
  // A role can change from another room. Coming back to the tab picks that up.
  refetchOnWindowFocus: true,
});

export const useSession = () => useQuery(sessionQuery);

/** What this person may do in this org, for showing and hiding controls. */
export function useAccess(org: string) {
  const role = useSession().data?.orgs.find((item) => item.slug === org)?.role ?? 'viewer';
  return { role, can: (action: Action) => can(role, action) };
}

export const useProviders = () =>
  useQuery({ queryKey: keys.providers, queryFn: auth.providers, staleTime: Infinity });
export const usePasskeys = () => useQuery({ queryKey: keys.passkeys, queryFn: auth.passkeys });
export const useDevices = () => useQuery({ queryKey: keys.devices, queryFn: auth.devices });
export const useInvitePreview = (token: string) =>
  useQuery({ queryKey: keys.invite(token), queryFn: () => invites.preview(token), retry: false });

export function useSendMagicLink() {
  return useMutation({ mutationFn: auth.sendMagicLink, meta: { inline: true } });
}

export function useVerifyMagicLink() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: auth.verifyMagicLink,
    meta: { inline: true },
    onSuccess: (session) => {
      setCsrfToken(session.csrf);
      client.setQueryData(keys.session, session);
    },
  });
}

export function useSignOut() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: auth.signOut,
    onSuccess: () => {
      setCsrfToken(null);
      client.clear();
    },
  });
}

export function useUpdateMe() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: auth.updateMe,
    onSuccess: () => client.invalidateQueries({ queryKey: keys.session }),
  });
}

export function useRevokeDevice() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: auth.revokeDevice,
    onSuccess: () => client.invalidateQueries({ queryKey: keys.devices }),
  });
}

export function useDeletePasskey() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: auth.deletePasskey,
    onSuccess: () => client.invalidateQueries({ queryKey: keys.passkeys }),
  });
}

export function useAcceptInvite() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: invites.accept,
    meta: { inline: true },
    // Returned, so the caller only navigates once the new org is in the session.
    onSuccess: () => client.invalidateQueries({ queryKey: keys.session }),
  });
}

/** A new person's one click on an invite: account, membership and session together. */
export function useJoinInvite() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ token, name }: { token: string; name?: string }) => invites.join(token, { aup_accepted: true, name }),
    meta: { inline: true },
    onSuccess: (session) => {
      setCsrfToken(session.csrf);
      client.setQueryData(keys.session, session);
    },
  });
}

/** A computer's invite: a passkey made on this device, then the account, membership and session together. */
export function useJoinInviteWithPasskey() {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: async ({ token }: { token: string }) => {
      const options = await invites.passkeyOptions(token);
      const credential = await createPasskey(options);
      return invites.passkeyJoin(token, credential, Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC');
    },
    onSuccess: (session) => {
      setCsrfToken(session.csrf);
      client.setQueryData(keys.session, session);
    },
  });
}
