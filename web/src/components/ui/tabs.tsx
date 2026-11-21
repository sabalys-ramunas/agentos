import { Tabs as Primitive, ToggleGroup } from 'radix-ui';
import type { ComponentProps, ReactNode } from 'react';
import { cn } from '@/lib/cn';

export const Tabs = Primitive.Root;
export const TabPanel = Primitive.Content;

/** Tabs: words on one hairline; the selected one is violet (accent.text) with a 2 px violet edge under it. */
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
        'focus-inset -mb-px h-11 border-b-2 border-transparent text-label whitespace-nowrap text-muted',
        'transition-colors duration-(--duration-fast) ease-standard hover:text-fg',
        'data-[state=active]:border-accent data-[state=active]:font-semibold data-[state=active]:text-accent-text',
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
 * by side). The chosen segment is the card on a quiet well, both pills. Arrow keys move between segments.
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
      className={cn('inline-flex gap-px rounded-full border border-line bg-inset p-[3px]', className)}
    >
      {options.map((option) => (
        <ToggleGroup.Item
          key={option.value}
          value={option.value}
          aria-label={option.ariaLabel}
          className={cn(
            'inline-flex items-center gap-2 rounded-full border border-transparent px-3 font-medium whitespace-nowrap text-muted',
            'transition-colors duration-(--duration-fast) ease-standard hover:text-fg',
            'data-[state=on]:border-line data-[state=on]:bg-card data-[state=on]:text-fg data-[state=on]:shadow-lift',
            size === 'sm' ? 'h-8 text-secondary md:h-7' : 'h-8 text-label',
          )}
        >
          {option.label}
        </ToggleGroup.Item>
      ))}
    </ToggleGroup.Root>
  );
}

/**
 * Tabs drawn as a segmented well (the drawings' "Open 3 · Saved 3 · Closed 6", "To decide 25 · Brand 1"): for a
 * page's few big views, each with its count. Same tab semantics and keys as `TabList`; the chosen one is the card.
 */
export function SegmentedTabList({ className, ...props }: ComponentProps<typeof Primitive.List>) {
  return (
    <Primitive.List
      className={cn('scrollbar-none inline-flex max-w-full gap-px overflow-x-auto rounded-full border border-line bg-inset p-[3px]', className)}
      {...props}
    />
  );
}

export function SegmentedTab({ className, count, children, ...props }: ComponentProps<typeof Primitive.Trigger> & { count?: number }) {
  return (
    <Primitive.Trigger
      className={cn(
        'inline-flex h-8 shrink-0 items-center gap-2 rounded-full border border-transparent px-3 text-label whitespace-nowrap text-muted',
        'transition-colors duration-(--duration-fast) ease-standard hover:text-fg',
        'data-[state=active]:border-line data-[state=active]:bg-card data-[state=active]:font-semibold data-[state=active]:text-fg data-[state=active]:shadow-lift',
        className,
      )}
      {...props}
    >
      {children}
      {count !== undefined ? <span className="tabular text-secondary font-normal text-faint">{count}</span> : null}
    </Primitive.Trigger>
  );
}
