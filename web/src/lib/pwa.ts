/**
 * The installable app: the service worker, the browser's install prompt, and the one message the
 * worker sends the panel (open this path). Nothing here caches anything; see public/sw.js.
 */
import { useSyncExternalStore } from 'react';
import { safeNext } from './format';

interface InstallEvent extends Event {
  prompt: () => Promise<void>;
  userChoice: Promise<{ outcome: 'accepted' | 'dismissed' }>;
}

let deferred: InstallEvent | null = null;
let installedNow = false;
const listeners = new Set<() => void>();
const notify = () => listeners.forEach((listener) => listener());

export function isStandalone(): boolean {
  if (typeof window === 'undefined') return false;
  return window.matchMedia('(display-mode: standalone)').matches || (navigator as Navigator & { standalone?: boolean }).standalone === true;
}

/** iPhone and iPad, including an iPad that says it is a Mac. */
export function isIos(): boolean {
  if (typeof navigator === 'undefined') return false;
  return /iPad|iPhone|iPod/.test(navigator.userAgent) || (/Macintosh/.test(navigator.userAgent) && navigator.maxTouchPoints > 1);
}

/** Called once at boot, before anything renders, so the browser's install event is not missed. */
export function startPwa() {
  if (typeof window === 'undefined') return;
  window.addEventListener('beforeinstallprompt', (event) => {
    // Held for a sensible moment instead of the browser's own banner on first load.
    event.preventDefault();
    deferred = event as InstallEvent;
    notify();
  });
  window.addEventListener('appinstalled', () => {
    deferred = null;
    installedNow = true;
    notify();
  });
  if (!('serviceWorker' in navigator)) return;
  // A literal comparison, so a live build keeps only this branch. In mock mode the mock API has
  // already registered its own worker, which includes this one.
  if (import.meta.env.VITE_API_MODE !== 'mock') {
    void navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => undefined);
  }
}

/** A notification asked the panel to open a path. Only a path on this site is followed. */
export function onOpenFromWorker(open: (path: string) => void): () => void {
  if (typeof navigator === 'undefined' || !('serviceWorker' in navigator)) return () => undefined;
  const listener = (event: MessageEvent) => {
    const data = event.data as { type?: string; path?: unknown } | null;
    if (data?.type !== 'openajar-open') return;
    const path = safeNext(data.path);
    if (path) open(path);
  };
  navigator.serviceWorker.addEventListener('message', listener);
  return () => navigator.serviceWorker.removeEventListener('message', listener);
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export interface InstallState {
  /** The browser offers its own install dialog (Chromium). */
  canPrompt: boolean;
  /** Safari on iPhone or iPad, not yet on the Home Screen: installing is a manual step. */
  manual: boolean;
  installed: boolean;
}

let snapshot: InstallState = { canPrompt: false, manual: false, installed: false };
function read(): InstallState {
  const next = {
    canPrompt: deferred !== null,
    manual: deferred === null && isIos() && !isStandalone(),
    installed: installedNow || isStandalone(),
  };
  if (next.canPrompt !== snapshot.canPrompt || next.manual !== snapshot.manual || next.installed !== snapshot.installed) snapshot = next;
  return snapshot;
}

export function useInstall(): InstallState & { install: () => Promise<'accepted' | 'dismissed' | 'unavailable'> } {
  const state = useSyncExternalStore(subscribe, read, () => snapshot);
  const install = async () => {
    const event = deferred;
    if (!event) return 'unavailable' as const;
    await event.prompt();
    const { outcome } = await event.userChoice;
    // The browser offers its prompt once. A second offer comes with the next visit.
    deferred = null;
    notify();
    return outcome;
  };
  return { ...state, install };
}
