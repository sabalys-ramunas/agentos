import type { Effect } from './lanes';

export interface Grant {
  id: string;
  subject: string;
  workspaces: string[];
  lanes: string[] | null;
  ceiling: Effect;
  specials: Effect[];
  budget: string | null;
  expires_at: string | null;
  revoked: boolean;
  created_at: string;
}

export interface NewGrant {
  subject: string;
  workspaces: string[];
  lanes?: string[];
  ceiling: Effect;
  specials: Effect[];
  budget?: string;
  ttl: string;
}

/**
 * What the cloud knows about whether a standing approval does anything. Today every node reads
 * `not_applied`, and `message` says so in a sentence the panel shows as written.
 */
export interface Enforcement {
  state: 'no_lanes' | 'not_applied' | 'partial' | 'applied';
  applied_by: string[];
  not_applied_by: string[];
  message: string;
}

export type StandingState = 'active' | 'used_up' | 'expired' | 'revoked';

export interface StandingApproval {
  id: string;
  workspace: string;
  effect: Effect;
  /** Lane patterns inside the workspace. null is every lane in it. */
  lanes: string[] | null;
  /** How many times it may cover. Null when no limit was set (an "always allow" kind of approval). */
  count: number | null;
  used: number;
  remaining: number | null;
  per_day: number | null;
  until: string;
  /** `flow:<id>@<version>` or `tool:<name>`. */
  targets: string[];
  /** How many content digests it is bound to. */
  contents: number;
  covers_publishing: boolean;
  note: string | null;
  state: StandingState;
  created_at: string;
  revoked_at: string | null;
  enforcement: Enforcement;
}

export interface NewStandingApproval {
  workspace?: string;
  effect?: Effect;
  lanes?: string[] | null;
  count: number;
  per_day?: number | null;
  until: string;
  targets?: string[];
  from_approvals?: { id: string; content_hash: string }[];
  note?: string;
}

// An override: a safety check a person turned off for one account, and why (docs/API.md, Workspaces).
export interface Override {
  id: number;
  at: string;
  /** Who did it, as the node says it: the owner, or a person by a name the node keeps. */
  by: string;
  lane: string;
  proxy: string | null;
  /** The proxy rules that were bypassed. */
  rules: string[];
  /** The reason the person typed. Shown as text, never as markup. */
  reason: string;
}
