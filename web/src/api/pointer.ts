/**
 * The agent's pointer, from the org's event stream to the live view that draws it. Not cached: a point is
 * news for a moment, so it goes straight to whoever watches that lane's screen now, and nowhere else.
 */
import type { AgentPointerEvent } from './types';

const watchers = new Map<string, Set<(event: AgentPointerEvent) => void>>();

export function emitPointer(event: AgentPointerEvent) {
  if (!event || typeof event.lane !== 'string') return;
  watchers.get(event.lane)?.forEach((watcher) => watcher(event));
}

/** Calls `watcher` with each pointer event of `lane` until the returned function is called. */
export function watchPointer(lane: string, watcher: (event: AgentPointerEvent) => void): () => void {
  const set = watchers.get(lane) ?? new Set();
  set.add(watcher);
  watchers.set(lane, set);
  return () => {
    set.delete(watcher);
    if (!set.size) watchers.delete(lane);
  };
}
