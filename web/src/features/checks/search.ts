/**
 * The address of Checks and of one check. The checks builder owns this file: add or change a param
 * here, and the router (app/router.tsx) picks it up with no edit there.
 */
import { clean, count, oneOf, text } from '@/app/search';
import type { Search } from '@/app/search';

export const CHECK_STATES = ['clean', 'findings', 'blocked', 'failed'] as const;

export interface ChecksSearch {
  /** Clean, with findings, with a blocker, or with a screen that did not render. */
  state?: (typeof CHECK_STATES)[number];
  /** Only checks of this page (its full url). */
  page?: string;
  /** Only with this product profile (`acme/shop`). */
  profile?: string;
}

export function checksSearch(search: Search): ChecksSearch {
  return clean({
    state: oneOf(search.state, CHECK_STATES),
    page: text(search.page, 2000),
    profile: text(search.profile),
  });
}

export const SEVERITIES = ['blocker', 'major', 'minor', 'info'] as const;

export interface CheckSearch {
  /** The finding picked: its number in the check, from 1. Its outline shows on the screenshot. */
  finding?: number;
  /** The screen shown: its key, `default@360-light`. */
  screen?: string;
  /** Only findings at this severity or worse. */
  severity?: (typeof SEVERITIES)[number];
  /** The earlier check this one is compared with (its id). Without it, the page's baseline. */
  since?: string;
}

export function checkSearch(search: Search): CheckSearch {
  return clean({
    finding: count(search.finding),
    screen: text(search.screen, 120),
    severity: oneOf(search.severity, SEVERITIES),
    since: text(search.since),
  });
}
