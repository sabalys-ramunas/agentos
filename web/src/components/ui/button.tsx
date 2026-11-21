import { Slot } from 'radix-ui';
import type { ButtonHTMLAttributes, ReactNode, Ref } from 'react';
import { cn } from '@/lib/cn';
import { Spinner } from './spinner';

const base =
  'inline-flex shrink-0 select-none items-center justify-center whitespace-nowrap font-medium tracking-[-0.006em] ' +
  'transition-[background-color,border-color,color,opacity,box-shadow] duration-(--duration-fast) ease-standard ' +
  'off:cursor-not-allowed off:opacity-100';

/*
 * Dawn buttons (web/DESIGN.md, Buttons): every button is a pill. The primary is the violet pill with
 * white words, once per screen. The secondary is the card with a firm hairline and the lift, and is the
 * default. Ghost has no edge. Danger is a secondary with a red word, never a red fill. Inverse sits on an
 * ink bar (the selection bar). Glass sits on the Dawn picture or the stage.
 */
const variants = {
  primary:
    'bg-primary text-on-primary shadow-[var(--shadow-primary)] hover:bg-primary-hover active:bg-primary-hover ' +
    'off:bg-pressed off:text-disabled off:shadow-none',
  secondary:
    'border border-line-strong bg-card text-fg shadow-lift hover:border-control hover:bg-inset active:bg-pressed ' +
    'off:border-line off:bg-card off:text-disabled off:shadow-none',
  ghost: 'text-muted hover:bg-hover hover:text-fg active:bg-pressed off:bg-transparent off:text-disabled',
  danger:
    'border border-line-strong bg-card text-bad shadow-lift hover:border-bad-line hover:bg-bad-fill active:bg-pressed ' +
    'off:border-line off:bg-card off:text-disabled off:shadow-none',
  // For a solid ink surface: a hairline in the secondary ink, and a fill one step toward the bar.
  inverse:
    'border border-inverse-line bg-transparent text-on-inverse hover:bg-inverse-hover active:bg-inverse-pressed',
  // On the Dawn picture or the dark stage: white words on dark glass.
  glass:
    'border border-glass-dark-line bg-glass-dark text-side backdrop-blur-md hover:bg-glass-dark-hover off:opacity-60',
  // The one action on the night (the control pill under a live screen): white with the stage's ink.
  light:
    'bg-side text-stage font-semibold hover:bg-night-pill-hover active:bg-night-pill-pressed off:opacity-60',
} as const;

/** While working a button keeps its own look. Grey would say "unavailable", not "on it". */
const busy = {
  primary: 'off:bg-primary off:text-on-primary',
  secondary: 'off:text-fg',
  ghost: 'off:text-fg',
  danger: 'off:text-bad',
  inverse: '',
  glass: '',
  light: '',
} as const;

const sizes = {
  // Every button is a pill. sm is 32 on a phone, where a finger lands on it, and 30 from md up.
  sm: 'h-8 gap-1 rounded-full px-3 text-secondary font-medium md:h-(--control-height-sm)',
  md: 'h-(--control-height-md) gap-2 rounded-full px-4 text-label',
  lg: 'h-(--control-height-lg) gap-2 rounded-full px-5 text-label',
} as const;

const iconSizes = { sm: 'size-8 rounded-full md:size-(--control-height-sm)', md: 'size-(--control-height-md) rounded-full', lg: 'size-10 rounded-full' } as const;

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
            variant === 'primary' || variant === 'glass'
              ? 'border-pill-edge-on-primary text-on-primary'
              : variant === 'light'
              ? 'border-pill-edge-on-light text-stage'
              : variant === 'inverse'
              ? 'border-inverse-line text-on-inverse'
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
  variant?: 'ghost' | 'secondary' | 'inverse' | 'glass';
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
