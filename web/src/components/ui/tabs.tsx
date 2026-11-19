import { Tabs as Primitive, ToggleGroup } from 'radix-ui';
import type { ComponentProps, ReactNode } from 'react';
import { cn } from '@/lib/cn';

export const Tabs = Primitive.Root;
export const TabPanel = Primitive.Content;

/** Tabs: words on one hairline; the selected one is ink with a 2 px ink edge under it. */
export function TabList({ className, ...props }: ComponentProps<typeof Primitive.List>) {
  return (
    <Primitive.List
      className={cn('scrollbar-none flex gap-6 overflow-x-auto border-b border-line', className)}
      {...props}
    />
  );
}

export function Tab({ className, ...props }: ComponentProps<typeof Primitive.Trigger>) {
  return (
    <Primitive.Trigger
      className={cn(
        'focus-inset -mb-px h-10 border-b-2 border-transparent text-label font-medium whitespace-nowrap text-faint',
        'transition-colors duration-(--duration-fast) ease-standard hover:text-fg',
        'data-[state=active]:border-fg data-[state=active]:text-fg',
        className,
      )}
      {...props}
    />
  );
}

export interface SegmentOption<T extends string> {
  value: T;
  label: ReactNode;
  /** A name for an option that is only an icon. */
  ariaLabel?: string;
}

interface SegmentedControlProps<T extends string> {
  value: T;
  onValueChange: (value: T) => void;
  options: SegmentOption<T>[];
  /** What the choice is: "Density". */
  label: string;
  size?: 'sm' | 'md';
  className?: string;
}

/**
 * A segmented control: one of a few views of the same thing (Comfortable or Compact, Slider or Side
 * by side). The chosen segment is the card on a quiet well. Arrow keys move between segments.
 */
export function SegmentedControl<T extends string>({ value, onValueChange, options, label, size = 'sm', className }: SegmentedControlProps<T>) {
  return (
    <ToggleGroup.Root
      type="single"
      value={value}
      onValueChange={(next) => {
        // A segmented control always has a choice: clicking the chosen one keeps it.
        if (next) onValueChange(next as T);
      }}
      aria-label={label}
      // Many choices on a phone scroll sideways inside the control, never the page.
      className={cn('scrollbar-none inline-flex max-w-full gap-px overflow-x-auto rounded-md border border-line bg-hover p-px', className)}
    >
      {options.map((option) => (
        <ToggleGroup.Item
          key={option.value}
          value={option.value}
          aria-label={option.ariaLabel}
          className={cn(
            'inline-flex shrink-0 items-center gap-1 rounded-sm border border-transparent px-2 font-medium whitespace-nowrap text-muted',
            'transition-colors duration-(--duration-fast) ease-standard hover:text-fg',
            'data-[state=on]:border-line data-[state=on]:bg-card data-[state=on]:text-fg data-[state=on]:shadow-lift',
            size === 'sm' ? 'h-8 text-secondary md:h-6' : 'h-8 text-label',
          )}
        >
          {option.label}
        </ToggleGroup.Item>
      ))}
    </ToggleGroup.Root>
  );
}
