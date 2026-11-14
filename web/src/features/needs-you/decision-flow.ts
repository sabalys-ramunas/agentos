/**
 * What happens between a tap and the next item, the same on every screen that decides:
 *
 * 1. The decided card stays where it was for a moment, showing that it is done. There is no
 *    button where the person just tapped, so a double tap lands on nothing.
 * 2. The next item arrives with its buttons off for a moment more (`useArmed`). A second tap, a
 *    held key or a thumb resting on the glass can never decide it (walkthrough bug 3).
 * 3. An undo window runs while the cloud holds the decision (`undo_until`). Undo puts the item
 *    back as it was.
 * 4. Then the real result, from the approval's `outcome`, never a bare "Approved".
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { useDecide, useUndo } from '@/api/hooks';
import type { DecisionOutcome } from '@/api/hooks';
import type { Approval } from '@/api/types';

/** How long a decided card shows that it is done before the next one comes. */
export const DONE_MS = 900;
/** How long the next item's buttons stay off once it is on screen. */
export const ARM_MS = 700;

/**
 * True once the item on screen may be decided. The first item after a decision waits `ARM_MS`,
 * so a tap or a key meant for the item before never lands on it. Moving between items by hand
 * (a click on the list, j and k) is ready at once: nothing was decided under the finger then.
 *
 * `decisions` counts decisions made on this screen.
 */
export function useArmed(id: string | null | undefined, decisions = 0): boolean {
  const current = id ?? null;
  const [seen, setSeen] = useState({ id: current, decisions });
  const [armed, setArmed] = useState<string | null>(current);
  if (current !== seen.id) {
    const afterDecision = decisions !== seen.decisions;
    setSeen({ id: current, decisions });
    if (!afterDecision || seen.id === null) setArmed(current);
  }
  useEffect(() => {
    if (current === null || current === armed) return;
    const timer = setTimeout(() => setArmed(current), ARM_MS);
    return () => clearTimeout(timer);
  }, [current, armed]);
  return current === null || current === armed;
}

export interface Decided {
  key: number;
  items: Approval[];
  decision: 'approve' | 'deny';
  /** What is happening, in words: "Posting to Aster Coffee's Instagram". */
  label: string;
  /** Until when Undo works, as a clock time in ms. Zero when it cannot be undone. */
  undoUntil: number;
  /** The cloud has taken the decision. */
  taken?: boolean;
  undone?: boolean;
  /** Why Undo did not work. */
  undoError?: string;
}

interface RunOptions {
  /** Words for the bar: "Posting to Aster Coffee's Instagram". */
  label: string;
  onSuccess?: (result: DecisionOutcome) => void;
  onError?: (error: unknown) => void;
}

interface FlowOptions {
  /** Undo worked: the items are pending again. */
  onUndone?: (ids: string[]) => void;
}

export function useDecisionFlow(org: string, { onUndone }: FlowOptions = {}) {
  const decide = useDecide(org);
  const undo = useUndo(org);
  const [done, setDone] = useState<{ approval: Approval; decision: 'approve' | 'deny' } | null>(null);
  const [last, setLast] = useState<Decided | null>(null);
  const [decisions, setDecisions] = useState(0);
  const doneTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  useEffect(() => () => clearTimeout(doneTimer.current), []);

  const run = useCallback(
    (items: Approval[], decision: 'approve' | 'deny', { label, onSuccess, onError }: RunOptions) => {
      if (items.length === 0) return;
      const key = Date.now();
      const [first] = items;
      setDecisions((count) => count + 1);
      clearTimeout(doneTimer.current);
      if (items.length === 1 && first) {
        setDone({ approval: first, decision });
        doneTimer.current = setTimeout(() => setDone(null), DONE_MS);
      }
      setLast({ key, items, decision, label, undoUntil: 0 });
      decide.mutate(
        { items, decision },
        {
          onSuccess: (result) => {
            const decided = new Set(result.decided);
            const kept = items.filter((item) => decided.has(item.id));
            // The cloud's own window; an older cloud sends none, and then there is nothing to undo.
            const until = Math.max(0, ...(result.approvals ?? []).map((item) => (item.undo_until ? new Date(item.undo_until).getTime() : 0)));
            setLast((current) => {
              if (current?.key !== key) return current;
              if (kept.length === 0) return null;
              return { ...current, items: kept, undoUntil: until, taken: true };
            });
            onSuccess?.(result);
          },
          onError: (error) => {
            clearTimeout(doneTimer.current);
            setDone(null);
            setLast((current) => (current?.key === key ? null : current));
            onError?.(error);
          },
        },
      );
    },
    [decide],
  );

  const takeBack = useCallback(() => {
    if (!last || last.undone || undo.isPending) return;
    const ids = last.items.map((item) => item.id);
    const key = last.key;
    undo.mutate(ids, {
      onSuccess: () => {
        setLast((current) => (current?.key === key ? { ...current, undone: true } : current));
        clearTimeout(doneTimer.current);
        setDone(null);
        onUndone?.(ids);
      },
      onError: () =>
        setLast((current) => (current?.key === key ? { ...current, undoUntil: 0, undoError: 'Too late to undo: it has already been sent.' } : current)),
    });
  }, [last, undo, onUndone]);

  const dismiss = useCallback(() => setLast(null), []);

  return { run, done, last, decisions, takeBack, dismiss, undoing: undo.isPending, deciding: decide.isPending };
}
