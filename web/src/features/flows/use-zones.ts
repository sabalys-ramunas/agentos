import { useMemo } from 'react';
import { useFlows, useNodes, useStatus, useWorkspaces } from '@/api/hooks';
import type { Schedule } from '@/api/types';
import { safeZone } from '@/lib/zone';

/** A schedule fires on its node's clock: schedule to lane, lane to node, node to zone. */
export function useZones(org: string) {
  const nodes = useNodes(org).data;
  const workspaces = useWorkspaces(org).data;
  const flows = useFlows(org).data;
  const timezone = useStatus(org).data?.timezone;
  return useMemo(() => {
    const nodeZone = new Map((nodes ?? []).map((node) => [node.id, node.timezone]));
    const laneNode = new Map((workspaces ?? []).flatMap((workspace) => workspace.lanes.map((lane) => [lane.id, lane.node] as const)));
    const flowLane = new Map((flows ?? []).map((flow) => [flow.id, flow.lane]));
    const fallback = safeZone(timezone);
    const zoneOf = (schedule: Schedule) => {
      const lane = schedule.lane ?? flowLane.get(schedule.flow);
      const node = lane ? laneNode.get(lane) : undefined;
      const zone = node ? nodeZone.get(node) : undefined;
      return zone ? safeZone(zone) : fallback;
    };
    return { zoneOf, fallback };
  }, [nodes, workspaces, flows, timezone]);
}
