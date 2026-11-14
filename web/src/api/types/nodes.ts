import type { BrainKind, BrainState, Lane, LaneKind, NodeState, VaultKind } from './lanes';
import type { WorkerJob } from './workers';

export interface NodeLoad {
  cpu_pct: number;
  mem_used_mb: number;
  mem_total_mb: number;
  disk_free_mb: number;
}

export interface FleetNode {
  id: string;
  state: NodeState;
  capabilities: string[];
  cpu: number;
  mem: { used_mb: number; total_mb: number };
  warm_lanes: number;
  max_warm_lanes: number;
  last_seen: string;
  /** panel assumption on the list route: docs/API.md only pins it on the detail route. */
  version: string;
  degraded: boolean;
  timezone?: string;
  /** Runs (`agentos run` jobs) running on it now. Absent when the computer runs no terminal lanes. */
  jobs_running?: number;
  /** Its lanes by kind: browsers, terminals, services. */
  lanes_by_kind?: Partial<Record<LaneKind, number>>;
  /** Lanes a tool call holds right now. */
  lanes_active?: number;
  /** Approvals waiting for a person, that the caller may see. */
  approvals_waiting?: number;
}

export interface NodeDetail extends FleetNode {
  os: string;
  arch: string;
  load: NodeLoad;
  lanes: Lane[];
}

export interface JoinToken {
  id: string;
  token?: string;
  name?: string | null;
  expires_at: string;
  install_command?: string;
}

export interface Brain {
  name: string;
  kind: BrainKind;
  provider: string;
  state: BrainState;
  seats: { in_use: number; total: number } | null;
  window_used_pct: number | null;
  /** Concept stage: the computer that keeps it. Older data does not say. */
  computer?: string | null;
  /** Concept stage: the models under this one account. Their ids are for Technical details. */
  models?: BrainModel[];
}

export interface BrainModel {
  id: string;
  /** What it can do. An agent's own thinking is `agent`, a worker's job is the rest. */
  can: ('agent' | WorkerJob)[];
  /** Roughly, for Technical details only. */
  usd_per_step?: number | null;
}

/** Names only. There is no field for a secret, on purpose. */
export interface VaultEntry {
  id: string;
  name: string;
  kind: VaultKind;
  domain: string | null;
  lanes: string[];
  created_at: string;
  last_used: string | null;
}

/** The vault as the computer reports it. Names only: no secret, passphrase or key ever reaches the app. */
export interface VaultStatus {
  initialized: boolean | null;
  state: 'uninitialized' | 'sealed' | 'unsealed' | 'error';
  mode?: 'keyfile' | 'passphrase' | null;
  key_version?: number | null;
  entries?: number;
  error?: { code: string; message: string } | null;
}

// Handed to an AI model: a task a worker or an agent gave a model, with its state now (docs/API.md, "Brains").
export type BrainTaskStatus =
  | 'queued'
  | 'starting'
  | 'running'
  | 'waiting_approval'
  | 'succeeded_unverified'
  | 'failed'
  | 'cancelled'
  | 'interrupted';

export interface BrainTask {
  id: string;
  /** The business it was for, by id. Null when the task was not for one. */
  workspace: string | null;
  status: BrainTaskStatus;
  status_reason: string | null;
  /** The AI service it went to, by name. Null until one took it. */
  brain: string | null;
  attempt: number;
  tokens: number;
  turns: number;
  cost_usd: number | null;
  /** What it was asked to do, as a short preview of the prompt. */
  summary: string;
  started_at: string | null;
  finished_at: string | null;
  /** The account it worked on, when the list knows it. The list does not. */
  lane: string | null;
  /** Whether it can carry on from where it stopped. */
  resumable: boolean;
}

/** One task, with the last lines of what the model said. Those lines are for Technical details. */
export interface BrainTaskDetail extends BrainTask {
  transcript_tail: string[];
  tail: string[];
  attempts?: number | null;
}
