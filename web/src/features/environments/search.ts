/**
 * The address of Environments. Add a param here, and the router (app/routes/work.tsx) picks it up.
 */
import { clean, oneOf } from '@/app/search';
import type { Search } from '@/app/search';

export const ENV_VIEWS = ['open', 'saved', 'closed'] as const;
export type EnvView = (typeof ENV_VIEWS)[number];

export interface EnvironmentsSearch {
  /** Which part of the list: open (the default, with the newest saves and closed ones), saved, or closed. */
  show?: Exclude<EnvView, 'open'>;
}

export function environmentsSearch(search: Search): EnvironmentsSearch {
  return clean({ show: oneOf(search.show, ['saved', 'closed'] as const) });
}
