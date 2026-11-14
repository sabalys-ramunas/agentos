/**
 * The address of Home. The home builder owns this file: add a param here, and the router
 * (app/router.tsx) picks it up with no edit there.
 */
import { clean, text } from '@/app/search';
import type { Search } from '@/app/search';

export interface HomeSearch {
  /** The running job brought to the front of Home's slider. */
  job?: string;
}

export function homeSearch(search: Search): HomeSearch {
  return clean({ job: text(search.job) });
}
