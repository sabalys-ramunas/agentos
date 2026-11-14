/** The small marks of the instrument language: state by shape as well as colour, and site glyphs. */
import type { EntityState } from '@/api/types';
import { cn } from '@/lib/cn';

export type MarkState = EntityState | 'current' | 'person';

export const MARK_WORDS: Record<MarkState, string> = {
  ok: 'Fine',
  current: 'An agent is acting',
  person: 'A person is in control',
  attention: 'Needs a look',
  blocked: 'Blocked',
  idle: 'Idle',
  down: 'Down',
};

/* The status dots: marks need 3:1, so they use the dot colours, not the text ones. In Mono an agent
   acting and a person in control are both ink; their shapes (a ringed dot, a diamond) tell them apart. */
const COLOR: Record<MarkState, string> = {
  ok: 'var(--status-ok-dot)',
  current: 'var(--status-live-dot)',
  person: 'var(--status-live-dot)',
  attention: 'var(--status-warn-dot)',
  blocked: 'var(--status-bad-dot)',
  idle: 'var(--status-idle-dot)',
  down: 'var(--status-bad-dot)',
};

/**
 * Centred on 0,0 and about 8 units wide, so it drops into any SVG. Shape carries the meaning so
 * a colour-blind reader, or a grey print, gets the same map.
 */
export function MarkShape({ state }: { state: MarkState }) {
  const color = COLOR[state];
  switch (state) {
    case 'current':
      return (
        <>
          <circle r={5.5} fill="none" stroke={color} strokeOpacity={0.45} />
          <circle r={2.75} fill={color} />
        </>
      );
    case 'attention':
      return <path d="M0 -4 L4.25 3.25 L-4.25 3.25 Z" fill={color} />;
    case 'blocked':
      return <rect x={-3.25} y={-3.25} width={6.5} height={6.5} rx={1} fill={color} />;
    case 'down':
      return <path d="M-3.25 -3.25 L3.25 3.25 M3.25 -3.25 L-3.25 3.25" stroke={color} strokeWidth={1.75} strokeLinecap="round" fill="none" />;
    case 'idle':
      return <circle r={2.75} fill="none" stroke={color} strokeWidth={1.25} />;
    case 'person':
      return <path d="M0 -4 L4 0 L0 4 L-4 0 Z" fill={color} />;
    default:
      return <circle r={3} fill={color} />;
  }
}

/** The same mark for HTML. Hidden from screen readers, because a word always sits beside it. */
export function StateMark({ state, className }: { state: MarkState; className?: string }) {
  return (
    <svg viewBox="-6 -6 12 12" aria-hidden="true" className={cn('inline-block size-3 shrink-0', className)}>
      <MarkShape state={state} />
    </svg>
  );
}

/** Two letters, like an element in a table. Never a third party's logo. */
export function siteLetters(label: string): string {
  const name = label.replace(/^www\./, '').split('.')[0] ?? label;
  const clean = name.replace(/[^a-zA-Z0-9]/g, '');
  const first = clean.charAt(0).toUpperCase();
  const second = clean.slice(1).match(/[^aeiouAEIOU]/)?.[0] ?? clean.charAt(1);
  return `${first}${second.toLowerCase()}`;
}

export function SiteGlyph({ label, className }: { label: string; className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={cn('inline-flex size-6 shrink-0 items-center justify-center rounded-sm border border-line bg-inset font-mono text-12 font-medium text-fg', className)}
    >
      {siteLetters(label)}
    </span>
  );
}
