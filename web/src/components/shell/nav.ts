import { Braces, History, House, Inbox, LayoutGrid, Mail, MonitorCloud, Palette, Repeat, ScanEye, Server, Settings, SquareTerminal } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import type { Role } from '@/api/types';
import { PLACE_GROUPS, PLACES } from '@/lib/vocabulary';

export type NavGroup = keyof typeof PLACE_GROUPS;

export type NavTo =
  | '/$org'
  | '/$org/environments'
  | '/$org/runs'
  | '/$org/checks'
  | '/$org/nodes'
  | '/$org/brand'
  | '/$org/accounts'
  | '/$org/flows'
  | '/$org/email'
  | '/$org/site-apis'
  | '/$org/needs-you'
  | '/$org/activity'
  | '/$org/settings';

export interface NavItem {
  label: string;
  /** The internal route. The address bar shows its everyday name (app/paths.ts). */
  to: NavTo;
  icon: LucideIcon;
  /** The second key of the "g then x" shortcut. Each is unique, and never one a screen acts on after g. */
  key: string;
  group: NavGroup;
}

/**
 * The menu the owner approved with Mono (2026-11-13), in five groups:
 *
 *   Work: Home, Environments, Runs, Checks
 *   Where it runs: Machines
 *   Who it is for: Brand, Accounts
 *   Automations: Routines, Email, Site APIs
 *   You: Needs you, History, Settings
 *
 * A new place goes into one of these groups, with a key of its own. Environments (2026-11-13) is the
 * first place of work after Home: an agent's own cloud computer is what agentos is for.
 */
export const NAV: NavItem[] = [
  { label: PLACES.home.word, to: '/$org', icon: House, key: 'h', group: 'work' },
  { label: PLACES.environments.word, to: '/$org/environments', icon: MonitorCloud, key: 'v', group: 'work' },
  { label: PLACES.runs.word, to: '/$org/runs', icon: SquareTerminal, key: 'r', group: 'work' },
  { label: PLACES.checks.word, to: '/$org/checks', icon: ScanEye, key: 'c', group: 'work' },
  { label: PLACES.machines.word, to: '/$org/nodes', icon: Server, key: 'm', group: 'where' },
  { label: PLACES.brand.word, to: '/$org/brand', icon: Palette, key: 'b', group: 'who' },
  { label: PLACES.accounts.word, to: '/$org/accounts', icon: LayoutGrid, key: 'a', group: 'who' },
  { label: PLACES.routines.word, to: '/$org/flows', icon: Repeat, key: 'o', group: 'automations' },
  { label: PLACES.email.word, to: '/$org/email', icon: Mail, key: 'e', group: 'automations' },
  { label: PLACES.siteApis.word, to: '/$org/site-apis', icon: Braces, key: 'i', group: 'automations' },
  { label: PLACES.needsYou.word, to: '/$org/needs-you', icon: Inbox, key: 'n', group: 'you' },
  { label: PLACES.history.word, to: '/$org/activity', icon: History, key: 'y', group: 'you' },
  { label: PLACES.settings.word, to: '/$org/settings', icon: Settings, key: 's', group: 'you' },
];

export const GROUP_ORDER: NavGroup[] = ['work', 'where', 'who', 'automations', 'you'];

/** The phone's tab bar: what a person reaches for with a thumb, and More for the rest. */
// Environments sits under More: its word does not fit a fifth of a phone, and Home lists the open ones first.
const TAB_TOS: NavTo[] = ['/$org', '/$org/runs', '/$org/checks', '/$org/nodes', '/$org/needs-you'];

/**
 * An approver says yes or no, and sees what came of it: Needs you, History and Settings, and
 * nothing else (startup.md B5). The server already refuses the rest; this is the menu.
 */
const APPROVER_TOS: NavTo[] = ['/$org/needs-you', '/$org/activity', '/$org/settings'];

export interface MenuSection {
  group: NavGroup;
  label: string;
  items: NavItem[];
}

export interface Menu {
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

const OWNER_MENU: Menu = {
  all: NAV,
  sections: sectionsOf(NAV),
  tabs: TAB_TOS.map((to) => NAV.find((item) => item.to === to)!),
  more: NAV.filter((item) => !TAB_TOS.includes(item.to)),
};

const APPROVER = NAV.filter((item) => APPROVER_TOS.includes(item.to));
const APPROVER_MENU: Menu = { all: APPROVER, sections: sectionsOf(APPROVER), tabs: APPROVER, more: [] };

export function menuFor(role: Role): Menu {
  return role === 'approver' ? APPROVER_MENU : OWNER_MENU;
}

/** Where the logo goes: Home, or Needs you for someone whose menu has no Home. */
export const homeFor = (role: Role) => (role === 'approver' ? ('/$org/needs-you' as const) : ('/$org' as const));

/**
 * The place a page belongs to, by the first part of its internal path. A page with no place of its
 * own (an account, a review link, a workspace) is shown under the place a person reached it from.
 */
const SECTION_OF: Record<string, NavTo> = {
  '': '/$org',
  environments: '/$org/environments',
  runs: '/$org/runs',
  checks: '/$org/checks',
  nodes: '/$org/nodes',
  brand: '/$org/brand',
  accounts: '/$org/accounts',
  lanes: '/$org/accounts',
  workspaces: '/$org/accounts',
  logins: '/$org/accounts',
  flows: '/$org/flows',
  email: '/$org/email',
  'site-apis': '/$org/site-apis',
  'needs-you': '/$org/needs-you',
  review: '/$org/needs-you',
  activity: '/$org/activity',
  settings: '/$org/settings',
};

/** The nav item a path such as `/acme/nodes/home-mini` sits under, or null. */
export function placeOf(pathname: string): NavItem | null {
  const section = pathname.split('/')[2] ?? '';
  const to = SECTION_OF[section];
  return to ? (NAV.find((item) => item.to === to) ?? null) : null;
}
