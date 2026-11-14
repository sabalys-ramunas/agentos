import { useCallback, useMemo } from 'react';
import { useMembers, useSession } from '@/api/hooks';
import type { NameOptions, PrincipalLike } from './principal';
import { principalName } from './principal';

/**
 * `principalName` with the org's people filled in, so a person reads as "Demo Owner" or "You"
 * rather than "A person". Members load once per org and are shared with Settings.
 */
export function usePrincipalName(org: string) {
  const user = useSession().data?.user;
  const members = useMembers(org).data;
  const people = useMemo(() => new Map((members ?? []).map((member) => [member.user.id, member.user.name || member.user.email])), [members]);
  const me = useMemo(() => (user ? { id: user.id, name: user.name } : null), [user]);
  return useCallback(
    (principal: PrincipalLike | null | undefined, options: Omit<NameOptions, 'me' | 'people'> = {}) => principalName(principal, { ...options, me, people }),
    [me, people],
  );
}

/** The signed-in person as lane words need them (`me`): "You have control" when it is theirs. Null while the session loads. */
export function useMe(): { id: string; name: string } | null {
  const user = useSession().data?.user;
  return useMemo(() => (user ? { id: user.id, name: user.name } : null), [user]);
}
