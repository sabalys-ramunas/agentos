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
        // Checked is the violet (accent.fill), with a white tick.
        'data-[state=checked]:border-accent data-[state=checked]:bg-accent data-[state=checked]:text-on-primary',
        'data-[state=indeterminate]:border-accent data-[state=indeterminate]:bg-accent data-[state=indeterminate]:text-on-primary',
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
        // Dawn: 38 by 22, off is the control grey (3:1, WCAG 1.4.11), on is the violet. A phone gets a
        // 34px tap area around it; the drawn switch does not change.
        'relative inline-flex h-[22px] w-[38px] shrink-0 items-center rounded-full bg-control p-[2px]',
        'before:absolute before:inset-x-0 before:-top-[6px] before:-bottom-[6px] before:content-[""] md:before:hidden',
        'transition-colors duration-(--duration-base) ease-standard data-[state=checked]:bg-accent',
        'disabled:cursor-not-allowed disabled:opacity-50',
        className,
      )}
      {...props}
    >
      <SwitchPrimitive.Thumb className="block size-[18px] rounded-full bg-[var(--control-thumb)] shadow-[var(--shadow-thumb)] transition-transform duration-(--duration-base) ease-standard data-[state=checked]:translate-x-4" />
    </SwitchPrimitive.Root>
  );
}
