import { DropdownMenu as Primitive, Popover as PopoverPrimitive, Tooltip as TooltipPrimitive } from 'radix-ui';
import type { ComponentProps, ReactNode } from 'react';
import { cn } from '@/lib/cn';

const floating =
  'z-50 rounded-lg border border-line bg-raised shadow-popover outline-none ' +
  'data-[state=open]:animate-pop-in data-[state=closed]:animate-pop-out';

export const Menu = Primitive.Root;
export const MenuTrigger = Primitive.Trigger;

export function MenuContent({ className, align = 'start', sideOffset = 4, ...props }: ComponentProps<typeof Primitive.Content>) {
  return (
    <Primitive.Portal>
      <Primitive.Content
        align={align}
        sideOffset={sideOffset}
        collisionPadding={8}
        className={cn(floating, 'min-w-48 p-1', className)}
        {...props}
      />
    </Primitive.Portal>
  );
}

export function MenuItem({ className, ...props }: ComponentProps<typeof Primitive.Item>) {
  return (
    <Primitive.Item
      className={cn(
        'flex h-8 cursor-pointer items-center gap-2 rounded-md px-2 text-body text-fg outline-none select-none',
        'data-[disabled]:cursor-not-allowed data-[disabled]:text-disabled data-[highlighted]:bg-hover',
        className,
      )}
      {...props}
    />
  );
}

export function MenuLabel({ className, ...props }: ComponentProps<typeof Primitive.Label>) {
  return <Primitive.Label className={cn('px-2 py-1 text-caption text-faint', className)} {...props} />;
}

export function MenuSeparator() {
  return <Primitive.Separator className="-mx-1 my-1 h-px bg-line" />;
}

export const Popover = PopoverPrimitive.Root;
export const PopoverTrigger = PopoverPrimitive.Trigger;

export function PopoverContent({ className, align = 'end', sideOffset = 8, ...props }: ComponentProps<typeof PopoverPrimitive.Content>) {
  return (
    <PopoverPrimitive.Portal>
      <PopoverPrimitive.Content
        align={align}
        sideOffset={sideOffset}
        collisionPadding={8}
        className={cn(floating, 'w-80 max-w-[calc(100vw-16px)]', className)}
        {...props}
      />
    </PopoverPrimitive.Portal>
  );
}

export const TooltipProvider = TooltipPrimitive.Provider;

export function Tooltip({ label, children, side = 'top' }: { label: ReactNode; children: ReactNode; side?: 'top' | 'right' | 'bottom' | 'left' }) {
  return (
    <TooltipPrimitive.Root>
      <TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger>
      <TooltipPrimitive.Portal>
        <TooltipPrimitive.Content
          side={side}
          sideOffset={6}
          className="z-70 max-w-80 rounded-md bg-inverse px-2 py-1 text-caption text-on-inverse data-[state=closed]:animate-fade-out data-[state=delayed-open]:animate-fade-in"
        >
          {label}
        </TooltipPrimitive.Content>
      </TooltipPrimitive.Portal>
    </TooltipPrimitive.Root>
  );
}
