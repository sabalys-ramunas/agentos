import type { CheckStatus } from './fleet';
import type { LaneState } from './lanes';

// An account's files: the inbox agents fill and the outbox its downloads land in (src/agentos/api/web/files.py).

export type FileBox = 'inbox' | 'outbox';
export type FileStatus = 'complete' | 'downloading' | 'partial' | 'failed';

export interface AccountFile {
  id: string;
  box: FileBox;
  name: string;
  status: FileStatus;
  size: number;
  size_text: string;
  /** The type judged from the bytes: image, video, pdf, csv, txt, archive or other. */
  kind: string | null;
  mime: string | null;
  source: 'put' | 'url' | 'download';
  created_at: string;
  finished_at: string | null;
  error: string | null;
  sha256?: string | null;
}

export interface AccountFiles {
  lane: string;
  inbox: AccountFile[];
  outbox: AccountFile[];
  limits: { put_bytes: number; put: string; download_bytes: number; download: string };
}

// An account's page: what its node says about the account's pace, limits, queue, taint, check, keepalive,
// sites and watch link (docs/API.md "Posting pace", "Meters, queues and taint", "Lane check", "Lane keepalive",
// "Lane control"). Each shape is what the node really answers, field names included.

/** A posting pace as written: waking hours, the gap between posts and posts a day. */
export interface Pace {
  commits_per_day?: number | null;
  min_gap_minutes?: number | null;
  /** Windows as text, such as "08:00-22:00". */
  active_hours?: string[] | null;
  timezone?: string | null;
}

export type PaceBlock = 'active_hours' | 'min_gap' | 'commits_per_day' | 'timezone';

export interface PaceZone {
  timezone: string;
  source: 'lane' | 'pin' | 'workspace' | 'node';
  source_words: string;
  exit_timezone: string | null;
  unreadable: boolean;
  note: string | null;
}

export interface LanePace {
  lane: string;
  pace: Pace | null;
  paced: boolean;
  zone: PaceZone;
  /** The lane's own clock now, as an ISO time with its offset. */
  local_now: string;
  workspace_timezone: string | null;
  /** The recommended pace for a new account, what "Use recommended" sets. */
  preset: Pace;
  posts_today: number;
  last_post_at: string | null;
  may_post_now: boolean;
  blocked_by: PaceBlock | null;
  next_allowed_at: string | null;
  next_allowed_local: string | null;
}

/** A workspace's clock for lanes with no pin and no zone of their own. */
export interface WorkspaceZone {
  workspace: string;
  timezone: string | null;
}

export type LimitName = 'usd_per_day' | 'tokens_per_day' | 'commits_per_hour' | 'commits_per_day';

/** The limits a lane or a grant holds. An absent name has no limit. */
export type Limits = Partial<Record<LimitName, number>>;

export interface MeterLimit {
  name: LimitName;
  value: number;
  used: number;
  left: number;
  /** Used over the value, 0 to 1 and past 1 once it is over. */
  fraction: number;
  unit: string;
  period: 'day' | 'hour';
  resets_at: string;
  set_on: string | null;
  state: MeterState;
  explain: string;
}

export type MeterState = 'ok' | 'warning' | 'cut_off';

export interface MeterUsage {
  usd: number;
  tokens: number;
  commits_today: number;
  commits_this_hour: number;
  actions: Record<string, number>;
  actions_today: number;
}

export interface Meter {
  scope: 'lane' | 'grant';
  /** The lane's id, or the grant's. */
  id: string;
  label: string;
  state: MeterState;
  explain: string | null;
  usage: MeterUsage;
  limits: MeterLimit[];
  /** "owner", "policy.toml biz-a/*", or null when the lane has none. */
  limits_from: string | null;
  day: string;
  timezone: string;
  warn_at: number[];
  resets_at: string;
  hour_resets_at: string;
}

/** Who holds the account now. `label` is what people see, never a token. */
export interface LaneClaim {
  holder: string;
  kind: string;
  ref: string;
  since: string;
  purpose: string;
  held_s: number;
  implicit: boolean;
  ttl_s?: number;
  expires_in_s?: number;
  renewable_until_s?: number;
  run_id?: string;
  yours?: boolean;
}

export interface QueueWaiter {
  position: number;
  mode: string;
  label: string;
  kind: string;
  ref: string;
  run_id?: string;
  waited_s: number;
  estimate_s: number | null;
  kept_out_by_claim: boolean;
  yours?: boolean;
}

export interface QueueInside {
  mode: string;
  label: string;
  kind: string;
  ref: string;
  run_id?: string;
  for_s: number;
}

export interface LaneQueue {
  lane: string;
  claim: LaneClaim | null;
  queue_length: number;
  queue: QueueWaiter[];
  inside: QueueInside[];
  call_estimate_s: number | null;
}

export interface TaintFollow {
  kind: string;
  host: string | null;
  last: string;
}

/** A grant family that read untrusted text. Taint lives on the family, not the account. */
export interface TaintFamily {
  /** The owner sees these; anyone else does not. */
  grant_id?: string;
  grants?: { id: string; label: string }[];
  context: 'turn' | 'family';
  labels: string[];
  untrusted: boolean;
  since: string | null;
  follows: TaintFollow[];
  /** "a web page at evil.example and an email", or null. Text from outside never appears here. */
  after_untrusted: string | null;
  waits_for_a_person: string[];
  clean_context: string;
}

export interface LaneTaint {
  lane: string;
  tainted: boolean;
  families: TaintFamily[];
  /** The effects that wait for a person while the account is tainted. */
  waits_for_a_person: string[];
}

export type CheckLevel = CheckStatus | 'unknown';

export interface LaneCheckItem {
  id: string;
  title: string;
  level: CheckLevel;
  detail: string;
  fix: string | null;
  data?: Record<string, unknown>;
}

/** The checklist of `agentos lane check`, run now. Reads only: it never warms the account. */
export interface LaneCheck {
  lane: string;
  level: CheckLevel;
  safe: boolean | null;
  state: LaneState;
  at: string | null;
  summary: string;
  counts: Record<CheckLevel, number>;
  checks: LaneCheckItem[];
}

/** A lane's own last check, carried on the lane (`Lane.check`). */
export interface LaneCheckMark {
  level: CheckLevel;
  safe: boolean | null;
}

/** The worst level per account from the last run. Cheap: nothing is probed. */
export interface LaneCheckBrief {
  lane: string;
  level: CheckLevel;
  safe: boolean | null;
  state: LaneState | null;
  at: string | null;
  failing: string[];
  warning: string[];
}

export interface LaneChecks {
  lanes: LaneCheckBrief[];
  note?: string;
}

export interface KeepaliveVisit {
  at: string;
  finished_at: string;
  trigger: string;
  site: string | null;
  outcome: string;
  reason: string | null;
}

/** The keepalive plan of an account: a visit that keeps its sign-in warm, and when it last ran. */
export type LaneKeepalive =
  | { available: false; lane: string }
  | {
      available: true;
      lane: string;
      enabled: boolean;
      opted_out: boolean;
      site: string | null;
      home: string | null;
      status: 'signed_out' | 'challenged' | null;
      status_reason: string | null;
      signed_in: boolean | null;
      session_expires_at: string | null;
      blocked: { code: string; reason: string } | null;
      last_visit: KeepaliveVisit | null;
      visits: KeepaliveVisit[];
      next_at: string | null;
      due_at: string | null;
      visiting: boolean;
      waiting?: string;
    };

/** The domains an account may reach. An empty list is no restriction. */
export interface LaneSites {
  lane: string;
  sites: string[];
  strict: boolean;
}

/** A change to the site list, and what came of it. */
export interface LaneSitesChange extends LaneSites {
  changed: boolean;
  running: boolean;
}

/** A signed link to watch an account for a short time. The link is shown once. */
export interface ViewLink {
  /** A path on the computer, such as /view/biz-a/social?t=... */
  url: string;
  expires_at: string;
  mode: 'view' | 'control';
  owner: boolean;
}
