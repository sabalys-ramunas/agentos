/**
 * Nothing waits: calm, and a little proud. A horizon with a half sun and a check, drawn in one
 * line that reads the tokens, so it inverts in dark mode (visual.md P6).
 */
import { Link } from '@tanstack/react-router';
import type { Approval } from '@/api/types';
import { Button } from '@/components/ui/button';
import { DecidedChips } from './outcome';

function Horizon() {
  return (
    <svg viewBox="0 0 240 120" width={240} height={120} aria-hidden="true" className="text-(--border-strong)">
      {/* Rays */}
      <g stroke="currentColor" strokeWidth="1.25" strokeLinecap="round">
        <path d="M120 22v10" />
        <path d="M78 38l6 7" />
        <path d="M162 38l-6 7" />
        <path d="M56 72h10" />
        <path d="M174 72h10" />
      </g>
      {/* The half sun */}
      <path d="M86 98a34 34 0 0 1 68 0" fill="none" stroke="var(--ok-dot)" strokeWidth="1.75" strokeLinecap="round" />
      {/* The check */}
      <path d="M108 80l8 8 16-16" fill="none" stroke="var(--green)" strokeWidth="2.25" strokeLinecap="round" strokeLinejoin="round" />
      {/* The horizon */}
      <path d="M24 98h192" stroke="currentColor" strokeWidth="1.25" strokeLinecap="round" />
      <path d="M96 106h48" stroke="currentColor" strokeWidth="1.25" strokeLinecap="round" opacity="0.6" />
    </svg>
  );
}

export function AllClear({ org, decided, canDecide }: { org: string; decided: Approval[]; canDecide: boolean }) {
  return (
    <section
      aria-label="All clear"
      className="flex flex-col items-center gap-4 rounded-xl border border-line px-6 py-16 text-center"
      style={{ background: 'radial-gradient(60% 50% at 50% 30%, color-mix(in srgb, var(--ok-dot) 10%, transparent), transparent 70%), var(--background-subtle)' }}
    >
      <Horizon />
      <div className="grid max-w-md gap-2">
        <h2 className="text-page-title-compact">All clear</h2>
        <p className="text-body text-muted">Nothing is waiting for you. Your routines carry on by themselves, and you will hear from us when something asks you first.</p>
      </div>
      <DecidedChips org={org} items={decided} />
      {canDecide ? (
        <Button asChild variant="ghost">
          <Link to="/$org/settings/notifications" params={{ org }}>
            Get told on your phone
          </Link>
        </Button>
      ) : null}
    </section>
  );
}
