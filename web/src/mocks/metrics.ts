/**
 * Metrics, built from the same activity the list shows, so a bar and the rows under it always
 * agree. Empty buckets are zeros, and `at` is the bucket start, as the contract says.
 */
import type { ActivityBucket, ActivityMetrics, ActivityRange, NodeMetrics, NodePoint, NodeRange } from '@/api/types';
import type { NodeRecord, OrgData } from './seed';

const HOUR = 3_600_000;
const ACTIVITY: Record<ActivityRange, { seconds: number; count: number }> = {
  '24h': { seconds: 3_600, count: 24 },
  '7d': { seconds: 21_600, count: 28 },
  '30d': { seconds: 86_400, count: 30 },
};

function emptyBucket(at: number): ActivityBucket {
  return {
    at: new Date(at).toISOString(),
    events: 0,
    runs: { succeeded_verified: 0, succeeded_unverified: 0, partial: 0, failed: 0 },
    approvals: { requested: 0, approved: 0, denied: 0 },
    cost: { tokens: 0, usd: 0 },
  };
}

export function activityMetrics(data: OrgData, range: ActivityRange, scope: { workspace?: string | null; lane?: string | null }): ActivityMetrics {
  const { seconds, count } = ACTIVITY[range];
  const size = seconds * 1000;
  const end = Math.ceil(Date.now() / size) * size;
  const start = end - count * size;
  const buckets = Array.from({ length: count }, (_, index) => emptyBucket(start + index * size));

  for (const item of data.activity) {
    if (scope.lane && item.lane !== scope.lane) continue;
    if (scope.workspace && !item.lane.startsWith(`${scope.workspace}/`)) continue;
    const bucket = buckets[Math.floor((new Date(item.at).getTime() - start) / size)];
    if (!bucket) continue;
    bucket.events += 1;
    bucket.cost.tokens += item.cost.tokens;
    bucket.cost.usd = Math.round((bucket.cost.usd + item.cost.usd) * 100) / 100;
    if (item.run_id && item.status in bucket.runs) bucket.runs[item.status as keyof ActivityBucket['runs']] += 1;
    if (item.effect === 'commit') bucket.approvals.requested += 1;
    if (item.effect === 'commit' && item.status !== 'cancelled') bucket.approvals.approved += 1;
  }

  const sum = (read: (bucket: ActivityBucket) => number) => buckets.reduce((total, bucket) => total + read(bucket), 0);
  return {
    range,
    bucket_seconds: seconds,
    buckets,
    totals: {
      events: sum((bucket) => bucket.events),
      runs: sum((bucket) => Object.values(bucket.runs).reduce((total, value) => total + value, 0)),
      approvals: sum((bucket) => bucket.approvals.requested),
      cost: { tokens: sum((bucket) => bucket.cost.tokens), usd: Math.round(sum((bucket) => bucket.cost.usd) * 100) / 100 },
    },
  };
}

/** Events per hour for the last 24 hours, oldest first. */
export function spark(data: OrgData, workspace: string): number[] {
  return activityMetrics(data, '24h', { workspace }).buckets.map((bucket) => bucket.events);
}

const NODE: Record<NodeRange, { seconds: number; count: number }> = {
  '1h': { seconds: 60, count: 60 },
  '24h': { seconds: 300, count: 288 },
  '7d': { seconds: 3_600, count: 168 },
};

function wave(seed: number, at: number): number {
  const hour = at / HOUR;
  return Math.sin(hour / 3.8 + seed) * 0.5 + Math.sin(hour / 1.3 + seed * 2) * 0.3 + Math.sin(hour * 2.1 + seed * 3) * 0.2;
}

export function nodeMetrics(node: NodeRecord, range: NodeRange): NodeMetrics {
  const { seconds, count } = NODE[range];
  const size = seconds * 1000;
  const end = Math.floor(Date.now() / size) * size;
  const seed = node.id.length;
  // hetzner-1 had a real outage six hours ago, so its charts have a real gap.
  const outage = node.id === 'hetzner-1' ? { from: Date.now() - 6.4 * HOUR, to: Date.now() - 5.7 * HOUR } : null;
  const points: NodePoint[] = Array.from({ length: count }, (_, index) => {
    const at = end - (count - 1 - index) * size;
    const iso = new Date(at).toISOString();
    if (outage && at >= outage.from && at <= outage.to) {
      return { at: iso, cpu_pct: null, mem_used_mb: null, mem_total_mb: null, disk_free_mb: null, warm_lanes: null, up: false };
    }
    const cpu = Math.round(Math.min(96, Math.max(3, node.load.cpu_pct + wave(seed, at) * 16)));
    const memory = Math.round(node.load.mem_used_mb * (1 + wave(seed + 1, at) * 0.08));
    // Disk fills slowly, then a cleanup frees some, so the line has a real shape.
    const disk = Math.round(node.load.disk_free_mb + ((end - at) / HOUR) * 18);
    return {
      at: iso,
      cpu_pct: cpu,
      mem_used_mb: memory,
      mem_total_mb: node.load.mem_total_mb,
      disk_free_mb: disk,
      warm_lanes: Math.max(0, Math.round(node.warm_lanes + wave(seed + 2, at) * 2)),
      up: true,
    };
  });
  return { range, bucket_seconds: seconds, points };
}
