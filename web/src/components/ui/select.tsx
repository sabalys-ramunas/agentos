import { Check, ChevronDown } from 'lucide-react';
import { Select as Primitive } from 'radix-ui';
import type { Ref } from 'react';
import { cn } from '@/lib/cn';

export interface SelectOption {
  value: string;
  label: string;
  hint?: string;
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
          size === 'sm' ? 'h-8 px-2 text-secondary md:h-7' : 'h-8 px-3 text-body',
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
                className="flex h-8 cursor-pointer items-center justify-between gap-4 rounded-md px-2 text-body text-fg outline-none select-none data-[highlighted]:bg-hover"
              >
                <Primitive.ItemText>{option.label}</Primitive.ItemText>
                <Primitive.ItemIndicator>
                  <Check className="size-4 text-muted" />
                </Primitive.ItemIndicator>
              </Primitive.Item>
            ))}
          </Primitive.Viewport>
        </Primitive.Content>
      </Primitive.Portal>
    </Primitive.Root>
  );
}
