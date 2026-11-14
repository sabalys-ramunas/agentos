/**
 * The sites and tools a lane can be on, by key. One place answers "what is this called" and
 * "which domain is it", so a lane name, a flow id, an approval summary and the site mark all
 * agree. The marks themselves live in `components/brand`.
 */

export interface SiteInfo {
  key: string;
  /** What a person calls it. */
  name: string;
  /** Domains that mean this site, most usual first. */
  domains: string[];
  /** Not a website: a tool on the computer (shell, service, notes). */
  tool?: boolean;
}

const LIST: SiteInfo[] = [
  { key: 'instagram', name: 'Instagram', domains: ['instagram.com'] },
  { key: 'facebook', name: 'Facebook', domains: ['facebook.com', 'fb.com', 'm.facebook.com'] },
  { key: 'linkedin', name: 'LinkedIn', domains: ['linkedin.com'] },
  { key: 'reddit', name: 'Reddit', domains: ['reddit.com', 'old.reddit.com'] },
  { key: 'medium', name: 'Medium', domains: ['medium.com'] },
  { key: 'x', name: 'X', domains: ['x.com', 'twitter.com'] },
  { key: 'youtube', name: 'YouTube', domains: ['youtube.com', 'youtu.be'] },
  { key: 'tiktok', name: 'TikTok', domains: ['tiktok.com'] },
  { key: 'threads', name: 'Threads', domains: ['threads.net', 'threads.com'] },
  { key: 'pinterest', name: 'Pinterest', domains: ['pinterest.com'] },
  { key: 'gmail', name: 'Gmail', domains: ['mail.google.com', 'gmail.com'] },
  { key: 'google', name: 'Google', domains: ['google.com'] },
  { key: 'shopify', name: 'Shopify', domains: ['shopify.com', 'myshopify.com'] },
  { key: 'wordpress', name: 'WordPress', domains: ['wordpress.com', 'wordpress.org'] },
  { key: 'etsy', name: 'Etsy', domains: ['etsy.com'] },
  { key: 'substack', name: 'Substack', domains: ['substack.com'] },
  { key: 'github', name: 'GitHub', domains: ['github.com'] },
  { key: 'whatsapp', name: 'WhatsApp', domains: ['whatsapp.com', 'web.whatsapp.com'] },
  { key: 'bluesky', name: 'Bluesky', domains: ['bsky.app'] },
  // Money, paperwork, shops and office work: agentos is not only for posting.
  { key: 'outlook', name: 'Outlook', domains: ['outlook.com', 'outlook.office.com', 'outlook.live.com', 'hotmail.com'] },
  { key: 'stripe', name: 'Stripe', domains: ['stripe.com', 'dashboard.stripe.com'] },
  { key: 'square', name: 'Square', domains: ['squareup.com', 'square.site', 'app.squareup.com'] },
  { key: 'xero', name: 'Xero', domains: ['xero.com', 'go.xero.com'] },
  { key: 'quickbooks', name: 'QuickBooks', domains: ['quickbooks.intuit.com', 'qbo.intuit.com', 'quickbooks.com'] },
  { key: 'paypal', name: 'PayPal', domains: ['paypal.com'] },
  { key: 'booking', name: 'Booking.com', domains: ['booking.com', 'admin.booking.com'] },
  { key: 'airbnb', name: 'Airbnb', domains: ['airbnb.com'] },
  { key: 'sheets', name: 'Google Sheets', domains: ['sheets.google.com'] },
  { key: 'drive', name: 'Google Drive', domains: ['drive.google.com'] },
  { key: 'calendar', name: 'Google Calendar', domains: ['calendar.google.com'] },
  { key: 'notion', name: 'Notion', domains: ['notion.so', 'notion.com'] },
  { key: 'dropbox', name: 'Dropbox', domains: ['dropbox.com'] },
  { key: 'mailchimp', name: 'Mailchimp', domains: ['mailchimp.com'] },
  { key: 'woocommerce', name: 'WooCommerce', domains: ['woocommerce.com'] },
  { key: 'ebay', name: 'eBay', domains: ['ebay.com'] },
  { key: 'tripadvisor', name: 'Tripadvisor', domains: ['tripadvisor.com'] },
  { key: 'trustpilot', name: 'Trustpilot', domains: ['trustpilot.com'] },
  { key: 'yelp', name: 'Yelp', domains: ['yelp.com'] },
  { key: 'telegram', name: 'Telegram', domains: ['telegram.org', 'web.telegram.org'] },
  // A bank, a tax office or another government portal: no brand of ours to show, so a plain mark.
  { key: 'bank', name: 'Bank', domains: [] },
  { key: 'gov', name: 'Government portal', domains: [] },
  { key: 'portal', name: 'Supplier portal', domains: [] },
  // Our own kinds of lane, drawn with our own marks.
  { key: 'blog', name: 'Blog', domains: [] },
  { key: 'mail', name: 'Mail', domains: [] },
  { key: 'shell', name: 'Shell', domains: [], tool: true },
  { key: 'terminal', name: 'Terminal', domains: [], tool: true },
  { key: 'pixel', name: 'Pixel', domains: [], tool: true },
  { key: 'notes-sync', name: 'Notes sync', domains: [], tool: true },
];

export const SITES: ReadonlyMap<string, SiteInfo> = new Map(LIST.map((site) => [site.key, site]));

const BY_DOMAIN = new Map(LIST.flatMap((site) => site.domains.map((domain) => [domain, site] as const)));

/** The bare host of a URL, a domain or a host with a path: `https://www.instagram.com/x` gives `instagram.com`. */
export function hostOf(value: string): string {
  const trimmed = value.trim().toLowerCase();
  let host = trimmed.split(/[/?#]/)[0] ?? '';
  if (/^[a-z][a-z0-9+.-]*:\/\//.test(trimmed)) {
    try {
      host = new URL(trimmed).hostname;
    } catch {
      host = '';
    }
  }
  return host.replace(/^www\./, '').replace(/:\d+$/, '');
}

/**
 * Which known site a key, a domain, a URL or a lane name means, or null. A lane name may carry a
 * suffix (`instagram-2`, `instagram-shop`), so its first dash-separated word is tried as well.
 */
export function siteOf(value: string | null | undefined): SiteInfo | null {
  if (!value) return null;
  let raw = value.trim().toLowerCase();
  // A lane id (`aster/instagram`) names its site in the second half.
  if (!raw.includes('://') && /^[a-z0-9_-]+\/[a-z0-9_-]+$/.test(raw)) raw = raw.split('/')[1] ?? raw;
  const direct = SITES.get(raw);
  if (direct) return direct;
  const host = hostOf(raw);
  for (let candidate = host; candidate.includes('.'); candidate = candidate.slice(candidate.indexOf('.') + 1)) {
    const match = BY_DOMAIN.get(candidate);
    if (match) return match;
  }
  // Any bank's or government's own site reads as one, by its address.
  if (/(^|\.)gov(\.[a-z]{2})?$|\.gov\.|(^|\.)(gouv|gob|valstybe|vmi|hmrc|irs)\./.test(host)) return SITES.get('gov') ?? null;
  if (/(^|[.-])(bank|banking|ebank)([.-]|$)|bank\./.test(host)) return SITES.get('bank') ?? null;
  const word = raw.split(/[-_ ]/)[0] ?? '';
  return SITES.get(word) ?? null;
}

/** A name for any site or lane: the known one, or the value tidied ("notes-sync" gives "Notes sync"). */
export function siteName(value: string): string {
  const known = siteOf(value);
  if (known) return known.name;
  const host = hostOf(value);
  if (host.includes('.')) return host;
  return value.charAt(0).toUpperCase() + value.slice(1).replaceAll('-', ' ');
}
