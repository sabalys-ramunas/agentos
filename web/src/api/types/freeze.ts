export type FreezeScope = 'lane' | 'workspace' | 'node' | 'mesh';

/** One freeze in force. `by` says who froze it (`owner`, `member:<id>`, `agent:<id>`, `push`), never a name. */
export interface FreezeRecord {
  id: string;
  scope: FreezeScope;
  /** The lane id, the workspace id, or the computer's name. */
  target: string;
  reason: string;
  by: string;
  at: string;
  via?: string | null;
  durable?: boolean;
  applied_at?: string | null;
  /** The grant ids this freeze revoked. */
  revoked?: string[];
}

export interface FreezeStatus {
  /** False on a node without the freeze service: nothing is frozen, and there is nothing to freeze with. */
  available: boolean;
  node?: string;
  frozen: FreezeRecord[];
  /** The accounts a freeze reaches, each with the freeze that reaches it, such as `lane aster/instagram`. */
  lanes: { lane: string; state: string; by: string }[];
  node_frozen: boolean;
  problem?: string | null;
  /** The latest entries of the freeze journal. Owner only. */
  journal?: { at?: string; event?: string; scope?: string; target?: string; by?: string }[];
}

/** What a freeze did, as the node reports it. Each count says what it stopped, revoked or paused. */
export interface FreezeReport {
  freeze_id: string;
  scope: FreezeScope;
  target: string;
  reason: string;
  by: string;
  already_frozen: boolean;
  frozen_at: string;
  durable?: boolean;
  details_saved?: boolean;
  /** The accounts the freeze reaches, and the ones among them that a warm browser keeps warm. */
  lanes: string[];
  lanes_warm: string[];
  stopped: { calls: number; ms: number; still_running: number };
  revoked: { grants: { id: string; subject: string; note: string }[]; cascades_to: string[]; standing: string[] };
  runs_cancelled: string[];
  schedules_paused: string[];
  triggers_paused?: string[];
  brain_tasks_cancelled?: string[];
  errors: string[];
  took_ms: number;
  warning?: string;
  next?: string;
}

export interface UnfreezeResult {
  scope: FreezeScope;
  target: string;
  unfrozen: boolean;
  was_frozen: boolean;
  freeze_id?: string | null;
  /** A wider freeze that still covers this scope, so nothing here is free yet. */
  still_frozen_by?: { scope: FreezeScope; target: string; reason: string } | null;
  next?: string;
}
