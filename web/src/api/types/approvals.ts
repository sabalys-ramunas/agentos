import type { ApprovalLaneState, ApprovalState, Effect, Outcome, Principal, Site } from './lanes';
import type { FormTrip } from './logins';
import type { WorkerThing } from './workers';

/**
 * What an approval shows, by kind (api-for-rebuild.md, the simulator's `jobs.py preview_for`).
 * Older clouds send none; the panel then reads the kind from the effect and the site.
 */
export type PreviewKind =
  | 'post'
  | 'email'
  | 'payment'
  | 'form'
  | 'order'
  | 'message'
  | 'delete'
  | 'command'
  | 'files'
  | 'setup'
  /** A git push: the remote and each ref with its commits (`push_refs`). */
  | 'push'
  /** A disposable cloud machine: its size, time limit and price. */
  | 'vm'
  /** A command in a lane, with what it syncs first (the first command of a new lane asks too). */
  | 'lane';

/** One ref of a git push, as the node's git gateway saw it. */
export interface PushRef {
  /** `refs/heads/main`, `refs/tags/v1.2`. */
  ref: string;
  /** The branch or tag name alone. */
  name: string;
  kind: 'create' | 'update' | 'delete';
  force: boolean;
  protected: boolean;
  /** Commits the push sends; null for a delete. */
  commits: number | null;
  /** Their one-line log, newest first, capped. Commit messages: data. */
  log: string[];
}

export interface PreviewItem {
  name: string;
  /** Bytes, for a file. */
  size?: number | null;
  quantity?: number | null;
  price?: number | null;
}

/** The structured preview. Every field is optional; each kind uses its own. */
export interface PreviewData {
  // email
  to?: string[];
  cc?: string[];
  from?: string;
  subject?: string;
  body?: string;
  attachments?: PreviewItem[];
  // payment, order, refund
  amount?: number;
  currency?: string;
  payee?: string;
  reference?: string;
  for?: string;
  from_account?: string;
  due?: string;
  order?: string;
  customer?: string;
  reason?: string;
  // what the node asks, in its words, when the kind's own question does not fit (a like the gate caught)
  ask?: string;
  yes?: string;
  no?: string;
  // form
  form?: string;
  fields?: { label: string; value: string; before?: string | null }[];
  changed?: string[];
  // message
  thread?: { from: string; text: string; stars?: number | null }[];
  reply?: string;
  // files, delete, command
  items?: PreviewItem[];
  command?: string;
  computer?: string;
  cwd?: string;
  destination?: string;
  // push
  remote?: string;
  push_refs?: PushRef[];
  // vm
  server_type?: string;
  vm_profile?: string;
  ttl_minutes?: number;
  price_hourly_eur?: number | null;
  /** The most it can cost for its whole time limit, in EUR. */
  max_cost_eur?: number | null;
  // lane: `computer` is the lane. A new lane has `lane_preset` and its settings in `fields`; a command in
  // one has `command`, `cwd` (above) and
  lane_preset?: string;
  timeout_s?: number;
  synced_files?: number | null;
  sync_dest?: string | null;
}

/**
 * How strong a yes must be, weakest first. A level is the
 * rule, a method is where it can be given. A person reads neither: words live in `lib/confirm.ts`.
 */
export type ConfirmLevel = 'chat' | 'signed_in' | 'present' | 'device_key';
export type ConfirmMethodId = 'panel' | 'cli' | 'passkey' | 'chat' | 'mac' | 'ios' | 'android' | 'watch' | 'totp';

export interface Approval {
  id: string;
  kind?: 'action' | 'question';
  state: ApprovalState;
  effect: Effect;
  /** Empty when the request has no lane. The wire says null, and the client evens that out. */
  lane: string;
  summary: string;
  content_hash: string;
  preview: { text?: string | null; image?: string | null; kind?: PreviewKind | null; data?: PreviewData | null };
  requested_by: Principal;
  created_at: string;
  expires_at: string;
  /** The tool or flow version being approved, from the node. */
  tool?: string | null;
  flow_target?: string | null;
  /** Why the node is asking, in its own words. */
  reason?: string | null;
  /** `sha256:` digest of the content, which a standing approval can be bound to. */
  content_digest?: string | null;
  /** After a decision: whether the node took it. `refused` means it is still waiting there. */
  delivery?: 'pending' | 'delivered' | 'refused' | null;
  delivery_error?: string | null;
  decided_by?: string | null;
  /** Who decided, as an id the app matches with the signed-in person: a member's id (the node writes their handle). */
  decided_by_id?: string | null;
  /** Who decided, in words, when it is a member (the node's member list). Null for the owner: say "The owner". */
  decided_by_name?: string | null;
  decided_at?: string | null;
  /** How strong a yes this needs, and where this person could give it. Absent on an older node. */
  requires?: ConfirmLevel | null;
  methods?: ConfirmMethodId[] | null;
  /** How the yes arrived. The node works these out; a caller never sets them. */
  decided_method?: ConfirmMethodId | null;
  decided_level?: ConfirmLevel | null;
  /** The chat app, or the device, by name. */
  decided_where?: string | null;
  /** The business slug and its own name, and "Aster Coffee's Instagram" ready for a sentence. */
  workspace?: string | null;
  workspace_name?: string | null;
  place?: string | null;
  site?: Site | null;
  /** The account right now. When `blocks`, a yes only queues the action. */
  lane_state?: ApprovalLaneState | null;
  /** Null until someone decides, and for a question. */
  outcome?: Outcome | null;
  /** Waiting for an answer, or failed after a yes and not yet cleared. */
  needs_you?: boolean;
  /** Until when the person who decided can still take it back (`POST .../undo`). Null once sent. */
  undo_until?: string | null;
}

/** One line of a batch reply. Only `ok: true` means the item was really decided. */
export type BatchResult = (Approval & { ok: true }) | { id: string; ok: false; error?: { code: string; message: string } };

export interface RefusedDecision {
  id: string;
  /** Kept from the screen, because a refused item can leave the queue. */
  summary: string;
  code: string;
  message: string;
}

/** A batch that did not all go through. It stays until a person dismisses it. */
export interface DecisionReport {
  id: string;
  decision: 'approve' | 'deny';
  decided: number;
  refused: RefusedDecision[];
}

/** An agent's question to a person, from `human_ask`. */
export interface Question {
  id: string;
  state: 'open' | 'answered' | 'declined' | 'expired';
  lane: string | null;
  question: string;
  options: string[] | null;
  answer: string | null;
  asked_by: Principal;
  created_at: string;
  expires_at: string;
  answered_at: string | null;
  answered_by: string | null;
  /** Concept stage: a worker asked, and this is how it got here. */
  worker?: WorkerThing | null;
  /** An agent asks the owner to connect a private repository (docs/ENVIRONMENTS.md). Absent on any other question. */
  repo_connect?: RepoConnect | null;
}

/**
 * What a repository card needs: the repository, why the node cannot read it, and GitHub's pages. The node checks a
 * token with GitHub before it keeps it, and the token is in no answer.
 */
export interface RepoConnect {
  repo: string;
  host: string;
  /** `refused`: the node had a key or token for it, and the host refused it. */
  reason: 'not_connected' | 'refused';
  /** GitHub only: where a person makes a fine-grained token, a classic one, and the repository's deploy keys. */
  token_url?: string;
  classic_token_url?: string;
  deploy_keys_url?: string;
}

/** The link to the node's own page for a card's token: a form trip, for the repository it names. */
export interface RepoTokenForm extends FormTrip {
  repo: string;
}

/** The card's answer once the node can read the repository. */
export interface RepoConnected {
  repo: string;
  connected: true;
  /** When GitHub says the token expires, or null when it does not say. */
  expires_at?: string | null;
}

/** The node's deploy key for the repository, for an admin of it to add. */
export interface RepoDeployKey {
  repo: string;
  name: string;
  public_key: string;
  fingerprint: string;
  settings_url: string;
}

/**
 * A one-time code a site asked for at its code step (docs/API.md, "Code cards"). The owner types it
 * and the node types it into the site. The code itself is never in this shape, nor in any answer.
 */
export interface CodeRequest {
  id: string;
  state: 'open' | 'used' | 'declined' | 'expired' | 'cancelled';
  lane: string;
  site: string;
  requested_by: Principal;
  created_at: string;
  expires_at: string;
}
