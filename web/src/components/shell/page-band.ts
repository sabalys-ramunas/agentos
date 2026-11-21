/**
 * The night band (web/DESIGN.md, "The night band"): a page whose top is a live screen (one environment) asks
 * for it, and the shell draws the night sky across the whole page column, under the top bar, down to the
 * bottom of the element the page marks. The top bar turns to the night with it.
 *
 *   const band = usePageBand<HTMLDivElement>();
 *   return <div ref={band} className="on-night">...</div>;
 *
 * The band goes when the page does. Its height follows the marked element, measured before paint, so
 * nothing jumps when the page loads.
 */
import { useCallback, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react';

/** The band reaches this far past the bottom of the marked element. */
export const BAND_TAIL = 32;

let current: number | null = null;
const listeners = new Set<() => void>();

function set(next: number | null) {
  if (next === current) return;
  current = next;
  listeners.forEach((listener) => listener());
}

/** The band's height from the top of the page column, in px, or null when the page has none. */
export function useBandHeight(): number | null {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => current,
  );
}

/** Mark the element the band ends under. Pass `on = false` to have none (a state of the page without a screen). */
export function usePageBand<T extends HTMLElement>(on = true) {
  const [element, setElement] = useState<T | null>(null);
  const frame = useRef(0);
  const ref = useCallback((node: T | null) => setElement(node), []);

  useLayoutEffect(() => {
    if (!on || !element) {
      set(null);
      return;
    }
    const column = element.closest<HTMLElement>('[data-page-column]');
    const measure = () => {
      const top = column?.getBoundingClientRect().top ?? 0;
      set(Math.round(element.getBoundingClientRect().bottom - top + BAND_TAIL));
    };
    measure();
    const observer = new ResizeObserver(() => {
      cancelAnimationFrame(frame.current);
      frame.current = requestAnimationFrame(measure);
    });
    observer.observe(element);
    if (column) observer.observe(column);
    return () => {
      observer.disconnect();
      cancelAnimationFrame(frame.current);
      set(null);
    };
  }, [element, on]);

  return ref;
}
