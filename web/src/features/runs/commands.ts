/** The command bar's rows for Runs: every run the person may see, found by its command, lane, branch or id. */
import { SquareTerminal } from 'lucide-react';
import { useJobs } from '@/api/hooks';
import type { CommandSource } from '@/components/shell/commands';
import { JOB_STATUS, titleOf } from './words';

export const commands: CommandSource = {
  id: 'runs',
  heading: 'Runs',
  order: 10,
  onlyWhenSearching: true,
  useItems: (org) =>
    (useJobs(org, { limit: 200 }).data?.jobs ?? []).map((job) => ({
      id: job.id,
      label: `${titleOf(job)}: ${JOB_STATUS[job.state].label}`,
      detail: job.command,
      mono: true,
      icon: SquareTerminal,
      keywords: `${job.id} ${job.lane} ${job.node} ${job.source?.branch ?? ''} ${job.started_by ?? ''}`,
      onSelect: ({ navigate }) => void navigate({ to: '/$org/runs/$id', params: { org, id: job.id } }),
    })),
};
