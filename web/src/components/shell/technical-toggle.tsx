import { useId } from 'react';
import { cn } from '@/lib/cn';
import { useTechnical } from '@/lib/technical';
import { Tooltip } from '../ui/menu';
import { Switch } from '../ui/toggle';

export const TECHNICAL_HINT = 'Shows ids, logs, costs per run and the terminal.';

/**
 * The per-person "Technical details" switch. In the rail it sits at the foot, its word from the large
 * breakpoint, and its hint as the switch's tooltip and description. As a row it fits a settings card.
 */
export function TechnicalToggle({ variant = 'rail', className }: { variant?: 'rail' | 'row'; className?: string }) {
  const [on, setOn] = useTechnical();
  const id = useId();
  const control = <Switch id={id} aria-label="Technical details" aria-describedby={`${id}-hint`} checked={on} onCheckedChange={setOn} />;
  if (variant === 'row') {
    return (
      <div className={cn('flex items-center justify-between gap-6', className)}>
        <div className="min-w-0">
          <label htmlFor={id} className="text-body">
            Technical details
          </label>
          <p id={`${id}-hint`} className="text-secondary text-faint">
            {TECHNICAL_HINT} Only for you, in this browser.
          </p>
        </div>
        {control}
      </div>
    );
  }
  // In the rail the hint is read with the switch and shown as its tooltip, so the night's foot stays two lines.
  return (
    <div data-technical-toggle className={cn('lg:px-3', className)}>
      <div className="flex h-9 items-center justify-center gap-2 lg:justify-between">
        <label data-rail-label htmlFor={id} className="hidden text-secondary text-muted lg:block">
          Technical details
        </label>
        <Tooltip label={`${on ? 'On' : 'Off'}. ${TECHNICAL_HINT}`} side="right">
          <span className="lg:contents">{control}</span>
        </Tooltip>
      </div>
      <p id={`${id}-hint`} className="sr-only">
        {TECHNICAL_HINT}
      </p>
    </div>
  );
}
