/**
 * Dragging a step by its handle, with a mouse or a finger. The card follows the pointer on a
 * transform, so nothing else on the board moves until it is dropped; the gaps between steps are
 * the places it can land, and the nearest one lights up. Escape puts it back.
 */
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { PointerEvent as ReactPointerEvent, RefObject } from 'react';
import type { Slot } from '@/lib/routines';

export const slotKey = (slot: Slot) => `${slot.parent ?? ''}|${slot.arm ?? ''}|${slot.index}`;

export function parseSlot(key: string): Slot {
  const [parent = '', arm = '', index = '0'] = key.split('|');
  return { parent: parent || null, arm: (arm || null) as Slot['arm'], index: Number(index) };
}

interface Drag {
  id: string;
  element: HTMLElement;
  x: number;
  y: number;
  moved: boolean;
}

export function useReorder(board: RefObject<HTMLElement | null>, onDrop: (id: string, slot: Slot) => void) {
  const drag = useRef<Drag | null>(null);
  const [dragging, setDragging] = useState<string | null>(null);
  const [over, setOver] = useState<string | null>(null);
  const overRef = useRef<string | null>(null);

  const finish = useCallback((drop: boolean) => {
    const current = drag.current;
    if (!current) return;
    current.element.style.transform = '';
    current.element.style.zIndex = '';
    current.element.style.pointerEvents = '';
    drag.current = null;
    const key = overRef.current;
    overRef.current = null;
    setDragging(null);
    setOver(null);
    if (drop && current.moved && key) onDrop(current.id, parseSlot(key));
  }, [onDrop]);

  useEffect(() => {
    const onMove = (event: PointerEvent) => {
      const current = drag.current;
      if (!current) return;
      const dx = event.clientX - current.x;
      const dy = event.clientY - current.y;
      if (!current.moved && Math.hypot(dx, dy) < 4) return;
      if (!current.moved) {
        current.moved = true;
        current.element.style.zIndex = '30';
        current.element.style.pointerEvents = 'none';
        setDragging(current.id);
      }
      current.element.style.transform = `translate(${dx}px, ${dy}px) rotate(0.6deg)`;
      let best: string | null = null;
      let distance = Infinity;
      for (const gap of board.current?.querySelectorAll<HTMLElement>('[data-slot]') ?? []) {
        if (current.element.contains(gap)) continue;
        const rect = gap.getBoundingClientRect();
        const away = Math.hypot((rect.left + rect.right) / 2 - event.clientX, ((rect.top + rect.bottom) / 2 - event.clientY) * 2);
        if (away < distance) {
          distance = away;
          best = gap.dataset.slot ?? null;
        }
      }
      overRef.current = best;
      setOver(best);
    };
    const onUp = () => finish(true);
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && drag.current) {
        event.stopPropagation();
        finish(false);
      }
    };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    window.addEventListener('pointercancel', onUp);
    window.addEventListener('keydown', onKey, true);
    return () => {
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
      window.removeEventListener('pointercancel', onUp);
      window.removeEventListener('keydown', onKey, true);
    };
  }, [board, finish]);

  const grip = (id: string) => ({
    onPointerDown: (event: ReactPointerEvent<HTMLElement>) => {
      if (event.button !== 0) return;
      const element = (event.currentTarget as HTMLElement).closest<HTMLElement>('[data-step-block]');
      if (!element) return;
      event.preventDefault();
      drag.current = { id, element, x: event.clientX, y: event.clientY, moved: false };
    },
  });

  return { grip, dragging, over };
}

/** Steps slide to their new places rather than jump. Transform only, and never with reduced motion. */
export function useSettle(board: RefObject<HTMLElement | null>, version: unknown) {
  const places = useRef(new Map<string, DOMRect>());
  useLayoutEffect(() => {
    const quiet = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    const next = new Map<string, DOMRect>();
    for (const card of board.current?.querySelectorAll<HTMLElement>('[data-step-card]') ?? []) {
      const id = card.dataset.stepCard ?? '';
      const rect = card.getBoundingClientRect();
      next.set(id, rect);
      const before = places.current.get(id);
      if (!before || quiet || !card.animate) continue;
      const dx = before.left - rect.left;
      const dy = before.top - rect.top;
      if (dx || dy) card.animate([{ transform: `translate(${dx}px, ${dy}px)` }, { transform: 'none' }], { duration: 180, easing: 'cubic-bezier(0.2, 0, 0, 1)' });
    }
    places.current = next;
  }, [board, version]);
}
