import type { StatusLabel, Tone } from '@/lib/labels';
import { cn } from '@/lib/cn';

/** `info` is the old name for live: both draw the agent's violet ring. */
const shape = (tone: Tone) => (tone === 'info' ? 'live' : tone);

/**
 * The status mark: a shape as well as a colour, so it reads in greyscale and for colour-blind
 * readers. Dot fine, diamond needs a look, square failed, a violet ring an agent at work (dashed and
 * empty while it starts), hollow ring idle, a quiet solid dot neutral. Nothing pulses: a page never moves
 * all the time. Hidden from screen readers: a word always sits beside it. `pulse` is accepted and ignored.
 */
export function StatusDot({ tone, starting, className }: { tone: Tone; pulse?: boolean; starting?: boolean; className?: string }) {
  return <span aria-hidden="true" data-tone={shape(tone)} data-dashed={starting ? '' : undefined} className={cn('ui-mark', className)} />;
}

/** The word takes its status colour only when it asks for attention: failed, needs a look, running. */
const word: Record<Tone, string> = {
  ok: 'text-muted',
  warn: 'text-warn',
  bad: 'text-bad',
  live: 'text-live',
  info: 'text-live',
  neutral: 'text-muted',
  idle: 'text-idle',
};

/** As a chip (a table's result column, as drawn): the soft tint of its tone, and the word in the tone's colour. */
const chip: Record<Tone, string> = {
  ok: 'bg-ok-fill text-ok',
  warn: 'bg-warn-fill text-warn',
  bad: 'bg-bad-fill text-bad',
  live: 'bg-live-soft text-live',
  info: 'bg-live-soft text-live',
  neutral: 'bg-inset text-muted',
  idle: 'bg-inset text-muted',
};

/** A mark and a short word. This is the only way status is shown; `chip` sets it on its tint, for a table's rows. */
export function Status({ status, quiet = false, chip: asChip = false, className }: { status: StatusLabel; quiet?: boolean; chip?: boolean; className?: string }) {
  return (
    <span
      data-status={shape(status.tone)}
      className={cn(
        'inline-flex items-center gap-2 text-caption font-medium whitespace-nowrap',
        asChip ? cn('h-6 rounded-full px-2', chip[status.tone]) : quiet ? 'text-muted' : word[status.tone],
        className,
      )}
    >
      <StatusDot tone={status.tone} starting={status.starting} />
      {status.label}
    </span>
  );
}
