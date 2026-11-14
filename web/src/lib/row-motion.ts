import { useEffect, useMemo, useRef, useState } from 'react';

/** How long a leaving row stays, to match the leave animation in app.css. */
const LEAVE_MS = 160;

export type Motion<T> = T & { entering?: boolean; leaving?: boolean };

interface Options {
  /** The pointer is over the list. Nothing may move under it. */
  hold: boolean;
  /** Rows the person just decided. These go at once, even during a hold. */
  decided: ReadonlySet<string>;
}

/**
 * Rows that animate in and out, without moving the row under the pointer. During a hold, new
 * rows wait and departed rows keep their place. Both catch up when the pointer leaves.
 */
export function useRowMotion<T extends { id: string }>(items: T[], { hold, decided }: Options) {
  const [leaving, setLeaving] = useState<ReadonlyMap<string, { item: T; index: number }>>(new Map());
  const [frozen, setFrozen] = useState<T[] | null>(null);
  const previous = useRef(items);
  const seen = useRef<Set<string> | null>(null);
  const timers = useRef(new Set<ReturnType<typeof setTimeout>>());

  useEffect(() => {
    // Only the start and end of a hold matter here.
    setFrozen(hold ? previous.current : null);
  }, [hold]);

  useEffect(() => {
    if (hold) return;
    const ids = new Set(items.map((item) => item.id));
    const gone = previous.current.flatMap((item, index) => (ids.has(item.id) ? [] : [{ item, index }]));
    previous.current = items;
    if (gone.length === 0) return;
    setLeaving((current) => new Map([...current, ...gone.map((entry) => [entry.item.id, entry] as const)]));
    // Not cleared when the list changes again: a row that left must always finish leaving, or
    // it would stay on screen as a ghost beside its own return (a refused decision coming back).
    const timer = setTimeout(() => {
      setLeaving((current) => {
        const next = new Map(current);
        for (const { item } of gone) next.delete(item.id);
        return next;
      });
    }, LEAVE_MS);
    timers.current.add(timer);
  }, [items, hold]);

  useEffect(() => () => timers.current.forEach(clearTimeout), []);

  const rows = useMemo(() => {
    const known = seen.current;
    const present = new Set(items.map((item) => item.id));
    const source = frozen ?? items;
    const shown: Motion<T>[] = frozen
      ? source.filter((item) => !decided.has(item.id)).map((item) => ({ ...item, leaving: !present.has(item.id) }))
      : source.map((item) => ({ ...item, entering: known !== null && !known.has(item.id) }));
    if (!frozen) {
      for (const { item, index } of leaving.values()) {
        // A row that came back is shown once, as itself.
        if (!present.has(item.id)) shown.splice(Math.min(index, shown.length), 0, { ...item, leaving: true });
      }
    }
    return shown;
  }, [items, frozen, leaving, decided]);

  // After the first paint every row counts as seen, so only real arrivals animate.
  useEffect(() => {
    if (!hold) seen.current = new Set(items.map((item) => item.id));
  }, [items, hold]);

  const waiting = frozen ? items.filter((item) => !frozen.some((row) => row.id === item.id)).length : 0;
  return { rows, waiting };
}
