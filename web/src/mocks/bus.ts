import type { LiveEvent } from '@/api/types';

type Listener = (event: LiveEvent) => void;

const listeners = new Map<string, Set<Listener>>();

export function subscribe(org: string, listener: Listener) {
  const set = listeners.get(org) ?? new Set();
  set.add(listener);
  listeners.set(org, set);
  return () => set.delete(listener);
}

export function emit(org: string, event: LiveEvent) {
  listeners.get(org)?.forEach((listener) => listener(event));
}
