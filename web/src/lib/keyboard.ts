import { useEffect, useRef } from 'react';

/**
 * Marks an element whose keys all go somewhere else: a lane's live view or terminal. Nothing
 * typed inside it may fire a panel shortcut, not even with Cmd or Ctrl.
 */
export const LANE_INPUT = 'data-lane-input';

/** True when the keypress is headed into a lane. */
export function isLaneInput(event: KeyboardEvent): boolean {
  const target = event.target;
  return target instanceof Element && target.closest(`[${LANE_INPUT}]`) !== null;
}

/** True when a keypress belongs to whatever has focus, not to a global shortcut. */
export function isTyping(event: KeyboardEvent): boolean {
  const target = event.target;
  if (!(target instanceof HTMLElement)) return false;
  if (isLaneInput(event)) return true;
  const tag = target.tagName;
  if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || target.isContentEditable) return true;
  // A modal owns the keyboard while it is open.
  return Boolean(target.closest('[role="dialog"], [role="menu"], [role="listbox"]'));
}

/** Single-key shortcuts for one screen. Handlers receive nothing and may be replaced freely. */
export function useKeys(map: Record<string, (event: KeyboardEvent) => void>, enabled = true) {
  const latest = useRef(map);
  useEffect(() => {
    latest.current = map;
  });
  useEffect(() => {
    if (!enabled) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.metaKey || event.ctrlKey || event.altKey || isTyping(event)) return;
      const handler = latest.current[event.key];
      if (handler) {
        event.preventDefault();
        handler(event);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [enabled]);
}
