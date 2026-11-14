import { useId } from 'react';
import { cn } from '@/lib/cn';
import { useTechnical } from '@/lib/technical';
import { Tooltip } from '../ui/menu';
import { Switch } from '../ui/toggle';

export const TECHNICAL_HINT = 'Shows ids, logs, costs per run and the terminal.';

/**
 * The per-person "Technical details" switch. In the rail it sits at the foot, with its words from
 * the large breakpoint and as a tooltip on the narrow icon rail. As a row it fits a settings card.
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
  return (
    <div data-technical-toggle className={cn('border-t border-line pt-3 lg:px-2', className)}>
      <div className="flex items-center justify-center gap-2 lg:justify-between">
        <label data-rail-label htmlFor={id} className="hidden text-secondary text-muted lg:block">
          Technical details
        </label>
        <Tooltip label={on ? 'Technical details: on' : 'Technical details: off'} side="right">
          <span className="lg:contents">{control}</span>
        </Tooltip>
      </div>
      <p data-rail-label id={`${id}-hint`} className="mt-1 hidden text-caption text-faint lg:block">
        {TECHNICAL_HINT}
      </p>
    </div>
  );
}
