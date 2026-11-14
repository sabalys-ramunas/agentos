import { Slot } from 'radix-ui';
import type { ButtonHTMLAttributes, ReactNode, Ref } from 'react';
import { cn } from '@/lib/cn';
import { Spinner } from './spinner';

const base =
  'inline-flex shrink-0 select-none items-center justify-center whitespace-nowrap font-medium tracking-[-0.006em] ' +
  'transition-[background-color,border-color,color,opacity,box-shadow] duration-(--duration-fast) ease-standard ' +
  'off:cursor-not-allowed off:opacity-100';

/*
 * Mono buttons (web/DESIGN.md, Buttons). The primary is ink, never a hue, and the one primary on a
 * screen. The secondary is the card with a firm hairline and the lift. Ghost has no edge. Danger is a
 * secondary with a red word. Inverse sits on an ink bar (the selection bar).
 */
const variants = {
  primary:
    'bg-primary text-on-primary hover:bg-primary-hover active:bg-primary-hover ' +
    'off:bg-pressed off:text-disabled',
  secondary:
    'border border-line-strong bg-card text-fg shadow-lift hover:border-control hover:bg-inset active:bg-pressed ' +
    'off:border-line off:bg-card off:text-disabled off:shadow-none',
  ghost: 'text-muted hover:bg-hover hover:text-fg active:bg-pressed off:bg-transparent off:text-disabled',
  danger:
    'border border-line-strong bg-card text-bad shadow-lift hover:border-bad-line hover:bg-bad-fill active:bg-pressed ' +
    'off:border-line off:bg-card off:text-disabled off:shadow-none',
  // For a solid ink surface: a hairline in the secondary ink, and a fill one step toward the bar.
  inverse:
    'border border-[color-mix(in_srgb,var(--text-inverse)_35%,transparent)] bg-transparent text-on-inverse ' +
    'hover:bg-[color-mix(in_srgb,var(--text-inverse)_14%,transparent)] active:bg-[color-mix(in_srgb,var(--text-inverse)_22%,transparent)]',
} as const;

/** While working a button keeps its own look. Grey would say "unavailable", not "on it". */
const busy = {
  primary: 'off:bg-primary off:text-on-primary',
  secondary: 'off:text-fg',
  ghost: 'off:text-fg',
  danger: 'off:text-bad',
  inverse: '',
} as const;

const sizes = {
  // 32px on a phone, where a finger lands on it; 28px from md up, where the page is denser.
  sm: 'h-8 gap-1 rounded-md px-2 text-13 md:h-7',
  md: 'h-8 gap-2 rounded-md px-3 text-14',
  lg: 'h-10 gap-2 rounded-md px-4 text-14',
} as const;

const iconSizes = { sm: 'size-8 rounded-md md:size-7', md: 'size-8 rounded-md', lg: 'size-10 rounded-md' } as const;

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: keyof typeof variants;
  size?: keyof typeof sizes;
  loading?: boolean;
  icon?: ReactNode;
  /** Render the child element (a router link) with button styling. */
  asChild?: boolean;
  /** The key that does the same thing, shown inside the button from md up ("A" to approve). */
  shortcut?: string;
  ref?: Ref<HTMLButtonElement>;
}

export function Button({
  variant = 'secondary',
  size = 'md',
  loading = false,
  icon,
  asChild = false,
  shortcut,
  className,
  children,
  disabled,
  type = 'button',
  onClick,
  ...props
}: ButtonProps) {
  const classes = cn(base, variants[variant], loading && busy[variant], sizes[size], className);
  if (asChild) {
    return (
      <Slot.Root className={classes} {...props}>
        {children}
      </Slot.Root>
    );
  }
  return (
    // Busy keeps focus: `aria-disabled` and the click ignored, never `disabled` (see `off` in app.css).
    <button
      type={type}
      disabled={disabled}
      aria-keyshortcuts={shortcut}
      aria-disabled={loading || undefined}
      aria-busy={loading || undefined}
      className={classes}
      onClick={(event) => {
        if (loading) {
          // A submit button would still send its form.
          event.preventDefault();
          return;
        }
        onClick?.(event);
      }}
      {...props}
    >
      {loading ? <Spinner /> : icon}
      {children}
      {shortcut ? (
        <kbd
          aria-hidden="true"
          className={cn(
            '-mr-1 hidden h-5 min-w-5 items-center justify-center rounded-sm border px-1 font-mono text-micro md:inline-flex',
            variant === 'primary' || variant === 'inverse'
              ? 'border-[color-mix(in_srgb,var(--text-inverse)_40%,transparent)] text-on-inverse'
              : 'border-line text-muted',
          )}
        >
          {shortcut}
        </kbd>
      ) : null}
    </button>
  );
}

export interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** Required: an icon alone says nothing to a screen reader. */
  label: string;
  variant?: 'ghost' | 'secondary' | 'inverse';
  size?: keyof typeof iconSizes;
  ref?: Ref<HTMLButtonElement>;
}

export function IconButton({ label, variant = 'ghost', size = 'md', className, children, type = 'button', ...props }: IconButtonProps) {
  return (
    <button
      type={type}
      aria-label={label}
      className={cn(base, variants[variant], iconSizes[size], className)}
      {...props}
    >
      {children}
    </button>
  );
}
