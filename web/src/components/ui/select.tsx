import { Check, ChevronDown } from 'lucide-react';
import { Select as Primitive } from 'radix-ui';
import type { Ref } from 'react';
import { cn } from '@/lib/cn';

export interface SelectOption {
  value: string;
  label: string;
  /** A quiet word after the label in the list ("Offline"), and in the item's name for a screen reader. */
  hint?: string;
  /** Listed, so a person sees it is there, but not chosen (its hint says why). */
  disabled?: boolean;
}

interface SelectProps {
  value: string;
  onValueChange: (value: string) => void;
  options: SelectOption[];
  placeholder?: string;
  disabled?: boolean;
  className?: string;
  id?: string;
  size?: 'sm' | 'md';
  'aria-label'?: string;
  'aria-describedby'?: string;
  'aria-invalid'?: boolean;
  'aria-required'?: boolean;
  ref?: Ref<HTMLButtonElement>;
}

export function Select({ value, onValueChange, options, placeholder, disabled, className, size = 'md', ref, ...aria }: SelectProps) {
  return (
    <Primitive.Root value={value} onValueChange={onValueChange} disabled={disabled}>
      <Primitive.Trigger
        ref={ref}
        className={cn(
          'inline-flex w-full items-center justify-between gap-2 rounded-md border border-line-strong bg-card text-left text-fg',
          'transition-colors duration-(--duration-fast) ease-standard hover:border-control',
          'data-[placeholder]:text-faint disabled:cursor-not-allowed disabled:border-line disabled:bg-inset disabled:text-disabled',
          'aria-invalid:border-bad-dot data-[state=open]:border-(--focus-ring)',
          size === 'sm' ? 'h-8 px-3 text-secondary' : 'h-10 px-3 text-body',
          className,
        )}
        {...aria}
      >
        <span className="truncate">
          <Primitive.Value placeholder={placeholder} />
        </span>
        <Primitive.Icon>
          <ChevronDown className="size-4 text-faint" />
        </Primitive.Icon>
      </Primitive.Trigger>
      <Primitive.Portal>
        <Primitive.Content
          position="popper"
          sideOffset={4}
          className={cn(
            'z-50 max-h-80 min-w-(--radix-select-trigger-width) overflow-hidden rounded-lg border border-line bg-raised shadow-popover',
            'data-[state=open]:animate-pop-in data-[state=closed]:animate-pop-out',
          )}
        >
          <Primitive.Viewport className="p-1">
            {options.map((option) => (
              <Primitive.Item
                key={option.value}
                value={option.value}
                disabled={option.disabled}
                className="flex h-9 cursor-pointer items-center justify-between gap-4 rounded-sm px-2 text-body text-fg outline-none select-none data-[disabled]:cursor-not-allowed data-[disabled]:text-muted data-[highlighted]:bg-hover data-[state=checked]:text-accent-text"
              >
                <span className="flex min-w-0 items-baseline gap-2">
                  <Primitive.ItemText>{option.label}</Primitive.ItemText>
                  {option.hint ? <span className="truncate text-secondary text-faint">{option.hint}</span> : null}
                </span>
                <Primitive.ItemIndicator>
                  <Check className="size-4 text-accent-text" />
                </Primitive.ItemIndicator>
              </Primitive.Item>
            ))}
          </Primitive.Viewport>
        </Primitive.Content>
      </Primitive.Portal>
    </Primitive.Root>
  );
}
