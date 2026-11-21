/** The command bar's rows for Needs you: every ask that waits, found by what it is and whose it is, as the list says it. */
import { Inbox } from 'lucide-react';
import { useApprovals, useWorkspaces } from '@/api/hooks';
import type { CommandSource } from '@/components/shell/commands';
import { waitedShort } from './queue-words';
import { rowTitleOf, whereOf } from './words';

export const commands: CommandSource = {
  id: 'needs-you',
  heading: 'Needs you',
  order: 5,
  onlyWhenSearching: true,
  useItems: (org) => {
    const workspaces = useWorkspaces(org).data ?? [];
    const business = (lane: string) => workspaces.find((item) => item.id === lane.split('/')[0])?.name ?? null;
    return (useApprovals(org).data ?? []).map((approval) => {
      const name = business(approval.lane);
      const title = rowTitleOf(approval, whereOf(approval, name));
      return {
        id: approval.id,
        label: name ? `${title}, ${name}` : title,
        detail: waitedShort(approval.created_at),
        icon: Inbox,
        keywords: `${approval.summary} ${approval.lane}`,
        onSelect: ({ navigate }) => void navigate({ to: '/$org/needs-you', params: { org }, search: { approval: approval.id } }),
      };
    });
  },
};
