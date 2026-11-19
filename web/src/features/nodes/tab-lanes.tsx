import { BusinessMark } from '@/components/brand/identity';
import { Link } from '@tanstack/react-router';
import { Layers } from 'lucide-react';
import type { Lane, NodeDetail, Workspace } from '@/api/types';
import { LaneIcon } from '@/components/lane-icon';
import { Card } from '@/components/ui/card';
import { ConditionBadge } from '@/components/ui/state-badge';
import { EmptyState } from '@/components/ui/states';
import { Table, Td, Th, Tr, mainCell } from '@/components/ui/table';
import { timeAgo } from '@/lib/format';
import { laneName } from '@/lib/labels';
import { laneCondition } from '@/lib/states';
import { useMe } from '@/lib/use-principal';
import { NONE, Panel } from './parts';

/** A lane's share of the machine: the number, and a bar against the busiest lane here. */
function Share({ value, max, unit }: { value: number | null | undefined; max: number; unit: string }) {
  if (value === null || value === undefined) return NONE;
  return (
    <span className="inline-flex items-center justify-end gap-2">
      <span aria-hidden="true" className="h-1 w-12 overflow-hidden rounded-full bg-pressed">
        <span className="block h-full rounded-full bg-faint" style={{ width: `${Math.max(4, (value / Math.max(1, max)) * 100)}%` }} />
      </span>
      <span className="mono tabular w-14 text-right text-secondary">
        {value}
        {unit}
      </span>
    </span>
  );
}

export function LanesTab({ org, node, workspaces }: { org: string; node: NodeDetail; workspaces: Workspace[] }) {
  const me = useMe();
  if (node.lanes.length === 0) {
    return (
      <Card>
        <EmptyState icon={<Layers />} title="No accounts on this machine" description="New accounts can be pinned to this machine from Home." />
      </Card>
    );
  }
  const groups = workspaces
    .map((workspace) => ({ workspace, lanes: node.lanes.filter((lane) => lane.workspace === workspace.id) }))
    .filter((group) => group.lanes.length > 0);
  // A heartbeat may carry each lane's share. If none does, the node does not report it yet.
  const reports = node.lanes.some((lane) => lane.cpu_pct !== undefined || lane.mem_mb !== undefined);
  const maxCpu = Math.max(1, ...node.lanes.map((lane) => lane.cpu_pct ?? 0));
  const maxMem = Math.max(1, ...node.lanes.map((lane) => lane.mem_mb ?? 0));
  const warm = node.lanes.filter((lane) => lane.state === 'warm' || lane.state === 'active');

  return (
    <Panel
      title="Accounts"
      description={
        reports
          ? `${warm.length} of ${node.lanes.length} ready. A resting account uses no CPU or memory until it is opened.`
          : `${node.lanes.length} accounts. This machine does not report each account's share of CPU and memory yet.`
      }
    >
      <Card className="overflow-hidden">
        <Table>
          <thead className="hidden md:table-header-group">
            <tr>
              <Th>Account</Th>
              <Th>Status</Th>
              <Th className="text-right">CPU</Th>
              <Th className="text-right">Memory</Th>
              <Th className="hidden lg:table-cell">Last used</Th>
            </tr>
          </thead>
          {groups.map(({ workspace, lanes }) => (
            <tbody key={workspace.id}>
              <tr className="border-b border-line bg-inset">
                <th scope="colgroup" colSpan={5} className="h-8 px-4 text-left text-caption font-medium text-muted md:px-6">
                  <span className="inline-flex items-center gap-2">
                    <BusinessMark name={workspace.name} color={workspace.color} />
                    {workspace.name}
                  </span>
                </th>
              </tr>
              {lanes.map((lane: Lane) => (
                <Tr key={lane.id} className="relative hover:bg-hover">
                  <Td className={mainCell}>
                    <Link
                      to="/$org/lanes/$ws/$name"
                      params={{ org, ws: lane.workspace, name: lane.name }}
                      className="flex min-w-0 items-center gap-2 rounded-sm text-body after:absolute after:inset-0"
                    >
                      <LaneIcon kind={lane.kind} />
                      <span className="truncate">{laneName(lane.name)}</span>
                      {lane.account?.site ? <span className="mono hidden truncate text-caption text-faint sm:inline">{lane.account.site}</span> : null}
                    </Link>
                    <span className="mt-1 flex items-center gap-2 md:hidden">
                      <ConditionBadge condition={laneCondition(lane, { node: node.state, me })} />
                      {lane.mem_mb ? <span className="mono tabular text-caption text-faint">{lane.cpu_pct}% · {lane.mem_mb} MB</span> : null}
                    </span>
                  </Td>
                  <Td className="hidden md:table-cell">
                    <ConditionBadge condition={laneCondition(lane, { node: node.state, me })} />
                  </Td>
                  <Td className="hidden text-right md:table-cell">
                    <Share value={lane.cpu_pct} max={maxCpu} unit="%" />
                  </Td>
                  <Td className="hidden text-right md:table-cell">
                    <Share value={lane.mem_mb} max={maxMem} unit=" MB" />
                  </Td>
                  <Td className="hidden text-secondary text-muted lg:table-cell">{lane.last_used ? timeAgo(lane.last_used) : 'Never'}</Td>
                </Tr>
              ))}
            </tbody>
          ))}
        </Table>
      </Card>
    </Panel>
  );
}
