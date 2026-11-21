import { Check, Copy } from 'lucide-react';
import { useEffect, useId, useRef, useState } from 'react';
import type { HTMLAttributes, ReactNode } from 'react';
import { cn } from '@/lib/cn';
import { IconButton } from './button';

export function Kbd({ className, ...props }: HTMLAttributes<HTMLElement>) {
  return (
    <kbd
      className={cn(
        'inline-flex h-5 min-w-5 items-center justify-center rounded-sm border border-line bg-card px-1 font-mono text-micro text-muted',
        className,
      )}
      {...props}
    />
  );
}

export function useCopy() {
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout>>(undefined);
  useEffect(() => () => clearTimeout(timer.current), []);
  const copy = (text: string) => {
    void navigator.clipboard?.writeText(text);
    setCopied(true);
    clearTimeout(timer.current);
    timer.current = setTimeout(() => setCopied(false), 1600);
  };
  return { copied, copy };
}

export function CopyButton({ text, label = 'Copy', size = 'sm' }: { text: string; label?: string; size?: 'sm' | 'md' }) {
  const { copied, copy } = useCopy();
  return (
    <IconButton label={copied ? 'Copied' : label} size={size} onClick={() => copy(text)}>
      {copied ? <Check className="size-4 text-ok" /> : <Copy className="size-4" />}
    </IconButton>
  );
}

/** A command or token with a copy affordance. Wraps rather than scrolls on a phone. */
export function CodeBlock({ code, label, className }: { code: string; label?: string; className?: string }) {
  return (
    <div className={cn('flex items-start gap-2 rounded-lg border border-line bg-inset py-2 pr-2 pl-4', className)}>
      <code className="min-w-0 flex-1 py-1 font-mono text-code text-fg">
        {code.split(/(\s+)/).map((part, index) =>
          part.trim() ? (
            <span key={index} className="inline-block max-w-full [overflow-wrap:anywhere] [text-wrap:balance]">
              {part}
            </span>
          ) : (
            part
          ),
        )}
      </code>
      <CopyButton text={code} label={label ?? 'Copy'} />
    </div>
  );
}

const AVATAR_INITIALS = (name: string) =>
  name
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => part[0]?.toUpperCase() ?? '')
    .join('');

/** Initials in a circle. `me` is the person signed in: Dawn's violet to blue, white initials (4.5:1 on both ends). */
export function Avatar({ name, me = false, className }: { name: string; me?: boolean; className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        'inline-flex size-6 shrink-0 items-center justify-center rounded-full text-micro font-medium',
        me ? 'bg-[linear-gradient(135deg,var(--violet-600),var(--identity-2))] font-semibold text-identity-ink' : 'bg-pressed text-muted',
        className,
      )}
    >
      {AVATAR_INITIALS(name)}
    </span>
  );
}

/** Thin usage bar, 4 px, ink on the chart track. Goes amber then red only when the number actually matters. */
export function Meter({ value, max, label, className }: { value: number; max: number; label: string; className?: string }) {
  const pct = max > 0 ? Math.min(100, Math.round((value / max) * 100)) : 0;
  const tone = pct >= 95 ? 'bg-bad-dot' : pct >= 80 ? 'bg-warn-dot' : 'bg-chart-bar-strong';
  return (
    <div
      role="meter"
      aria-label={label}
      aria-valuenow={value}
      aria-valuemin={0}
      aria-valuemax={max}
      className={cn('h-1 overflow-hidden rounded-full bg-chart-grid', className)}
    >
      <div
        className={cn('h-full rounded-full transition-[width] duration-(--duration-slow) ease-standard', tone)}
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

/** The openajar mark, "Window a", as its app icon: a light tile with the violet a (assets/icon.svg). */
export function Logo({ className }: { className?: string }) {
  const id = useId();
  // The mark's colours are tokens (mark.*, build-tokens.py), so the one source holds them too.
  return (
    <svg viewBox="0 0 64 64" aria-hidden="true" className={cn('size-6', className)}>
      <defs>
        <linearGradient id={`${id}t`} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="var(--mark-tile-top)" />
          <stop offset="1" stopColor="var(--mark-tile-bottom)" />
        </linearGradient>
        <linearGradient id={`${id}a`} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="var(--mark-a-top)" />
          <stop offset="1" stopColor="var(--mark-a-bottom)" />
        </linearGradient>
      </defs>
      <rect x="0.5" y="0.5" width="63" height="63" rx="14" fill={`url(#${id}t)`} stroke="var(--mark-edge)" />
      <path
        fill={`url(#${id}a)`}
        fillRule="evenodd"
        transform="translate(1.5 -0.5) scale(0.5)"
        d="M44 34H98V88C98 92.4 94.4 96 90 96C85.6 96 82 92.4 82 88V87.4C78.4 92.6 72.2 96 64 96H44C33 96 24 87 24 76V54C24 43 33 34 44 34ZM45 53C40.5 53 37 56.5 37 61V74C37 79.5 40.5 83 46 83H64C77 83 85 79 85 70V53Z"
      />
    </svg>
  );
}

/** The mark in one colour (currentColor): the small "Window a", as the desktop's top bar draws it. */
export function MarkGlyph({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 20 20" aria-hidden="true" className={cn('size-4', className)}>
      <path
        fill="currentColor"
        fillRule="evenodd"
        d="M6.6 4.2 H16.8 V14.6 C16.8 15.4 16.2 16 15.4 16 C14.6 16 14 15.4 14 14.6 V14.4 C13.3 15.4 12.1 16 10.6 16 H6.6 C4.6 16 3 14.4 3 12.4 V7.8 C3 5.8 4.6 4.2 6.6 4.2Z M7 7.6 C6 7.6 5.4 8.2 5.4 9.2 V12 C5.4 13 6 13.6 7 13.6 H10.6 C12.9 13.6 14.2 12.8 14.2 11 V7.6Z"
      />
    </svg>
  );
}

/** A label and value row, for details panels and inspect drawers. */
export function Detail({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-4 py-2">
      <dt className="shrink-0 text-secondary text-faint">{label}</dt>
      <dd className="min-w-0 text-right text-secondary break-words text-fg">{children}</dd>
    </div>
  );
}

/**
 * The head of a page: the page title (28/36, 24/32 on a phone), the lede under it in the secondary
 * ink, and the page's actions on the right. An optional eyebrow sits over the title (a kind's word).
 */
export function PageHeader({
  title,
  description,
  actions,
  eyebrow,
}: {
  title: string;
  description?: ReactNode;
  actions?: ReactNode;
  eyebrow?: ReactNode;
}) {
  return (
    <header className="mb-8 grid grid-cols-[minmax(0,1fr)_auto] items-end gap-x-4">
      <div className="grid min-w-0 gap-2">
        {eyebrow ? <div className="min-w-0">{eyebrow}</div> : null}
        <h1 className="text-page-title-compact text-balance [overflow-wrap:anywhere] md:text-page-title">{title}</h1>
      </div>
      {actions ? <div className="flex flex-wrap items-center justify-end gap-2">{actions}</div> : <span />}
      {description ? <p className="col-span-2 mt-2 max-w-[46em] text-body text-muted md:text-lede">{description}</p> : null}
    </header>
  );
}
