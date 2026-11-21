/**
 * Is the window at least this wide? For a part that moves between places by width (an environment's panels
 * leave the night band under 1024 px), so it is rendered once, in the right place, rather than twice with one
 * copy hidden. Read before the first paint, so nothing jumps.
 */
import { useLayoutEffect, useState } from 'react';

export function useMinWidth(px: number): boolean {
  const query = `(min-width: ${px}px)`;
  const [wide, setWide] = useState(() => typeof window !== 'undefined' && window.matchMedia?.(query).matches === true);
  useLayoutEffect(() => {
    const list = window.matchMedia?.(query);
    if (!list) return;
    const update = () => setWide(list.matches);
    update();
    list.addEventListener('change', update);
    return () => list.removeEventListener('change', update);
  }, [query]);
  return wide;
}
