/**
 * The "Technical details" switch: one person's choice to see ids, logs, the terminal and costs
 * per run. Off by default for everyone. Kept in this browser per user for now; an account
 * setting can take over later without changing `useTechnical`.
 */
import { useCallback, useSyncExternalStore } from 'react';
import { useSession } from '@/api/hooks';

const PREFIX = 'agentos.technical.';
const listeners = new Set<() => void>();
/** Used only when storage refuses writes. */
const memory = new Map<string, boolean>();

export const technicalKey = (userId: string) => `${PREFIX}${userId}`;

export function readTechnical(userId: string | null | undefined): boolean {
  if (!userId) return false;
  try {
    return localStorage.getItem(technicalKey(userId)) === 'on';
  } catch {
    return false;
  }
}

export function writeTechnical(userId: string, on: boolean) {
  try {
    if (on) localStorage.setItem(technicalKey(userId), 'on');
    else localStorage.removeItem(technicalKey(userId));
  } catch {
    // Private mode can refuse storage. The choice then lasts for this page only.
    memory.set(userId, on);
  }
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  // Another tab of the same person flipping it.
  const onStorage = (event: StorageEvent) => {
    if (event.key?.startsWith(PREFIX)) listener();
  };
  window.addEventListener('storage', onStorage);
  return () => {
    listeners.delete(listener);
    window.removeEventListener('storage', onStorage);
  };
}

/** Whether this person wants technical details, and a setter. Signed out reads as off. */
export function useTechnical(): [boolean, (on: boolean) => void] {
  const userId = useSession().data?.user.id ?? null;
  const on = useSyncExternalStore(
    subscribe,
    () => memory.get(userId ?? '') ?? readTechnical(userId),
    () => false,
  );
  const set = useCallback((next: boolean) => {
    if (userId) writeTechnical(userId, next);
  }, [userId]);
  return [on, set];
}
