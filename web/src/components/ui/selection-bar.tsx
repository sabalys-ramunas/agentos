import { X } from 'lucide-react';
import { useEffect } from 'react';
import type { ReactNode } from 'react';
import { cn } from '@/lib/cn';
import { Button } from './button';

interface SelectionBarProps {
  count: number;
  /** "run", "runs": the bar says "2 runs selected". */
  noun: [string, string];
  /** Buttons for what can be done to the selection. Use variant="inverse". */
  actions: ReactNode;
  onClear: () => void;
  className?: string;
}

/**
 * The ink bar while rows are selected: how many, what can be done, and Clear (Esc). It floats at the foot of the
 * view (sticky, above the phone's tab bar), so choosing a row never pushes the table down; put it after the table.
 * The count is said through a live region that is always in the page, so the first choice is announced too.
 */
export function SelectionBar({ count, noun, actions, onClear, className }: SelectionBarProps) {
  useEffect(() => {
    if (count === 0) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !event.defaultPrevented) onClear();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [count, onClear]);
  const words = `${count} ${count === 1 ? noun[0] : noun[1]} selected`;
  return (
    <>
      <span aria-live="polite" className="sr-only">
        {count ? words : ''}
      </span>
      {count ? (
        <div
          role="toolbar"
          aria-label="Selection"
          className={cn(
            'sticky bottom-[calc(var(--tabbar-height)+env(safe-area-inset-bottom)+16px)] z-20 flex animate-selection-rise flex-wrap items-center gap-2 rounded-xl bg-inverse py-2 pr-2 pl-4 text-secondary text-on-inverse shadow-popover md:bottom-6',
            className,
          )}
        >
          <span aria-hidden="true" className="tabular mr-auto font-medium">
            {words}
          </span>
          {actions}
          <Button variant="inverse" size="sm" onClick={onClear} icon={<X className="size-4" />} shortcut="Esc">
            Clear
          </Button>
        </div>
      ) : null}
    </>
  );
}
