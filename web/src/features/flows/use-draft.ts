/**
 * A routine being edited, with undo. Every change is a new definition, so undo is a stack of them.
 * Typing into one field is one step of undo, not a step per letter.
 */
import { useCallback, useRef, useState } from 'react';
import type { RoutineDefinition } from '@/api/types';

interface Stack {
  past: RoutineDefinition[];
  present: RoutineDefinition;
  future: RoutineDefinition[];
}

const LIMIT = 100;
const TYPING_MS = 800;

export function useDraft(start: RoutineDefinition) {
  const [stack, setStack] = useState<Stack>({ past: [], present: start, future: [] });
  const [saved, setSaved] = useState(() => JSON.stringify(start));
  const typing = useRef<{ key: string; at: number } | null>(null);

  const change = useCallback((next: (draft: RoutineDefinition) => RoutineDefinition, key?: string) => {
    const now = Date.now();
    const same = key !== undefined && typing.current?.key === key && now - typing.current.at < TYPING_MS;
    typing.current = key === undefined ? null : { key, at: now };
    setStack((current) => {
      const present = next(current.present);
      if (present === current.present) return current;
      return { past: same ? current.past : [...current.past, current.present].slice(-LIMIT), present, future: [] };
    });
  }, []);

  const undo = useCallback(() => {
    typing.current = null;
    setStack((current) => {
      const present = current.past.at(-1);
      return present ? { past: current.past.slice(0, -1), present, future: [current.present, ...current.future] } : current;
    });
  }, []);

  const redo = useCallback(() => {
    typing.current = null;
    setStack((current) => {
      const [present, ...future] = current.future;
      return present ? { past: [...current.past, current.present], present, future } : current;
    });
  }, []);

  return {
    draft: stack.present,
    change,
    undo,
    redo,
    canUndo: stack.past.length > 0,
    canRedo: stack.future.length > 0,
    dirty: JSON.stringify(stack.present) !== saved,
    markSaved: () => setSaved(JSON.stringify(stack.present)),
  };
}
