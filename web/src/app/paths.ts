/**
 * The everyday names in the address bar. People read Accounts, Routines, History and Machines
 * (structure.md section 3, Mono), so the URLs say them too: `/acme/history`, `/acme/machines/home-mini`,
 * `/acme/accounts/aster/instagram`.
 *
 * Inside the panel the routes keep their engine names (`/$org/activity`, `/$org/nodes`, `/$org/lanes`,
 * `/$org/flows`), which is what every typed link and `useSearch({ from })` is written against. The
 * router rewrites between the two (`rewrite` in `makeRouter`): a URL coming in is read with the
 * engine name, a URL going out is written with the everyday one. So:
 *
 * - every old link keeps working: emails, pushes, webhooks, the map's `ref.href` and bookmarks
 *   that say `/activity`, `/nodes/...`, `/computers/...` (Machines' name before Mono), `/lanes/...`
 *   or `/flows` open the same page;
 * - an old URL loaded in the browser is replaced by the new one before the app starts
 *   (`canonicalUrl`), so it is never shown again;
 * - a new screen links with the internal route as usual and the person sees the new word.
 *
 * `/$org/accounts` on its own is a real page (every account by business); only
 * `/$org/accounts/<business>/<account>` is the old lane page.
 */

/** Top-level paths that are not an org. A segment after these is never renamed. */
const NOT_AN_ORG = new Set(['login', 'sign-in', 'sign-up', 'check-email', 'auth', 'legal', 'invite', 'onboarding', 'api', 'offline.html', 'computer-form', 'shared']);

/** Engine name inside, everyday name outside. */
const RENAMES: [internal: string, outside: string][] = [
  ['activity', 'history'],
  ['flows', 'routines'],
  ['nodes', 'machines'],
];

/** Everyday names that were once used and still arrive in old links. Read, never written. */
const OLD_NAMES: [internal: string, outside: string][] = [['nodes', 'computers']];

function split(pathname: string): string[] | null {
  const parts = pathname.split('/');
  // ['', org, section, ...]
  if (parts.length < 3 || !parts[1] || NOT_AN_ORG.has(parts[1])) return null;
  return parts;
}

/** `/acme/history` to `/acme/activity`. Anything else is returned as it is. */
export function toInternalPath(pathname: string): string {
  const parts = split(pathname);
  if (!parts) return pathname;
  const section = parts[2];
  const renamed = [...RENAMES, ...OLD_NAMES].find(([, outside]) => outside === section);
  if (renamed) parts[2] = renamed[0];
  // An account's page. `/acme/accounts` alone is the Accounts page itself.
  else if (section === 'accounts' && parts.length >= 5 && parts[3] && parts[4]) parts[2] = 'lanes';
  return parts.join('/');
}

/** `/acme/activity` to `/acme/history`: what a person sees and copies. */
export function toPublicPath(pathname: string): string {
  const parts = split(pathname);
  if (!parts) return pathname;
  const section = parts[2];
  const renamed = RENAMES.find(([internal]) => internal === section);
  if (renamed) parts[2] = renamed[1];
  else if (section === 'lanes' && parts.length >= 5) parts[2] = 'accounts';
  return parts.join('/');
}

/** The same address with the everyday names, or null when it already has them. */
export function canonicalUrl(pathname: string, rest = ''): string | null {
  const next = toPublicPath(toInternalPath(pathname));
  return next === pathname ? null : next + rest;
}
