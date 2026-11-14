const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

export function timeAgo(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return 'Never';
  const elapsed = now - new Date(iso).getTime();
  if (elapsed < 45_000) return 'Just now';
  if (elapsed < HOUR) return `${Math.max(1, Math.round(elapsed / MINUTE))}m ago`;
  if (elapsed < DAY) return `${Math.round(elapsed / HOUR)}h ago`;
  if (elapsed < 30 * DAY) return `${Math.round(elapsed / DAY)}d ago`;
  return monthDay(iso);
}

/** "Nov 9". */
export function monthDay(at: string | number | Date): string {
  return new Date(at).toLocaleDateString('en', { month: 'short', day: 'numeric' });
}

export function timeUntil(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return '';
  const left = new Date(iso).getTime() - now;
  if (left <= 0) return 'Expired';
  if (left < HOUR) return `${Math.max(1, Math.round(left / MINUTE))}m`;
  if (left < DAY) return `${Math.round(left / HOUR)}h`;
  return `${Math.round(left / DAY)}d`;
}

/** "09:05", in the reader's own zone. */
export function clock(at: string | number | Date): string {
  return new Date(at).toLocaleTimeString('en', { hour: '2-digit', minute: '2-digit', hour12: false });
}

/** "09:05" in UTC, as the action log is kept. */
export function utcClock(iso: string): string {
  return new Date(iso).toLocaleTimeString('en', { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'UTC' });
}

/** "09:05:42", for a log line. */
export function clockSeconds(iso: string): string {
  return new Date(iso).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

/** "Today 09:00" or "Tomorrow 09:00", so a time in a list is never ambiguous. */
export function dayAndTime(iso: string, now = Date.now()): string {
  const target = new Date(iso);
  const start = new Date(now);
  start.setHours(0, 0, 0, 0);
  const days = Math.floor((target.getTime() - start.getTime()) / DAY);
  if (days === 0) return `Today ${clock(iso)}`;
  if (days === 1) return `Tomorrow ${clock(iso)}`;
  return dateTime(iso);
}

/** Node-supplied links only ever open over http or https. */
export function safeUrl(url: string | null | undefined): string | null {
  if (!url) return null;
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'https:' || parsed.protocol === 'http:' ? parsed.href : null;
  } catch {
    return null;
  }
}

/**
 * Where a redirect from the API may send the whole tab: an https page, or this panel. Anything
 * else (`javascript:`, `data:`, plain http elsewhere) is refused (security review 19).
 */
export function safeRedirect(url: unknown, origin = location.origin): string | null {
  if (typeof url !== 'string' || !url) return null;
  try {
    const parsed = new URL(url, origin);
    return parsed.protocol === 'https:' || parsed.origin === origin ? parsed.href : null;
  } catch {
    return null;
  }
}

/** The server's own limit on a `next` (`fields.MAX_NEXT_LENGTH`). */
const MAX_NEXT = 512;

/**
 * Where to go after sign-in. It comes from the URL, so only a path on this site is allowed, by
 * the same rules as the server's `safe_next` (security review 15): //evil.example, a backslash
 * anywhere, and any control or invisible format character (a tab, a newline, a zero-width
 * space) all count as leaving, because a browser strips or bends them.
 */
export function safeNext(next: unknown): string | undefined {
  if (typeof next !== 'string' || next.length === 0 || next.length > MAX_NEXT) return undefined;
  if (!next.startsWith('/') || next.startsWith('//') || next.includes('\\') || /\p{C}/u.test(next)) return undefined;
  try {
    const base = 'https://panel.invalid';
    return new URL(next, base).origin === base ? next : undefined;
  } catch {
    return undefined;
  }
}

export function dateTime(iso: string): string {
  return new Date(iso).toLocaleString('en', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
}

export function date(iso: string): string {
  return new Date(iso).toLocaleDateString('en', { year: 'numeric', month: 'short', day: 'numeric' });
}

export function usd(value: number): string {
  return `$${value.toFixed(2)}`;
}

export function compact(value: number): string {
  return new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 }).format(value);
}

export function gb(mb: number): string {
  return `${(mb / 1024).toFixed(1)} GB`;
}

export function shortHash(hash: string): string {
  const [algo, value = ''] = hash.split(':');
  return `${algo}:${value.slice(0, 12)}`;
}

export function plural(count: number, one: string, many = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`;
}

export function slugify(text: string): string {
  return text
    .toLowerCase()
    .normalize('NFKD')
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 40);
}

/** "420ms", "7.0s", "2m 14s" or "3h 12m": the shortest reading that still means something. */
export function elapsed(ms: number): string {
  const value = Math.max(0, ms);
  if (value < 1_000) return `${Math.round(value)}ms`;
  if (value < 60_000) return `${(value / 1_000).toFixed(1)}s`;
  const seconds = Math.round(value / 1_000);
  if (seconds < 3_600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
  return `${Math.floor(seconds / 3_600)}h ${Math.floor((seconds % 3_600) / 60)}m`;
}
