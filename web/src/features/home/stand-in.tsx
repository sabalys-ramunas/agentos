/**
 * What an account looks like before its picture arrives, or when there is none: a calm tile in
 * the site's colour with a small drawing of the kind of work it does (a post, an email, an
 * invoice list, a price table, a form), the site's mark, whose account it is and what it did
 * last. Never a grey box. Drawn from tokens, so it follows light and dark.
 */
import type { CSSProperties } from 'react';
import { SiteMark, siteColor } from '@/components/brand/site-mark';
import { cn } from '@/lib/cn';
import { siteOf } from '@/lib/sites';

export type WorkKind = 'post' | 'email' | 'money' | 'table' | 'form' | 'shop' | 'files' | 'shell';

const KINDS: Record<string, WorkKind> = {
  gmail: 'email',
  outlook: 'email',
  mail: 'email',
  mailchimp: 'email',
  xero: 'money',
  quickbooks: 'money',
  stripe: 'money',
  paypal: 'money',
  bank: 'money',
  sheets: 'table',
  notion: 'table',
  calendar: 'table',
  tripadvisor: 'table',
  trustpilot: 'table',
  yelp: 'table',
  gov: 'form',
  booking: 'form',
  airbnb: 'form',
  portal: 'form',
  shopify: 'shop',
  square: 'shop',
  woocommerce: 'shop',
  etsy: 'shop',
  ebay: 'shop',
  drive: 'files',
  dropbox: 'files',
  'notes-sync': 'files',
  shell: 'shell',
  terminal: 'shell',
};

/** The kind of work an account does, from its site. Anything unknown is a web page with posts. */
export function workKind(site: string): WorkKind {
  const key = siteOf(site)?.key ?? '';
  return KINDS[key] ?? 'post';
}

const bar = 'block h-1.5 rounded-full bg-fg/12';

function Body({ kind, accent }: { kind: WorkKind; accent: string }) {
  switch (kind) {
    case 'email':
      return (
        <div className="grid gap-2">
          {[0, 1, 2].map((row) => (
            <div key={row} className="flex items-center gap-2">
              <span className={cn('size-1.5 shrink-0 rounded-full', row === 0 ? '' : 'opacity-0')} style={{ background: accent }} />
              <span className={cn(bar, 'w-1/4', row === 0 && 'bg-fg/25')} />
              <span className={cn(bar, 'flex-1')} />
            </div>
          ))}
        </div>
      );
    case 'money':
      return (
        <div className="grid gap-2">
          {[62, 44, 52].map((width, row) => (
            <div key={row} className="flex items-center gap-2">
              <span className="size-2.5 shrink-0 rounded-[3px] border border-fg/20" />
              <span className={bar} style={{ width: `${width}%` }} />
              <span className={cn(bar, 'ml-auto w-1/5 bg-fg/25')} />
            </div>
          ))}
        </div>
      );
    case 'table':
      return (
        <div className="grid grid-cols-3 gap-px overflow-hidden rounded-sm bg-fg/10">
          {Array.from({ length: 9 }, (_, cell) => (
            <span key={cell} className="flex h-3 items-center bg-bg px-1">
              <span className={cn(bar, 'h-1 w-full', cell < 3 && 'bg-fg/25', cell === 5 && 'bg-transparent')} style={cell === 5 ? { background: accent, opacity: 0.6 } : undefined} />
            </span>
          ))}
        </div>
      );
    case 'form':
      return (
        <div className="grid gap-2">
          {[0, 1].map((row) => (
            <div key={row} className="grid gap-1">
              <span className={cn(bar, 'h-1 w-1/4')} />
              <span className="block h-2.5 rounded-[3px] border border-fg/15" style={row === 1 ? { borderColor: accent } : undefined} />
            </div>
          ))}
        </div>
      );
    case 'shop':
      return (
        <div className="grid gap-2">
          {[0, 1].map((row) => (
            <div key={row} className="flex items-center gap-2">
              <span className="size-4 shrink-0 rounded-[3px] bg-fg/10" />
              <span className="grid flex-1 gap-1">
                <span className={cn(bar, 'h-1 w-3/4')} />
                <span className={cn(bar, 'h-1 w-1/3')} />
              </span>
              <span className="h-2 w-6 rounded-full" style={{ background: accent, opacity: row === 0 ? 0.55 : 0.2 }} />
            </div>
          ))}
        </div>
      );
    case 'files':
      return (
        <div className="grid grid-cols-3 gap-2">
          {[0, 1, 2].map((cell) => (
            <span key={cell} className="relative h-6 rounded-[3px] border border-fg/15">
              <span className="absolute top-0 right-0 size-1.5 rounded-bl-[2px] bg-fg/15" />
            </span>
          ))}
        </div>
      );
    case 'shell':
      return (
        <div className="grid gap-1 font-mono">
          {[40, 70, 25].map((width, row) => (
            <div key={row} className="flex items-center gap-2">
              <span className="text-[8px] leading-none text-fg/40">{row === 2 ? '>' : '$'}</span>
              <span className={bar} style={{ width: `${width}%` }} />
            </div>
          ))}
        </div>
      );
    default:
      return (
        <div className="grid gap-2">
          <span className="block aspect-[16/5] rounded-sm" style={{ background: `linear-gradient(160deg, color-mix(in srgb, ${accent} 30%, transparent), color-mix(in srgb, ${accent} 8%, transparent))` }} />
          <span className={cn(bar, 'w-4/5')} />
        </div>
      );
  }
}

interface StandInProps {
  site: string;
  /** Whose account: "Aster Coffee", "@astercoffee". */
  title?: string | null;
  /** What it did last: "Posted a photo · 4h ago". */
  line?: string | null;
  /** A small square (Today, in pictures) leaves the words to the card under it. */
  compact?: boolean;
  className?: string;
}

export function StandIn({ site, title, line, compact = false, className }: StandInProps) {
  const color = siteColor(site);
  const accent = color ?? 'var(--foreground-subtle)';
  const kind = workKind(site);
  const style: CSSProperties = {
    background: color
      ? `radial-gradient(120% 90% at 50% 0%, color-mix(in srgb, ${color} 14%, var(--background-subtle)), color-mix(in srgb, ${color} 5%, var(--background-subtle)))`
      : 'var(--background-subtle)',
  };
  return (
    <div aria-hidden="true" data-stand-in={kind} style={style} className={cn('absolute inset-0 flex flex-col items-center justify-center overflow-hidden', compact ? 'p-3' : 'px-4 pt-2 pb-9', className)}>
      <div className={cn('w-full rounded-lg border border-fg/8 bg-bg/90 shadow-[0_1px_2px_rgb(0_0_0/0.04),0_8px_24px_-12px_rgb(0_0_0/0.18)]', compact ? 'max-w-[82%] p-2' : 'max-w-[58%] p-3')}>
        <div className="mb-2 flex min-w-0 items-center gap-2">
          <SiteMark site={site} size={compact ? 14 : 16} label={false} />
          {title && !compact ? <span className="min-w-0 truncate text-11 font-medium text-muted">{title}</span> : <span className={cn(bar, 'w-1/3')} />}
        </div>
        <Body kind={kind} accent={accent} />
      </div>
      {line && !compact ? <p className="mt-2 max-w-[86%] truncate text-12 text-muted">{line}</p> : null}
    </div>
  );
}
