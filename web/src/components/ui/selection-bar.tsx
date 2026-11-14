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
 * The ink bar over a table while rows are selected: how many, what can be done, and Clear (Esc).
 * It rises in, and Esc anywhere clears the selection.
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
  if (count === 0) return null;
  return (
    <div
      role="toolbar"
      aria-label="Selection"
      className={cn(
        'flex animate-selection-rise flex-wrap items-center gap-2 bg-inverse py-2 pr-3 pl-5 text-secondary text-on-inverse',
        className,
      )}
    >
      <span aria-live="polite" className="mr-auto font-medium">
        {count} {count === 1 ? noun[0] : noun[1]} selected
      </span>
      {actions}
      <Button variant="inverse" size="sm" onClick={onClear} icon={<X className="size-4" />} shortcut="Esc">
        Clear
      </Button>
    </div>
  );
}
