/** What the live view and the terminal share: the connection notice, the toolbar status and focus handling. */
import { Link } from '@tanstack/react-router';
import { CircleSlash, LogIn, PlugZap, RefreshCw, ServerOff, ShieldCheck } from 'lucide-react';
import { useEffect, useRef } from 'react';
import type { Lane } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Spinner } from '@/components/ui/spinner';
import { StatusDot } from '@/components/ui/status';
import type { Tone } from '@/lib/labels';
import { useNow } from '@/lib/use-now';
import { streamCopy } from './copy';
import type { Phase, StreamKind, StreamState } from './stream';

export interface ScreenProps {
  org: string;
  lane: Lane;
  /** This person holds control, so keys and pointer go to the lane. */
  interactive: boolean;
  /** Who holds it when it is not this person, for the "view only" line. */
  holder: string | null;
  /** This person may take the account over (the owner alone). Without it the "view only" line says so. */
  canTake: boolean;
  expanded: boolean;
  onExpandedChange: (expanded: boolean) => void;
  probe?: () => Promise<'signed_out' | 'denied' | 'missing' | 'away' | null>;
}

/** The line under a live view that is not this person's to type into, in words. Only the owner takes an account over. */
export function viewOnlyWords(holder: string | null, canTake: boolean): string {
  if (holder) return `View only. ${holder} has control`;
  return canTake ? 'View only. Take over to type or click' : 'View only. Only the owner can take over this account.';
}

export const PHASE_TONE: Record<Phase, Tone> = {
  idle: 'idle',
  connecting: 'neutral',
  live: 'ok',
  reconnecting: 'warn',
  away: 'warn',
  waiting: 'warn',
  signed_out: 'bad',
  denied: 'bad',
  missing: 'bad',
  ended: 'idle',
  failed: 'bad',
};

const ICON: Partial<Record<Phase, typeof PlugZap>> = {
  reconnecting: RefreshCw,
  away: ServerOff,
  waiting: ShieldCheck,
  signed_out: LogIn,
  denied: CircleSlash,
  missing: CircleSlash,
  ended: PlugZap,
  failed: PlugZap,
};

/** Covers the screen whenever there is no live picture, and says why in plain words. */
export function StreamNotice({ state, kind, org, node, onRetry }: { state: StreamState; kind: StreamKind; org: string; node: string; onRetry: () => void }) {
  const counting = state.retryAt !== null;
  const now = useNow(counting);
  if (state.phase === 'live' || state.phase === 'idle') return null;
  const copy = streamCopy(state, kind, node, now);
  const Icon = ICON[state.phase];
  return (
    <div
      role="status"
      data-phase={state.phase}
      className="absolute inset-0 z-10 flex animate-fade-in flex-col items-center justify-center bg-subtle p-6 text-center"
    >
      {Icon ? <Icon className="mb-4 size-6 text-faint" /> : <Spinner className="mb-4 size-6 text-faint" />}
      <h3 className="text-14 font-medium">{copy.title}</h3>
      <p className="tabular mt-1 max-w-sm text-14 text-muted">{copy.body}</p>
      {copy.action === 'retry' ? (
        <Button className="mt-4" size="sm" onClick={onRetry}>
          {state.phase === 'ended' ? 'Reconnect' : state.phase === 'failed' ? 'Try again' : 'Try now'}
        </Button>
      ) : copy.action === 'sign_in' ? (
        <Link to="/sign-in" className="mt-4 text-14 text-blue hover:underline">
          Sign in
        </Link>
      ) : copy.action === 'needs_you' ? (
        <div className="mt-4 flex gap-2">
          <Link to="/$org/needs-you" params={{ org }} className="inline-flex h-7 items-center rounded-md border border-line px-3 text-13 font-medium hover:bg-fill-1">
            Open Needs you
          </Link>
          <Button size="sm" onClick={onRetry}>
            Reconnect
          </Button>
        </div>
      ) : null}
    </div>
  );
}

/** The dot and word at the start of the toolbar. */
export function StreamBadge({ state, kind, node }: { state: StreamState; kind: StreamKind; node: string }) {
  const copy = streamCopy(state, kind, node, Date.now());
  return (
    <span className="inline-flex items-center gap-2 text-13 whitespace-nowrap text-muted">
      <StatusDot tone={PHASE_TONE[state.phase]} />
      {state.switching ? 'Switching' : copy.label}
    </span>
  );
}

/**
 * Taking control puts the keyboard in the lane; handing back, or losing control, puts focus
 * back where it was before. Focus is only moved for a change this person made.
 */
export function useLaneFocus(interactive: boolean, live: boolean, focus: () => void, root: React.RefObject<HTMLElement | null>) {
  const before = useRef<HTMLElement | null>(null);
  /** The last thing focused outside the lane. Read when the lane takes focus, because the Take
   * over button is disabled while its request runs, and a browser drops focus from it then. */
  const outside = useRef<HTMLElement | null>(null);
  const focusRef = useRef(focus);
  useEffect(() => {
    focusRef.current = focus;
  });
  useEffect(() => {
    const onFocus = (event: FocusEvent) => {
      const target = event.target;
      if (target instanceof HTMLElement && target !== document.body && !root.current?.contains(target)) outside.current = target;
    };
    document.addEventListener('focusin', onFocus);
    return () => document.removeEventListener('focusin', onFocus);
  }, [root]);
  useEffect(() => {
    if (interactive && live) {
      const active = document.activeElement;
      before.current = active instanceof HTMLElement && active !== document.body && !root.current?.contains(active) ? active : outside.current;
      focusRef.current();
      return;
    }
    const target = before.current;
    const active = document.activeElement;
    // Only when the keyboard was in the lane: someone who moved on keeps their place.
    if (interactive || !target || !(active === document.body || root.current?.contains(active))) return;
    before.current = null;
    // The button that handed back may still be busy for a moment, and a disabled button
    // cannot take focus, so wait for it.
    let tries = 0;
    const restore = () => {
      if (!target.isConnected) return;
      target.focus();
      if (document.activeElement !== target && tries++ < 20) timer = setTimeout(restore, 50);
    };
    let timer = setTimeout(restore, 0);
    return () => clearTimeout(timer);
  }, [interactive, live, root]);
}

/**
 * Shift and Escape leave the lane, so the keyboard is never trapped in it. Plain Escape
 * still reaches the site, which may need it.
 */
export function useLeaveKey(root: React.RefObject<HTMLElement | null>, onLeave: () => void) {
  const leave = useRef(onLeave);
  useEffect(() => {
    leave.current = onLeave;
  });
  useEffect(() => {
    const element = root.current;
    if (!element) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || !event.shiftKey) return;
      event.preventDefault();
      event.stopPropagation();
      leave.current();
    };
    element.addEventListener('keydown', onKey, { capture: true });
    return () => element.removeEventListener('keydown', onKey, { capture: true });
  }, [root]);
}
