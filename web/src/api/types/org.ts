import type { BrainState, Cost, InviteState, NodeState, Role } from './lanes';
import type { GoalKey } from './setup';

export interface OrgSummary {
  id: string;
  slug: string;
  name: string;
  role: Role;
  plan: string;
  needs_you: number;
}

export interface Org {
  id: string;
  slug: string;
  name: string;
  plan: string;
  created_at: string;
  /** IANA zone, such as Europe/Vilnius. Sets digest hours for members who have not chosen one. */
  timezone?: string | null;
  /** The first job, and the owner's own words for "Something else". */
  goal?: GoalKey | null;
  goal_note?: string | null;
  /** On create only: the first workspace made with the org, when first run named one. */
  workspace?: string | null;
}

export interface SessionUser {
  id: string;
  email: string;
  name: string;
  email_verified: boolean;
  has_passkey: boolean;
  avatar_color?: string | null;
}

export interface Session {
  user: SessionUser;
  csrf: string;
  step_up_until: string | null;
  expires_at: string;
  orgs: OrgSummary[];
}

export interface Device {
  id: string;
  label: string;
  /** Null where the computer does not keep the address a session came from. */
  ip: string | null;
  location: string | null;
  last_active_at: string;
  current: boolean;
}

export interface Passkey {
  id: string;
  name: string;
  created_at: string;
  last_used_at: string | null;
}

export interface Member {
  id: string;
  user: { id: string; email: string; name: string };
  role: Role;
  workspaces: string[] | null;
  /** The role in each business (`*`: every one). A person's role can differ by business, so the app says so. */
  roles_by_workspace?: { workspace: string; role: Role }[];
  joined_at: string;
  last_active_at: string | null;
}

export interface Invite {
  id: string;
  email: string;
  role: Role;
  workspaces: string[] | null;
  invited_by: string;
  created_at: string;
  expires_at: string;
  state: InviteState;
  /**
   * Only on the answer that creates an invite on a computer: the code that opens it at
   * `/invite/{token}`. The computer keeps only a hash of it, so it is shown once.
   */
  token?: string;
}

export interface InvitePreview {
  org_name: string;
  role: Role;
  inviter: string;
  email?: string;
  /** "Mara", for "Mara asked you to help with Aster Coffee". */
  inviter_first_name?: string;
  /** The businesses this person will see, in words, or the org's name when it is all of them. */
  business?: string;
  /** Business names, not slugs. Null: the whole org. */
  workspaces?: string[] | null;
  /** "pr***@example.com": which address the link went to. */
  email_hint?: string;
  /**
   * True: opening this link is the sign-in (it went to their address), so one button creates
   * the account, accepts and signs in (`POST /api/invites/{token}/join`). False: an existing
   * account or an owner or admin invite, who sign in as usual and then accept.
   */
  one_click?: boolean;
  /**
   * How this invite is accepted. A computer has no email, so its invite is accepted with a passkey
   * (`POST /api/invites/{token}/passkey`). Absent: the link (or sign-in) described above.
   */
  join_by?: 'link' | 'passkey';
  expires_at?: string;
}

/** `POST /api/invites/{token}/join`: a whole session, and the org it joined. */
export interface InviteJoined extends Session {
  org: string;
}

export interface AuditEvent {
  id: string;
  at: string;
  actor: string;
  action: string;
  target: string;
  ip: string | null;
}

export interface PlanLimits {
  nodes: number;
  active_lanes: number;
  workspaces: number;
  members: number;
  retention_days: number;
}

export interface PlanUsage {
  nodes: number;
  active_lanes: number;
  workspaces: number;
  members: number;
}

export interface Status {
  node: string;
  version: string;
  csrf: string;
  needs_you: number;
  needs_you_detail?: {
    approvals: number;
    questions: number;
    lanes: string[];
    codes?: number;
    /** What did not go through after a yes (not counted again among the stopped runs). */
    outcomes?: number;
    /** Runs that stopped halfway and wait for the owner's say-so. */
    runs?: number;
    /** Accounts whose agent asked for a login. Also in `lanes`. */
    logins?: string[];
  };
  timezone?: string;
  cost_today: Cost;
  brain: { name: string; state: BrainState; offline_ready: boolean } | null;
  nodes: { id: string; state: NodeState; degraded: boolean }[];
  /** A computer has no plan, so it sends none. The billing screen says so. */
  plan?: string;
  /** Where an agent on this computer reaches agentos. A cloud sends none, and the cloud's address is used. */
  mcp_url?: string | null;
  /** A computer has no plan caps, so it sends none. Screens show the count without a limit. */
  limits?: PlanLimits;
  /** Counted for the owner only: a member is not told how many members there are. */
  usage?: PlanUsage;
}

export interface AuthProviders {
  magic_link: boolean;
  google: boolean;
  passkey: boolean;
  /** A computer signs its owner in with a one-time code from `agentos panel login`. */
  code?: boolean;
}

/** A device a member is signed in on: one browser or phone, by the way they signed in (docs/API.md, Members). */
export interface MemberSession {
  id: string;
  /** What the device said about itself, or how its person signed in. Never an id. */
  label: string;
  via: 'code' | 'passkey' | 'device' | string;
  created_at: string;
  last_seen_at: string;
  expires_at: string;
  /** Whether this is the session the page was read on. Only the member's own page can say so. */
  current: boolean;
}

/** One member, with the passkeys they sign in with and the devices they are signed in on. */
export interface MemberDetail extends Member {
  devices: { passkeys: Passkey[]; sessions: MemberSession[] };
}
