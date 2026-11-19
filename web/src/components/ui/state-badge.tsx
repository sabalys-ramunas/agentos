/**
 * The four states a person reads at once, plus Working now. Shape carries the meaning as well as
 * colour (a check, a person, a no-entry bar, a moon, a bolt), so it reads in greyscale and for
 * colour-blind readers. A word always sits beside the glyph, so the glyph is hidden from screen
 * readers.
 */
import { cn } from '@/lib/cn';
import type { Condition, PersonState } from '@/lib/states';
import { STATE_WORDS } from '@/lib/states';

export type GlyphSize = 14 | 16 | 20 | 24 | 40;

export function StateGlyph({ state, size = 16, className }: { state: PersonState; size?: GlyphSize; className?: string }) {
  return (
    <svg
      viewBox="0 0 20 20"
      width={size}
      height={size}
      aria-hidden="true"
      data-state={state}
      className={cn('inline-block shrink-0 overflow-visible', className)}
    >
      {state === 'good' ? (
        <>
          <circle cx={10} cy={10} r={10} fill="var(--ok-dot)" />
          <path d="M5.75 10.25l2.75 2.75 5.75-6" fill="none" stroke="var(--status-ink)" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" />
        </>
      ) : state === 'waiting' ? (
        <>
          <circle cx={10} cy={10} r={10} fill="var(--wait-dot)" />
          <circle cx={10} cy={7.25} r={2.6} fill="var(--wait-ink)" />
          <path d="M5.25 15.25a4.75 4.25 0 0 1 9.5 0z" fill="var(--wait-ink)" />
        </>
      ) : state === 'stuck' ? (
        <>
          <rect width={20} height={20} rx={5} fill="var(--stuck-dot)" />
          <rect x={4.5} y={8.75} width={11} height={2.5} rx={1.25} fill="var(--status-ink)" />
        </>
      ) : state === 'sleeping' ? (
        <>
          <circle cx={10} cy={10} r={9.25} fill="none" stroke="var(--sleep-dot)" strokeWidth={1.5} />
          <path d="M12.4 5.6a4.6 4.6 0 1 0 2.1 7.6 3.7 3.7 0 0 1-2.1-7.6z" fill="var(--sleep-dot)" />
        </>
      ) : (
        <>
          <circle className="state-ring" cx={10} cy={10} r={10} fill="none" stroke="var(--current)" strokeWidth={1.5} />
          <circle cx={10} cy={10} r={10} fill="var(--current)" />
          <path d="M11 3.75L6 11h3.4l-.9 5.25L14 9h-3.4z" fill="var(--text-inverse)" />
        </>
      )}
    </svg>
  );
}

interface StateBadgeProps {
  state: PersonState;
  /** Overrides the state word, for counts such as "3 for you". */
  label?: string;
  /** The plain sentence, shown after the word when `detail` is on, and as a tooltip otherwise. */
  sentence?: string;
  detail?: boolean;
  size?: GlyphSize;
  className?: string;
}

/** The glyph and the word. The one way a person-facing screen shows state. */
export function StateBadge({ state, label, sentence, detail = false, size = 16, className }: StateBadgeProps) {
  const word = label ?? STATE_WORDS[state];
  return (
    <span
      title={!detail && sentence ? sentence : undefined}
      className={cn('inline-flex min-w-0 items-center gap-2 text-secondary text-fg', className)}
    >
      <StateGlyph state={state} size={size} />
      <span className="min-w-0 truncate">
        <span className="font-medium">{word}</span>
        {detail && sentence ? <span className="text-muted"> · {sentence}</span> : null}
      </span>
    </span>
  );
}

/** A Condition from `lib/states`, as a badge. */
export function ConditionBadge({ condition, ...rest }: { condition: Condition } & Omit<StateBadgeProps, 'state' | 'sentence'>) {
  return <StateBadge state={condition.state} label={condition.label} sentence={condition.sentence} {...rest} />;
}
