import type { LaneCheckMark, LaneClaim } from './account';
import type { GoalKey } from './setup';

export type LaneKind = 'browser' | 'terminal' | 'service' | 'android';
export type LaneState = 'cold' | 'warming' | 'warm' | 'active' | 'quarantined';
export type Signal = 'ok' | 'captcha' | 'checkpoint' | 'rate_limited' | 'blocked' | 'logged_out' | 'unknown';
export type LoggedInHint = 'needs_login' | 'ok' | 'unknown';
export type ControlState = 'agent' | 'human' | 'paused';
export type ControlAction = 'take' | 'hand_back' | 'stop_agent' | 'deny_request';
/**
 * One control action. `done` goes with a hand back only: whether the person says the check that holds the
 * lane (a robot check, a sign-in) is done. False keeps the hold, so agents stay off until it is. Left out,
 * a hand back clears holds as it always has.
 */
export interface ControlRequest {
  action: ControlAction;
  done?: boolean;
}
export type Readiness = 'ready' | 'needs_login' | 'needs_attention';
export type Health = 'ok' | 'attention' | 'blocked' | 'idle';
export type Effect =
  | 'read'
  | 'draft'
  | 'commit'
  | 'pay'
  | 'irreversible'
  | 'secret'
  | 'exec'
  | 'install_overlay'
  | 'install_base'
  | 'service'
  | 'brain'
  /** Send to, attach to or stop an agent lane. */
  | 'agent'
  /** Rent, extend or delete a disposable cloud machine. */
  | 'vm';
export type RunStatus =
  | 'queued'
  | 'running'
  | 'waiting_approval'
  | 'succeeded_verified'
  | 'succeeded_unverified'
  | 'partial'
  | 'failed'
  | 'cancelled';
export type Role = 'owner' | 'admin' | 'member' | 'approver' | 'viewer' | 'auditor';
export type NodeState = 'up' | 'degraded' | 'down';
export type ApprovalState = 'pending' | 'approved' | 'denied' | 'expired';
export type BrainState = 'ready' | 'cooling_down' | 'needs_auth' | 'down';
export type BrainKind = 'subscription' | 'local' | 'metered';
export type VaultKind = 'password' | 'totp' | 'cookie' | 'api_key';
export type InviteState = 'pending' | 'accepted' | 'revoked' | 'expired';

export interface Cost {
  tokens: number;
  usd: number;
}

export interface Proxy {
  label: string;
  /** The cloud sends the label alone and keeps the address on the node. Older mocks send both. */
  host?: string | null;
  port?: number | null;
}

/** Who is driving a lane. Part of every Lane, and carried by the `control` event. */
export interface LaneControl {
  state: ControlState;
  /** Who holds it, for display. Anyone can set their own name, so it never decides anything. */
  by: string | null;
  /**
   * The holder's user id (security review 7).
   * Once a response carries the key at all, it alone decides whether the holder is this person.
   */
  by_id?: string | null;
  since: string | null;
  /** Not kept by a node, which has no takeback request: left out there, so a screen reads it as none. */
  takeback_requested?: boolean;
  takeback_reason: string | null;
  takeback_by: string | null;
  last_human_input: string | null;
}

/**
 * Which site an account is for. `source` says how
 * the API knows: from the node's account, or from the lane's name.
 */
export interface Site {
  key: string;
  label: string;
  domain: string | null;
  source?: string | null;
}

/** An account's health in one plain word, worked out by the cloud from state, signal and the rest. */
export type HealthState =
  | 'ok'
  | 'asleep'
  | 'working'
  | 'quarantined'
  | 'wrong_account'
  | 'logged_out'
  | 'captcha'
  | 'checkpoint'
  | 'blocked'
  | 'rate_limited'
  | 'offline'
  | 'missing'
  | 'unknown';

export interface LaneHealth {
  state: HealthState;
  /** An approved action will not happen until this is fixed. */
  blocks: boolean;
  /** A plain sentence, or null when fine. Safe to show as it is. */
  reason: string | null;
  /** Which of Home's states it counts as (api-for-rebuild.md section 7). */
  face?: HomeFace | null;
}

/** Home's four states, plus working now, as the cloud counts them. */
export type HomeFace = 'all_good' | 'waiting' | 'stuck' | 'sleeping' | 'working';

/**
 * The newest picture the cloud can point at for an account: the readback screenshot of its last
 * run that has one, with the real post's link. A full 1024 x 640 PNG fetched from the computer on
 * request, so load it lazily and fall back to the site's mark.
 */
export interface LanePicture {
  kind: 'proof' | string;
  url: string;
  at: string;
  run_id: string | null;
  post_url: string | null;
}

/** On an approval: the account's health, plus the warning to show before a yes. */
export interface ApprovalLaneState extends LaneHealth {
  warning: string | null;
}

export type OutcomeState = 'pending' | 'done' | 'partial' | 'failed' | 'not_run' | 'unknown';

/** What came of a decided approval. Written by the cloud in the panel's words. */
export interface Outcome {
  state: OutcomeState;
  /** One sentence: "Couldn't post to Aster Coffee's Instagram". */
  title: string;
  reason: string | null;
  /** The computer's own words, for Technical details only. */
  detail: string | null;
  source: 'decision' | 'run' | 'call' | 'node' | 'trace';
  at: string;
  run_id: string | null;
  proof: { url: string | null; screenshot: string | null } | null;
  cleared_at: string | null;
  cleared_by: string | null;
}

export interface LaneAccount {
  site: string;
  user_id: string | null;
  source: string | null;
  status: 'match' | 'wrong_account' | 'unpinned';
}

export interface Lane {
  id: string;
  /** When the lane's node stopped reporting it. It stays on that node until an admin releases it. */
  missing_since?: string | null;
  workspace: string;
  name: string;
  kind: LaneKind;
  engine: string | null;
  preset: string;
  state: LaneState;
  signal: Signal;
  proxy: Proxy | null;
  exit_ip: string | null;
  last_used: string | null;
  node: string;
  quarantine_reason: string | null;
  logged_in_hint: LoggedInHint;
  /** Present when the lane is warm. */
  viewer?: { url: string } | null;
  control: LaneControl;
  /** Why agents are held off this lane, and when that ends. No end means a person clears it. */
  hold: string | null;
  hold_until: string | null;
  /** Which rule produced the signal, such as "instagram challenge URL". */
  signal_detail: string | null;
  account: LaneAccount | null;
  /** Optional on a heartbeat: a node may report its share of the machine per lane. */
  cpu_pct?: number | null;
  mem_mb?: number | null;
  /** Who holds the account now, and how many calls wait for it (`GET .../queue` has the places). */
  claim?: LaneClaim | null;
  queue_length?: number;
  /** Switched to hold still by a person, so no call goes to it. */
  frozen?: boolean;
  /** From the login queue. */
  readiness?: Readiness;
  readiness_note?: string | null;
  login_url?: string | null;
  /** The account's health in one word. Use this, never `signal` alone, for the status word. */
  health?: LaneHealth | null;
  /** The lane's own last check, from its last run (nothing is probed). Null before any run. A failing check makes the account stuck. */
  check?: LaneCheckMark | null;
  site?: Site | null;
  /** On `GET /workspaces` only: the account's last picture, or null. */
  picture?: LanePicture | null;
  /** On `GET /workspaces` only, while the account is awake: its screen right now (member and up). */
  live_picture?: string | null;
  /** An agent asked for a login on this account and nobody has started one yet. Null when none asked. */
  login_request?: LoginRequest | null;
}

/** An agent's open request for a login: why, which agent asked, and when (`login_request` on a lane). */
export interface LoginRequest {
  lane: string;
  why: string;
  by: string;
  at: string;
}

export interface Workspace {
  id: string;
  name: string;
  color: string;
  goal?: GoalKey | null;
  goal_note?: string | null;
  health: Health;
  summary: string;
  lanes: Lane[];
  /** Events per hour for the last 24 hours, oldest first. */
  spark?: number[];
  /** The business's one word, worst first. */
  state?: HomeFace;
  /** Approvals waiting, failed outcomes nobody cleared, and accounts a person can fix. */
  needs_you?: number;
  /** Accounts by Home's states; each account is in exactly one. */
  counts?: Record<HomeFace, number>;
}

export interface Principal {
  kind: 'agent' | 'human' | 'flow' | 'brain' | 'worker';
  id: string;
  thread?: string | null;
  /** A member's name in words, from the node's member list. Never shown in place of it: the id is not words. */
  name?: string | null;
}
