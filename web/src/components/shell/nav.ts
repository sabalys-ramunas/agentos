import { Braces, CirclePlay, History, House, Inbox, Monitor, Palette, Repeat, Settings, Users } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import type { Role } from '@/api/types';
import { PLACE_GROUPS, PLACES } from '@/lib/vocabulary';

export type NavGroup = keyof typeof PLACE_GROUPS;

export type NavTo =
  | '/$org'
  | '/$org/needs-you'
  | '/$org/environments'
  | '/$org/activity'
  | '/$org/accounts'
  | '/$org/flows'
  | '/$org/brand'
  | '/$org/runs'
  | '/$org/site-apis'
  | '/$org/settings';

/** The lists a place shows as tabs. Each is its own route and address, so old links open the right tab. */
export type TabTo = '/$org/environments' | '/$org/nodes' | '/$org/flows' | '/$org/email' | '/$org/runs' | '/$org/checks';

export interface PlaceTab {
  label: string;
  to: TabTo;
}

export interface NavItem {
  label: string;
  /** The internal route. The address bar shows its everyday name (app/paths.ts). */
  to: NavTo;
  icon: LucideIcon;
  /** The second key of the "g then x" shortcut. Each is unique, and never one a screen acts on after g. */
  key: string;
  group: NavGroup;
  /** A place that holds two lists shows them as tabs; the first is where the place opens. */
  tabs?: PlaceTab[];
  /**
   * A developer place stays out of the rail and the tab bar's More until it is first used (see
   * `developer-places.ts`). The command bar and its address always reach it.
   */
  untilUsed?: boolean;
}

/**
 * The simpler navigation the owner chose (2026-11-21, nav-proposal.png): 13 places became 10.
 *
 *   Home, Needs you, Computers, What happened         (no heading: what everyone needs every day)
 *   Your business: Accounts, Jobs that repeat, Brand
 *   For developers: Runs and checks, Site APIs         (each hidden until first used)
 *   Settings                                           (set apart at the end)
 *
 * Computers is Environments and Machines as two tabs, Jobs that repeat is Routines and Email, and
 * Runs and checks is Runs and Checks. A new place goes into one of these groups, with a key of its own.
 */
export const NAV: NavItem[] = [
  { label: PLACES.home.word, to: '/$org', icon: House, key: 'h', group: 'first' },
  { label: PLACES.needsYou.word, to: '/$org/needs-you', icon: Inbox, key: 'n', group: 'first' },
  {
    label: PLACES.computers.word,
    to: '/$org/environments',
    icon: Monitor,
    key: 'c',
    group: 'first',
    tabs: [
      { label: PLACES.environments.word, to: '/$org/environments' },
      { label: PLACES.machines.word, to: '/$org/nodes' },
    ],
  },
  { label: PLACES.history.word, to: '/$org/activity', icon: History, key: 'y', group: 'first' },
  { label: PLACES.accounts.word, to: '/$org/accounts', icon: Users, key: 'a', group: 'business' },
  {
    label: PLACES.jobs.word,
    to: '/$org/flows',
    icon: Repeat,
    key: 'o',
    group: 'business',
    tabs: [
      { label: PLACES.routines.word, to: '/$org/flows' },
      { label: PLACES.email.word, to: '/$org/email' },
    ],
  },
  { label: PLACES.brand.word, to: '/$org/brand', icon: Palette, key: 'b', group: 'business' },
  {
    label: PLACES.runsAndChecks.word,
    to: '/$org/runs',
    icon: CirclePlay,
    key: 'r',
    group: 'developers',
    untilUsed: true,
    tabs: [
      { label: PLACES.runs.word, to: '/$org/runs' },
      { label: PLACES.checks.word, to: '/$org/checks' },
    ],
  },
  { label: PLACES.siteApis.word, to: '/$org/site-apis', icon: Braces, key: 'i', group: 'developers', untilUsed: true },
  { label: PLACES.settings.word, to: '/$org/settings', icon: Settings, key: 's', group: 'end' },
];

/**
 * The keys of the places that are now tabs, so hands that learned them still land: g v Environments,
 * g m Machines and g e Email. Not shown anywhere; the shown key is the place's.
 */
export const OLD_KEYS: Record<string, TabTo> = { v: '/$org/environments', m: '/$org/nodes', e: '/$org/email' };

export const GROUP_ORDER: NavGroup[] = ['first', 'business', 'developers', 'end'];

/** The phone's tab bar: the four places everyone needs, and More for the rest. */
const TAB_TOS: NavTo[] = ['/$org', '/$org/needs-you', '/$org/environments', '/$org/activity'];

/**
 * An approver says yes or no, and sees what came of it: Needs you, What happened and Settings, and
 * nothing else (startup.md B5). The server already refuses the rest; this is the menu.
 */
const APPROVER_TOS: NavTo[] = ['/$org/needs-you', '/$org/activity', '/$org/settings'];

export interface MenuSection {
  group: NavGroup;
  /** The group's heading, or empty for a group that has none (the first four, and Settings). */
  label: string;
  items: NavItem[];
}

export interface Menu {
  /** Every place this role has, shown or not: the command bar and the keys reach them all. */
  all: NavItem[];
  /** The rail, group by group. Empty groups are left out. */
  sections: MenuSection[];
  tabs: NavItem[];
  more: NavItem[];
}

function sectionsOf(items: NavItem[]): MenuSection[] {
  return GROUP_ORDER.map((group) => ({ group, label: PLACE_GROUPS[group].word, items: items.filter((item) => item.group === group) })).filter(
    (section) => section.items.length > 0,
  );
}

/** Every developer place, as if all were used: what the command bar and the keys reach. */
const EVERY_USED: ReadonlySet<NavTo> = new Set(NAV.filter((item) => item.untilUsed).map((item) => item.to));

/**
 * The menu for a role. `used` holds the developer places that have been used (`useUsedPlaces`); the
 * others are left out of the rail and More. Without it, every place shows.
 */
export function menuFor(role: Role, used: ReadonlySet<NavTo> = EVERY_USED): Menu {
  if (role === 'approver') {
    const approver = NAV.filter((item) => APPROVER_TOS.includes(item.to));
    return { all: approver, sections: sectionsOf(approver), tabs: approver, more: [] };
  }
  const shown = NAV.filter((item) => !item.untilUsed || used.has(item.to));
  return {
    all: NAV,
    sections: sectionsOf(shown),
    tabs: TAB_TOS.map((to) => NAV.find((item) => item.to === to)!),
    more: shown.filter((item) => !TAB_TOS.includes(item.to)),
  };
}

/**
 * The place a page belongs to, by the first part of its internal path. A page with no place of its
 * own (an account, a review link, a workspace) is shown under the place a person reached it from.
 */
const SECTION_OF: Record<string, NavTo> = {
  '': '/$org',
  'needs-you': '/$org/needs-you',
  review: '/$org/needs-you',
  environments: '/$org/environments',
  nodes: '/$org/environments',
  activity: '/$org/activity',
  accounts: '/$org/accounts',
  lanes: '/$org/accounts',
  workspaces: '/$org/accounts',
  logins: '/$org/accounts',
  flows: '/$org/flows',
  email: '/$org/flows',
  brand: '/$org/brand',
  runs: '/$org/runs',
  checks: '/$org/runs',
  'site-apis': '/$org/site-apis',
  settings: '/$org/settings',
};

/** The nav item a path such as `/acme/nodes/home-mini` sits under, or null. */
export function placeOf(pathname: string): NavItem | null {
  const section = pathname.split('/')[2] ?? '';
  const to = SECTION_OF[section];
  return to ? (NAV.find((item) => item.to === to) ?? null) : null;
}

/** The tab of a two-list place a path sits in (`/acme/nodes/home-mini` is in Machines), or null. */
export function tabOf(pathname: string): PlaceTab | null {
  const section = pathname.split('/')[2] ?? '';
  return placeOf(pathname)?.tabs?.find((tab) => tab.to === `/$org/${section}`) ?? null;
}
