import type { Cost } from './lanes';

export interface ActivityBucket {
  at: string;
  events: number;
  runs: { succeeded_verified: number; succeeded_unverified: number; partial: number; failed: number };
  approvals: { requested: number; approved: number; denied: number };
  cost: Cost;
}

export type ActivityRange = '24h' | '7d' | '30d';

export interface ActivityMetrics {
  range: ActivityRange;
  bucket_seconds: number;
  buckets: ActivityBucket[];
  totals: { events: number; runs: number; approvals: number; cost: Cost };
}

export type NodeRange = '1h' | '24h' | '7d';

export interface NodePoint {
  at: string;
  cpu_pct: number | null;
  mem_used_mb: number | null;
  mem_total_mb: number | null;
  disk_free_mb: number | null;
  warm_lanes: number | null;
  up: boolean;
}

export interface NodeMetrics {
  range: NodeRange;
  bucket_seconds: number;
  points: NodePoint[];
}
