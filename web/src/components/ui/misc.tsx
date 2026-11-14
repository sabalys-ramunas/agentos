import { Check, Copy } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
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

export function Avatar({ name, className }: { name: string; className?: string }) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        'inline-flex size-6 shrink-0 items-center justify-center rounded-full bg-fill-2 text-11 font-medium text-muted',
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

export function Logo({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 64 64" aria-hidden="true" className={cn('size-6', className)}>
      <rect width="64" height="64" rx="14" fill="var(--brand)" />
      <path fill="#fff" d="M28.6 16h6.8l12 32h-6.6l-2.8-7.8H26l-2.8 7.8h-6.6l12-32Zm-.8 19h8.4L32 23.2 27.8 35Z" />
    </svg>
  );
}

/** A label and value row, for details panels and inspect drawers. */
export function Detail({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-4 py-2">
      <dt className="shrink-0 text-13 text-faint">{label}</dt>
      <dd className="min-w-0 text-right text-13 break-words text-fg">{children}</dd>
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
      {description ? <p className="col-span-2 mt-2 max-w-[46em] text-14 text-muted md:text-lede">{description}</p> : null}
    </header>
  );
}
