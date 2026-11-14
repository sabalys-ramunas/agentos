import type { StatusLabel, Tone } from '@/lib/labels';
import { cn } from '@/lib/cn';

/** `info` is the old name for live: both draw the ringed ink dot. */
const shape = (tone: Tone) => (tone === 'info' ? 'live' : tone);

/**
 * The status mark: a shape as well as a colour, so it reads in greyscale and for colour-blind
 * readers. Dot fine, diamond needs a look, square failed, ringed dot running (pulsing when `pulse`),
 * hollow ring idle, a quiet solid dot neutral. Hidden from screen readers: a word always sits beside it.
 */
export function StatusDot({ tone, pulse, className }: { tone: Tone; pulse?: boolean; className?: string }) {
  return (
    <span
      aria-hidden="true"
      data-tone={shape(tone)}
      data-pulse={pulse ? '' : undefined}
      className={cn('ui-mark', className)}
    />
  );
}

/** The word takes its status colour only when it asks for attention: failed, needs a look, running. */
const word: Record<Tone, string> = {
  ok: 'text-muted',
  warn: 'text-warn',
  bad: 'text-bad',
  live: 'text-fg',
  info: 'text-fg',
  neutral: 'text-muted',
  idle: 'text-idle',
};

/** A mark and a short word. This is the only way status is shown. */
export function Status({ status, quiet = false, className }: { status: StatusLabel; quiet?: boolean; className?: string }) {
  return (
    <span
      data-status={shape(status.tone)}
      className={cn(
        'inline-flex items-center gap-2 text-12 leading-4 font-medium whitespace-nowrap',
        quiet ? 'text-muted' : word[status.tone],
        className,
      )}
    >
      <StatusDot tone={status.tone} pulse={status.pulse} />
      {status.label}
    </span>
  );
}
