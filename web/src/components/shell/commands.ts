/**
 * What the command bar lists beyond the places: runs, checks, machines, a brand's components. A
 * screen adds its own items without touching the shell:
 *
 *   1. Make `src/features/<yours>/commands.ts`.
 *   2. Export `commands`, a `CommandSource` (or an array of them).
 *   3. Done. The shell finds every such file at build time (`import.meta.glob` below).
 *
 *   // src/features/runs/commands.ts
 *   export const commands: CommandSource = {
 *     id: 'runs',
 *     heading: 'Runs',
 *     order: 10,
 *     useItems: (org) =>
 *       (useJobs(org).data ?? []).map((job) => ({
 *         id: job.id,
 *         label: job.note || job.cmd,
 *         detail: job.id,
 *         icon: SquareTerminal,
 *         keywords: `${job.cmd} ${job.lane}`,
 *         onSelect: ({ navigate }) => void navigate({ to: '/$org/runs/$id', params: { org, id: job.id } }),
 *       })),
 *   };
 *
 * `useItems` is a hook: it runs on every render of the open command bar, with the same rules as any
 * hook (call other hooks at its top, unconditionally). Return [] while there is nothing to show.
 * Items appear in the order given, under `heading`, after the places and before the actions.
 */
import type { useNavigate } from '@tanstack/react-router';
import type { LucideIcon } from 'lucide-react';

export interface CommandContext {
  org: string;
  navigate: ReturnType<typeof useNavigate>;
}

export interface CommandItem {
  /** Unique within its source. */
  id: string;
  /** What the row says. */
  label: string;
  /** A quiet second part on the right: an id, a branch, a machine. */
  detail?: string;
  /** Mono for `detail`, when it is an id or a command. */
  mono?: boolean;
  icon?: LucideIcon;
  /** More words that find this row. The label and detail always do. */
  keywords?: string;
  /** Keys shown on the right, such as ['G', 'R']. Only shown; the shell does not bind them. */
  keys?: string[];
  onSelect: (context: CommandContext) => void;
}

export interface CommandSource {
  id: string;
  heading: string;
  /** Lower comes first. The shell's own sources use 50 (Machines) and up. */
  order?: number;
  /** Long lists (every run, every account) show only once the person types. */
  onlyWhenSearching?: boolean;
  useItems: (org: string, query: string) => CommandItem[];
}

type SourceModule = { commands?: CommandSource | CommandSource[] };

const found = import.meta.glob<SourceModule>('/src/features/*/commands.ts', { eager: true });

const builtIn: CommandSource[] = [];

/** For the shell's own sources (Machines). Features use the file convention above. */
export function registerCommandSource(source: CommandSource) {
  if (!builtIn.some((item) => item.id === source.id)) builtIn.push(source);
}

/** Every source, in order. Stable for the life of the page, so each may call hooks. */
export function commandSources(): CommandSource[] {
  const fromFeatures = Object.values(found).flatMap((module) => (module.commands ? [module.commands].flat() : []));
  return [...fromFeatures, ...builtIn].sort((a, b) => (a.order ?? 100) - (b.order ?? 100));
}
