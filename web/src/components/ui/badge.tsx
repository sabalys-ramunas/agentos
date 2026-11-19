import type { ButtonHTMLAttributes, HTMLAttributes, ReactNode } from 'react';
import { cn } from '@/lib/cn';
import type { Kind } from './kind';

export type BadgeTone = 'neutral' | 'ok' | 'warn' | 'bad' | 'live' | 'accent';

const soft: Record<BadgeTone, string> = {
  neutral: 'border-line bg-card text-muted',
  ok: 'border-ok-line bg-ok-fill text-ok',
  warn: 'border-warn-line bg-warn-fill text-warn',
  bad: 'border-bad-line bg-bad-fill text-bad',
  live: 'border-live-line bg-live-soft text-fg',
  accent: 'border-accent-line bg-accent-soft text-fg',
};

const solid: Record<BadgeTone, string> = {
  neutral: 'border-transparent bg-fg text-on-primary',
  ok: 'border-transparent bg-ok-dot text-status-ink',
  warn: 'border-transparent bg-warn-dot text-status-ink',
  bad: 'border-transparent bg-bad-dot text-status-ink',
  live: 'border-transparent bg-fg text-on-primary',
  accent: 'border-transparent bg-fg text-on-primary',
};

export interface BadgeProps extends HTMLAttributes<HTMLSpanElement> {
  tone?: BadgeTone;
  /** The solid version, for emphasis. */
  solid?: boolean;
}

/** A pill, 20 px, a soft tint with darker text per tone, or solid for emphasis. */
export function Badge({ className, tone = 'neutral', solid: isSolid = false, ...props }: BadgeProps) {
  return (
    <span
      className={cn(
        'inline-flex h-5 items-center gap-1 rounded-full border px-2 text-caption leading-4 font-medium whitespace-nowrap',
        isSolid ? solid[tone] : soft[tone],
        className,
      )}
      {...props}
    />
  );
}

export type CountTone = 'ink' | 'quiet' | 'warn' | 'bad';

const counts: Record<CountTone, string> = {
  ink: 'bg-fg text-on-primary',
  quiet: 'border border-line text-faint',
  warn: 'border border-warn-line bg-warn-fill text-warn',
  bad: 'border border-bad-line bg-bad-fill text-bad',
};

/** A number in a pill: quiet by default, ink for a count that leads (the rail), amber or red when it asks. */
export function Count({
  value,
  tone = 'quiet',
  className,
  ...props
}: { value: number; tone?: CountTone; className?: string } & HTMLAttributes<HTMLSpanElement>) {
  return (
    <span
      {...props}
      className={cn(
        'inline-flex h-5 min-w-5 items-center justify-center rounded-full px-1 font-mono text-micro tabular',
        counts[tone],
        className,
      )}
    >
      {value}
    </span>
  );
}

export interface ChipProps extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, 'children'> {
  pressed: boolean;
  label: ReactNode;
  count?: number;
  /** A kind's small square, so the chips double as the legend. */
  kind?: Kind;
}

/** A filter chip: a pill that is on or off. Pressed is ink. */
export function Chip({ pressed, label, count, kind, className, type = 'button', ...props }: ChipProps) {
  return (
    <button
      type={type}
      aria-pressed={pressed}
      data-kind={kind}
      className={cn(
        'inline-flex h-8 items-center gap-2 rounded-full border px-3 text-secondary font-medium whitespace-nowrap md:h-7',
        'transition-colors duration-(--duration-fast) ease-standard',
        pressed ? 'border-fg bg-fg text-on-primary' : 'border-line bg-card text-muted hover:border-line-strong hover:text-fg',
        kind && 'pl-2',
        className,
      )}
      {...props}
    >
      {kind ? <span aria-hidden="true" className={cn('ui-kind-square', pressed && 'outline outline-1 outline-on-primary')} /> : null}
      {label}
      {count !== undefined ? <span className={cn('font-mono text-micro tabular', pressed ? 'text-on-primary' : 'text-faint')}>{count}</span> : null}
    </button>
  );
}
