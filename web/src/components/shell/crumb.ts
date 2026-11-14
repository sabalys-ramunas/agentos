/**
 * The last part of the top bar's breadcrumb. By default it is the page's id from the address (a
 * run's id, a machine's name), in mono. A page that has a better name calls `usePageCrumb` with it:
 *
 *   usePageCrumb(job ? job.note || job.id : undefined);
 *   usePageCrumb(profile?.name, { mono: false });
 *
 * The crumb is cleared when the page goes away.
 */
import { useEffect, useSyncExternalStore } from 'react';

export interface PageCrumb {
  label: string;
  mono: boolean;
}

let current: PageCrumb | null = null;
const listeners = new Set<() => void>();

function set(next: PageCrumb | null) {
  current = next;
  listeners.forEach((listener) => listener());
}

export function usePageCrumb(label: string | undefined, { mono = true }: { mono?: boolean } = {}) {
  useEffect(() => {
    if (!label) return;
    set({ label, mono });
    return () => set(null);
  }, [label, mono]);
}

export function useCurrentCrumb(): PageCrumb | null {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => current,
  );
}
