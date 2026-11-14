/**
 * The address of Brand and of one product profile. The brand builder owns this file: add or change a
 * param here, and the router (app/router.tsx) picks it up with no edit there.
 *
 * A profile's own address is `/$org/brand/$profile` with `profile` its ref, `workspace/profile`
 * (the router writes the slash as %2F). `workspace~profile` is read the same.
 */
import { clean, flag, oneOf, text } from '@/app/search';
import type { Search } from '@/app/search';

export interface BrandSearch {
  /** Only profiles in this workspace. */
  workspace?: string;
  /** Only profiles with a draft waiting for a yes. */
  waiting?: boolean;
}

export function brandSearch(search: Search): BrandSearch {
  return clean({ workspace: text(search.workspace), waiting: flag(search.waiting) });
}

export const PROFILE_SECTIONS = ['components', 'colour', 'type', 'space', 'guidelines', 'references', 'environments'] as const;

export interface ProfileSearch {
  /** The section scrolled to when the page opens. It opens on Components. */
  section?: (typeof PROFILE_SECTIONS)[number];
  /** A component picked: its card is marked. */
  component?: string;
}

export function profileSearch(search: Search): ProfileSearch {
  return clean({ section: oneOf(search.section, PROFILE_SECTIONS), component: text(search.component) });
}
