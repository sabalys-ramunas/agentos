import { useSyncExternalStore } from 'react';

const NARROW = '(max-width: 1023px)';

/** Below the desk layout: one item at a time, the buttons at the bottom in reach of a thumb. */
export function useNarrow(): boolean {
  return useSyncExternalStore(
    (listener) => {
      const query = window.matchMedia(NARROW);
      query.addEventListener('change', listener);
      return () => query.removeEventListener('change', listener);
    },
    () => window.matchMedia(NARROW).matches,
    () => false,
  );
}
