import { useSyncExternalStore } from 'react';

/** A tiny persisted value with subscribers, for theme and the active workspace. */
export function createStored<T extends string>(key: string, fallback: T) {
  const listeners = new Set<() => void>();
  const read = () => {
    try {
      return (localStorage.getItem(key) as T | null) ?? fallback;
    } catch {
      return fallback;
    }
  };
  return {
    get: read,
    set(value: T) {
      try {
        localStorage.setItem(key, value);
      } catch {
        // Private mode can refuse storage. The choice then lasts for this page only.
      }
      listeners.forEach((listener) => listener());
    },
    use() {
      return useSyncExternalStore(
        (listener) => {
          listeners.add(listener);
          return () => listeners.delete(listener);
        },
        read,
        () => fallback,
      );
    },
  };
}
