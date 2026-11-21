/**
 * Choices as cards (web/DESIGN.md, Forms). `ChoiceGroup` is one answer from a few, each a card with a title
 * and a line under it: real radios, so Tab enters the group once and the arrow keys move the answer. The
 * chosen card has a violet edge and a violet dot, never a tinted fill. `CheckCard` is the same card for
 * a yes or no of its own, one of several that can all be on.
 */
import { Check } from 'lucide-react';
import { Checkbox as CheckboxPrimitive, RadioGroup } from 'radix-ui';
import { useId } from 'react';
import type { ReactNode } from 'react';
import { cn } from '@/lib/cn';

const card =
  'group relative flex w-full min-w-0 items-start gap-3 rounded-xl border border-line bg-card p-4 text-left shadow-lift ' +
  'transition-[border-color,background-color] duration-(--duration-fast) ease-standard ' +
  'hover:border-line-strong disabled:cursor-not-allowed disabled:opacity-70 disabled:shadow-none disabled:hover:border-line ' +
  // Chosen: the violet edge and a soft violet ring around it (an outline, so studio sees it).
  'data-[state=checked]:border-accent data-[state=checked]:outline-3 data-[state=checked]:outline-offset-0 ' +
  'data-[state=checked]:outline-halo-accent';

interface ChoiceGroupProps<T extends string> {
  /** The question, read to a screen reader; shown as a label when `showLabel` is set. */
  label: string;
  showLabel?: boolean;
  value: T | null;
  onValueChange: (value: T) => void;
  children: ReactNode;
  /** Cards side by side from a tablet up: 2 or 3 to a row. One column on a phone. */
  columns?: 1 | 2 | 3;
  className?: string;
}

export function ChoiceGroup<T extends string>({ label, showLabel = false, value, onValueChange, children, columns = 1, className }: ChoiceGroupProps<T>) {
  const id = useId();
  return (
    <div className={cn('grid gap-2', className)}>
      {showLabel ? (
        <p id={id} className="text-label text-fg">
          {label}
        </p>
      ) : null}
      <RadioGroup.Root
        aria-labelledby={showLabel ? id : undefined}
        aria-label={showLabel ? undefined : label}
        value={value ?? ''}
        onValueChange={(next) => onValueChange(next as T)}
        className={cn('grid gap-3', columns === 2 && 'sm:grid-cols-2', columns === 3 && 'sm:grid-cols-2 lg:grid-cols-3')}
      >
        {children}
      </RadioGroup.Root>
    </div>
  );
}

interface ChoiceProps {
  value: string;
  title: ReactNode;
  description?: ReactNode;
  /** A small mark before the words: an icon in a tile, a brand mark. */
  icon?: ReactNode;
  /** Something that cannot be chosen now (dimmed to 70%). The description or badge should say why. */
  disabled?: boolean;
  /** "Works today", "Coming soon", "Recommended": beside the icon, or after the title. */
  badge?: ReactNode;
  /** `stacked`: the icon and the radio on the top line, the words under them (cards in a grid). */
  layout?: 'row' | 'stacked';
  className?: string;
}

export function Choice({ value, title, description, icon, disabled, badge, layout = 'row', className }: ChoiceProps) {
  if (layout === 'stacked') {
    return (
      <RadioGroup.Item value={value} disabled={disabled} className={cn(card, 'flex-col gap-3', className)}>
        <span className="flex w-full items-start gap-3">
          {icon ? <span className="shrink-0">{icon}</span> : null}
          {badge ? <span className="flex h-8 items-center">{badge}</span> : null}
          <span className="ml-auto flex h-8 items-center">
            <Dot />
          </span>
        </span>
        <span className="grid min-w-0 gap-1">
          <span className={cn('text-card-title', disabled ? 'text-muted' : 'text-fg')}>{title}</span>
          {description ? <span className="text-secondary text-muted">{description}</span> : null}
        </span>
      </RadioGroup.Item>
    );
  }
  return (
    <RadioGroup.Item value={value} disabled={disabled} className={cn(card, className)}>
      {icon ? <span className="shrink-0">{icon}</span> : null}
      <span className="grid min-w-0 flex-1 gap-1">
        <span className={cn('flex flex-wrap items-center gap-2 text-label', disabled ? 'text-muted' : 'text-fg')}>
          {title}
          {badge}
        </span>
        {description ? <span className="text-secondary text-muted">{description}</span> : null}
      </span>
      <span className="flex h-5 shrink-0 items-center">
        <Dot />
      </span>
    </RadioGroup.Item>
  );
}

/** The radio's drawn dot: an empty ring, or violet with a white centre when chosen. */
function Dot() {
  return (
    <span
      aria-hidden
      className={cn(
        'grid size-5 place-items-center rounded-full border border-control bg-card transition-colors duration-(--duration-fast)',
        'group-hover:border-muted group-data-[state=checked]:border-accent group-data-[state=checked]:bg-accent',
      )}
    >
      <RadioGroup.Indicator className="size-2 rounded-full bg-on-primary" />
    </span>
  );
}

interface CheckCardProps {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  title: ReactNode;
  description?: ReactNode;
  icon?: ReactNode;
  disabled?: boolean;
  className?: string;
}

export function CheckCard({ checked, onCheckedChange, title, description, icon, disabled, className }: CheckCardProps) {
  return (
    <CheckboxPrimitive.Root checked={checked} onCheckedChange={(next) => onCheckedChange(next === true)} disabled={disabled} className={cn(card, className)}>
      {icon ? <span className="shrink-0">{icon}</span> : null}
      <span className="grid min-w-0 flex-1 gap-1">
        <span className={cn('text-label', disabled ? 'text-muted' : 'text-fg')}>{title}</span>
        {description ? <span className="text-secondary text-muted">{description}</span> : null}
      </span>
      <span aria-hidden className="flex h-5 shrink-0 items-center">
        <span
          className={cn(
            'grid size-4 place-items-center rounded-sm border border-control bg-card text-on-primary transition-colors duration-(--duration-fast)',
            'group-data-[state=checked]:border-accent group-data-[state=checked]:bg-accent',
          )}
        >
          <CheckboxPrimitive.Indicator>
            <Check className="size-3" strokeWidth={3} />
          </CheckboxPrimitive.Indicator>
        </span>
      </span>
    </CheckboxPrimitive.Root>
  );
}

interface RadioRowProps<T extends string> {
  /** The question, shown on the left from a tablet up and above the answers on a phone. */
  label: string;
  value: T;
  onValueChange: (value: T) => void;
  options: readonly { value: T; label: string }[];
  className?: string;
}

/**
 * A short question with a few short answers, side by side (web/DESIGN.md, Forms): "Format: CSV, NDJSON".
 * Real radios, so the arrow keys move the answer. More than four answers, or answers that need a line of
 * their own, are a `ChoiceGroup` or a `Select`.
 */
export function RadioRow<T extends string>({ label, value, onValueChange, options, className }: RadioRowProps<T>) {
  const id = useId();
  return (
    <div className={cn('grid gap-2 sm:grid-cols-[minmax(0,6rem)_minmax(0,1fr)] sm:items-center sm:gap-4', className)}>
      <span id={id} className="text-label text-muted">
        {label}
      </span>
      <RadioGroup.Root aria-labelledby={id} value={value} onValueChange={(next) => onValueChange(next as T)} className="flex flex-wrap gap-x-5 gap-y-2">
        {options.map((option) => (
          <RadioGroup.Item
            key={option.value}
            value={option.value}
            className="group -mx-1 inline-flex h-8 items-center gap-2 rounded-md px-1 text-body text-fg"
          >
            <span
              aria-hidden
              className={cn(
                'grid size-4 place-items-center rounded-full border border-control bg-card transition-colors duration-(--duration-fast)',
                'group-hover:border-muted group-data-[state=checked]:border-accent group-data-[state=checked]:bg-accent',
              )}
            >
              <RadioGroup.Indicator className="size-1.5 rounded-full bg-on-primary" />
            </span>
            {option.label}
          </RadioGroup.Item>
        ))}
      </RadioGroup.Root>
    </div>
  );
}
