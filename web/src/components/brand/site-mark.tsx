/**
 * A site's own mark, to name the site an account is on ("this is Instagram"). Official artwork
 * from Simple Icons (CC0, imported one icon at a time so the bundle carries only these), or from
 * `vendored.ts` where Simple Icons no longer carries the brand. Drawn unmodified, in the brand's
 * colour or plain monochrome. Never ours, never animated, never combined with our mark
 * (DESIGN.md, Brand marks).
 */
import { Banknote, Globe, Landmark, Mail, NotebookPen, RefreshCw, Smartphone, SquareTerminal, Truck } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import {
  siAirbnb,
  siBluesky,
  siBookingdotcom,
  siDropbox,
  siEbay,
  siEtsy,
  siFacebook,
  siGithub,
  siGmail,
  siGoogle,
  siGooglecalendar,
  siGoogledrive,
  siGooglesheets,
  siInstagram,
  siMailchimp,
  siMedium,
  siNotion,
  siPaypal,
  siPinterest,
  siQuickbooks,
  siReddit,
  siShopify,
  siSquare,
  siStripe,
  siSubstack,
  siTelegram,
  siThreads,
  siTiktok,
  siTripadvisor,
  siTrustpilot,
  siWhatsapp,
  siWoocommerce,
  siWordpress,
  siXero,
  siYelp,
  siX,
  siYoutube,
} from 'simple-icons';
import { cn } from '@/lib/cn';
import { hostOf, siteName, siteOf } from '@/lib/sites';
import { linkedin } from './vendored';

export type MarkSize = 14 | 16 | 20 | 24 | 32 | 40;

export interface BrandArt {
  title: string;
  hex: string;
  path: string;
  viewBox?: string;
}

const ART: Record<string, BrandArt> = {
  instagram: siInstagram,
  facebook: siFacebook,
  linkedin,
  reddit: siReddit,
  medium: siMedium,
  x: siX,
  youtube: siYoutube,
  tiktok: siTiktok,
  threads: siThreads,
  pinterest: siPinterest,
  gmail: siGmail,
  google: siGoogle,
  shopify: siShopify,
  wordpress: siWordpress,
  etsy: siEtsy,
  substack: siSubstack,
  github: siGithub,
  whatsapp: siWhatsapp,
  bluesky: siBluesky,
  stripe: siStripe,
  square: siSquare,
  xero: siXero,
  quickbooks: siQuickbooks,
  paypal: siPaypal,
  booking: siBookingdotcom,
  airbnb: siAirbnb,
  sheets: siGooglesheets,
  drive: siGoogledrive,
  calendar: siGooglecalendar,
  notion: siNotion,
  dropbox: siDropbox,
  mailchimp: siMailchimp,
  woocommerce: siWoocommerce,
  ebay: siEbay,
  tripadvisor: siTripadvisor,
  trustpilot: siTrustpilot,
  yelp: siYelp,
  telegram: siTelegram,
};

/** Our own marks for kinds of lane that are not someone else's brand. */
const OWN: Record<string, LucideIcon> = {
  blog: NotebookPen,
  mail: Mail,
  // Outlook's own artwork is in no free set we can ship, so it is named by our plain mail mark.
  outlook: Mail,
  bank: Banknote,
  gov: Landmark,
  portal: Truck,
  shell: SquareTerminal,
  terminal: SquareTerminal,
  pixel: Smartphone,
  'notes-sync': RefreshCw,
};

/** Whether a brand mark exists for this site (key, domain, URL or lane name). */
export function hasBrandMark(site: string): boolean {
  const key = siteOf(site)?.key;
  return key !== undefined && key in ART;
}

/**
 * The site's own colour, for a soft stand-in behind its mark when there is no picture yet. Null
 * for our own marks, for unknown sites and for black brands, which take the page's colours.
 */
export function siteColor(site: string): string | null {
  const value = /^[\w-]+\/[\w-]+$/.test(site) ? (site.split('/')[1] ?? site) : site;
  const key = siteOf(value)?.key;
  const art = key ? ART[key] : undefined;
  if (!art || luminance(art.hex) < 0.02) return null;
  return `#${art.hex}`;
}

/** Relative luminance of a brand hex, 0 (black) to 1 (white). */
export function luminance(hex: string): number {
  const value = hex.replace('#', '');
  const channel = (start: number) => {
    const c = parseInt(value.slice(start, start + 2), 16) / 255;
    return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(0) + 0.7152 * channel(2) + 0.0722 * channel(4);
}

/**
 * The colour a brand mark is drawn in. Black brands (X, TikTok, Medium, Threads) would vanish on
 * the dark theme and the night band, so they take the primary text colour (redefined on the night), which is the
 * brand's own monochrome rule.
 */
export function brandFill(art: BrandArt, tone: 'brand' | 'mono'): string {
  if (tone === 'mono') return 'currentColor';
  return luminance(art.hex) < 0.02 ? 'var(--text-primary)' : `#${art.hex}`;
}

interface SiteMarkProps {
  /** A site key, a domain, a URL or a lane name: `instagram`, `instagram.com`, `aster/instagram`. */
  site: string;
  size?: MarkSize;
  tone?: 'brand' | 'mono';
  /** On a neutral rounded tile, so marks of different shapes line up in a row. */
  tile?: boolean;
  /** The accessible name. False when a visible name sits beside it. */
  label?: string | false;
  className?: string;
}

function Generic({ initial, size }: { initial: string; size: number }) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} aria-hidden="true">
      <circle cx={12} cy={12} r={10.5} fill="none" stroke="currentColor" strokeOpacity={0.35} strokeWidth={1.5} />
      <ellipse cx={12} cy={12} rx={4.5} ry={10.5} fill="none" stroke="currentColor" strokeOpacity={0.2} strokeWidth={1.25} />
      <text x={12} y={16.25} textAnchor="middle" fontSize={12} fontWeight={600} fill="currentColor" style={{ fontFamily: 'var(--font-sans)' }}>
        {initial}
      </text>
    </svg>
  );
}

export function SiteMark({ site, size = 16, tone = 'brand', tile = false, label, className }: SiteMarkProps) {
  // A lane id ("aster/instagram") names its site in the second half.
  const value = /^[\w-]+\/[\w-]+$/.test(site) ? (site.split('/')[1] ?? site) : site;
  const known = siteOf(value);
  const name = known?.name ?? siteName(value);
  const art = known ? ART[known.key] : undefined;
  const Own = known ? OWN[known.key] : undefined;
  const inner = tile ? Math.round(size * 0.6) : size;

  let glyph;
  if (art) {
    glyph = (
      <svg viewBox={art.viewBox ?? '0 0 24 24'} width={inner} height={inner} aria-hidden="true" style={{ fill: brandFill(art, tone) }}>
        <path d={art.path} />
      </svg>
    );
  } else if (Own) {
    glyph = <Own aria-hidden="true" width={inner} height={inner} strokeWidth={size >= 32 ? 1.5 : 2} />;
  } else {
    const host = hostOf(value);
    const initial = (host || value).replace(/[^a-z0-9]/gi, '').charAt(0).toUpperCase() || '?';
    glyph = initial === '?' ? <Globe aria-hidden="true" width={inner} height={inner} /> : <Generic initial={initial} size={inner} />;
  }

  const a11y = label === false ? { 'aria-hidden': true as const } : { role: 'img', 'aria-label': label ?? name };
  return (
    <span
      {...a11y}
      data-site={known?.key ?? 'unknown'}
      style={{ width: size, height: size, borderRadius: tile ? Math.round(size * 0.28) : undefined }}
      className={cn(
        'inline-flex shrink-0 items-center justify-center text-muted',
        tile && 'border border-line bg-inset',
        className,
      )}
    >
      {glyph}
    </span>
  );
}
