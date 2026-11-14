import { Check, Minus } from 'lucide-react';
import { Checkbox as CheckboxPrimitive, Switch as SwitchPrimitive } from 'radix-ui';
import type { ComponentProps } from 'react';
import { cn } from '@/lib/cn';

export function Checkbox({ className, ...props }: ComponentProps<typeof CheckboxPrimitive.Root>) {
  return (
    <CheckboxPrimitive.Root
      className={cn(
        'inline-flex size-4 shrink-0 items-center justify-center rounded-sm border border-control bg-card',
        'transition-colors duration-(--duration-fast) ease-standard hover:border-muted',
        'data-[state=checked]:border-primary data-[state=checked]:bg-primary data-[state=checked]:text-on-primary',
        'data-[state=indeterminate]:border-primary data-[state=indeterminate]:bg-primary data-[state=indeterminate]:text-on-primary',
        'disabled:cursor-not-allowed disabled:opacity-50',
        className,
      )}
      {...props}
    >
      <CheckboxPrimitive.Indicator>
        {props.checked === 'indeterminate' ? <Minus className="size-3" /> : <Check className="size-3" />}
      </CheckboxPrimitive.Indicator>
    </CheckboxPrimitive.Root>
  );
}

export function Switch({ className, ...props }: ComponentProps<typeof SwitchPrimitive.Root>) {
  return (
    <SwitchPrimitive.Root
      className={cn(
        // A phone gets a 32px tap area around the 20px switch; the drawn switch does not change.
        'relative inline-flex h-5 w-8 shrink-0 items-center rounded-full bg-control p-px',
        'before:absolute before:inset-x-0 before:-top-[6px] before:-bottom-[6px] before:content-[""] md:before:hidden',
        'transition-colors duration-(--duration-base) ease-standard data-[state=checked]:bg-primary',
        'disabled:cursor-not-allowed disabled:opacity-50',
        className,
      )}
      {...props}
    >
      <SwitchPrimitive.Thumb className="block size-[18px] rounded-full bg-card shadow-lift transition-transform duration-(--duration-base) ease-standard data-[state=checked]:translate-x-3" />
    </SwitchPrimitive.Root>
  );
}
