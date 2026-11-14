/**
 * The address of Needs you. Links the node writes use `?approval=<id>` (policy/notifier.py, pushes and
 * emails), so that param never changes meaning. The needs-you builder owns this file: add a param here,
 * and the router (app/router.tsx) picks it up with no edit there.
 */
import { clean, oneOf, text } from '@/app/search';
import type { Search } from '@/app/search';

export interface NeedsYouSearch {
  /** Opens one item: a push, an email or a Home card says which. */
  approval?: string;
  /** Only this kind of thing waiting (`push`, `post`, `payment`, `brand` ...). */
  kind?: string;
  /** Only from this workspace. */
  workspace?: string;
  /** What is waiting, or what was decided. */
  show?: 'waiting' | 'decided';
}

export function needsYouSearch(search: Search): NeedsYouSearch {
  return clean({
    approval: text(search.approval),
    kind: text(search.kind),
    workspace: text(search.workspace),
    show: oneOf(search.show, ['waiting', 'decided'] as const),
  });
}
