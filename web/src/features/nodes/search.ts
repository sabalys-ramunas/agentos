/**
 * The address of Machines and of one machine (`/machines`, `/machines/<id>`; inside the app the routes
 * are still `/$org/nodes`). The machines builder owns this file: add or change a param or a tab here,
 * and the router (app/router.tsx) picks it up with no edit there.
 *
 * `/machines` is the table. `/machines/<id>` is the same table with that machine's detail open beside
 * it. `/machines/<id>?tab=logs` is the machine's full page, at one of its tabs.
 */
import { clean, flag, oneOf, text } from '@/app/search';
import type { Search } from '@/app/search';

export const MACHINE_SORTS = ['name', 'state', 'load', 'memory'] as const;
export type MachineSort = (typeof MACHINE_SORTS)[number];

/** How the table is sorted. Both the table and a machine opened beside it read these. */
export interface TableSearch {
  sort?: MachineSort;
  /** Unset means the column's natural direction: names A to Z, the busiest and the worst first. */
  dir?: 'asc' | 'desc';
}

export interface NodesSearch extends TableSearch {
  /** Opens "Add a machine". */
  add?: boolean;
}

const tableSearch = (search: Search): TableSearch => ({
  // `sort` once also took these words from the old map's list; they still sort.
  sort: oneOf(search.sort, MACHINE_SORTS),
  dir: oneOf(search.dir, ['asc', 'desc'] as const),
});

export function nodesSearch(search: Search): NodesSearch {
  // `view`, `select`, `state` and `q` belonged to the old map and diagram: an old link opens the table.
  return clean({ add: flag(search.add) ? true : undefined, ...tableSearch(search) });
}

export const NODE_TABS = ['lanes', 'workers', 'browsers', 'schedules', 'logs', 'installed', 'environments', 'resources', 'connection', 'health', 'backups', 'settings'] as const;
export type NodeTab = (typeof NODE_TABS)[number];

/** Everything a machine's page shows is in the URL, so a link and Back land on the same view. */
export interface NodeSearch extends TableSearch {
  /** Set: the machine's full page at this tab. Unset: the table with the machine open beside it. */
  tab?: NodeTab;
  range?: '1h' | '7d';
  source?: string;
  /** The lowest level shown. Unset means info, so debug lines only show when asked for. */
  level?: 'debug' | 'warn' | 'error';
  q?: string;
  /** Installed programs, by where they are installed. */
  scope?: 'node' | 'lane' | 'base_image';
  /** A job on this machine, opened beside its list. */
  job?: string;
  /** A terminal on this machine, opened beside its list. */
  terminal?: string;
}

export function nodeSearch(search: Search): NodeSearch {
  return clean({
    tab: oneOf(search.tab, NODE_TABS),
    range: oneOf(search.range, ['1h', '7d'] as const),
    source: text(search.source),
    level: oneOf(search.level, ['debug', 'warn', 'error'] as const),
    q: text(search.q),
    scope: oneOf(search.scope, ['node', 'lane', 'base_image'] as const),
    job: text(search.job),
    terminal: text(search.terminal),
    ...tableSearch(search),
  });
}
