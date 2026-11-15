import type { Effect, Principal, RunStatus } from './lanes';

// Proof: the action log replayed by account and day, checked, and shared as a read-only link
// (docs/API.md, "Audit"). The node's shapes, as the app reads them.

/** How well a row is covered by a signed checkpoint. The worst row of a step decides its step. */
export type ProofChainStatus = 'ok' | 'unprovable' | 'unchained' | 'tampered';

export interface ProofFinding {
  level: 'tampered' | 'unprovable' | 'note';
  text: string;
  /** The first and last row this sentence is about. */
  seq?: [number, number];
  mark?: number;
}

export interface ProofChain {
  status: ProofChainStatus;
  proven_through: { mark: number; seq: number; at: string } | null;
  findings: ProofFinding[];
}

export interface ProofStep {
  id: string;
  seq: number;
  at: string;
  ended_at: string | null;
  kind: string;
  tool: string | null;
  effect: Effect | null;
  /** `unfinished` while the call has no end yet. */
  status: string;
  summary: string;
  /** The call's own arguments, redacted at write. Technical details only. */
  args?: unknown;
  error: string | null;
  duration_ms: number | null;
  principal: Principal | null;
  /** The grant's label is words; its id is left out of a shared view. */
  grant: { id: string | null; label: string | null } | null;
  run: {
    id: string;
    flow: string | null;
    status: RunStatus | string;
    lane: string | null;
    summary: string | null;
    proof: { url: string | null; readbacks: unknown[]; note: string | null } | null;
  } | null;
  /** An image URL under the same scope as the day. Null when there is no screenshot. */
  screenshot: string | null;
  chain: { status: ProofChainStatus; rows: { seq: number; status: ProofChainStatus }[] };
}

/** One account's day, oldest first. */
export interface ProofReplay {
  lane: string;
  day: string;
  timezone: string;
  from: string;
  to: string;
  truncated: boolean;
  chain: ProofChain;
  steps: ProofStep[];
}

/** Which accounts acted on a day, the index a day's replay starts from. */
export interface ProofDayIndex {
  day: string;
  timezone: string;
  from: string;
  to: string;
  lanes: { lane: string; rows: number }[];
}

export interface ProofVerify {
  /** pending: every signed row checks out, and the newest wait for the next checkpoint. */
  status: 'ok' | 'pending' | 'tampered' | 'unverifiable';
  checked: number;
  rows: { first: number | null; last: number | null; in_scope: number };
  proven_through: { mark: number; seq: number; at: string } | null;
  /** The id of the chain the log was checked as. Not a status: the rows say that. */
  chain: string | null;
  keys: { kid: string; pub: string; from_mark: number | null }[];
  unchained: number;
  findings: ProofFinding[];
}

export type ProofShareState = 'active' | 'expired' | 'revoked';

export interface ProofShare {
  id: string;
  kind: 'trace' | 'lane_day';
  target: { trace?: string; lane?: string; day?: string; timezone?: string };
  screenshots: boolean;
  state: ProofShareState;
  created_at: string;
  expires_at: string;
  revoked_at: string | null;
  by: string;
  views: number;
  last_view_at: string | null;
}

/** A new link. The token is in `url`, shown once, and never listed again. */
export interface ProofShareCreated {
  id: string;
  token: string;
  kind: 'trace' | 'lane_day';
  target: ProofShare['target'];
  screenshots: boolean;
  expires_at: string;
  url: string;
  api_url: string;
}

export interface ProofShareInput {
  lane?: string;
  day?: string;
  trace?: string;
  /** `24h` or `7d`. */
  ttl?: string;
  screenshots?: boolean;
}

/** What a read-only link shows: one day of one account, or one step. */
export type ProofShared =
  | ({ share: { id: string; kind: 'trace' | 'lane_day'; expires_at: string } } & ProofReplay)
  | {
      share: { id: string; kind: 'trace' | 'lane_day'; expires_at: string };
      trace: string;
      lane: string | null;
      step: ProofStep;
      chain: ProofChain;
    };
