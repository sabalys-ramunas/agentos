/** The command bar's Checks rows: the newest check of each page, found by its path. */
import { ScanSearch } from 'lucide-react';
import { isStudioMissing, useStudioRuns } from '@/api/hooks';
import type { CommandSource } from '@/components/shell/commands';
import { pagePath, runStatus } from './model';

export const commands: CommandSource = {
  id: 'checks',
  heading: 'Checks',
  order: 20,
  onlyWhenSearching: true,
  useItems: (org) => {
    const data = useStudioRuns(org, { limit: 200 }).data;
    if (!data || isStudioMissing(data)) return [];
    const seen = new Set<string>();
    return data.runs
      .filter((run) => !seen.has(run.url) && (seen.add(run.url), true))
      .map((run) => ({
        id: run.id,
        label: `Check of ${pagePath(run.url).path}`,
        detail: runStatus(run).label,
        icon: ScanSearch,
        keywords: `${run.url} ${run.profile ?? ''} design check studio`,
        onSelect: ({ navigate }) => void navigate({ to: '/$org/checks/$id', params: { org, id: run.id } }),
      }));
  },
};
