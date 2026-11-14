/**
 * Resource shapes, exactly as docs/API.md and docs/WEB-NODE.md define them. Fields marked
 * "panel assumption" are not pinned by either document yet and are listed in DESIGN.md
 * so the API side can confirm them.
 */

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

export interface TraceEvent {
  id: string;
  at: string;
  kind: string;
  tool: string;
  lane: string;
  /**
   * The cloud sends the full Principal (`{ kind, id, thread }`); older data and the topology feed
   * send the id alone. Read it through `lib/principal`, never render it raw.
   */
  principal: Principal | string;
  effect: Effect;
  status: RunStatus;
  summary: string;
  cost: Cost;
  run_id: string | null;
  /** A lane control's action (take over, hand back, ...), when this row is one; null otherwise. Additive. */
  control?: ControlAction | null;
}

export interface Page<T> {
  items: T[];
  next_before: string | null;
}

export interface RunStep {
  id: string;
  at: string;
  tool: string;
  summary: string;
  status: RunStatus;
}

export interface Run {
  id: string;
  flow: string | null;
  lane: string;
  status: RunStatus;
  summary: string;
  principal: Principal | string;
  started_at: string;
  finished_at: string | null;
  cost: Cost;
  proof: { url?: string | null; id?: string | null; screenshot?: string | null } | null;
  steps?: RunStep[];
  /** What the run does to the world. The cloud sends it; older data may not. */
  effect?: Effect;
  /** The approval a person gave first, when it needed one. */
  approval_id?: string | null;
  /** Set while a run stopped halfway and waits for a person to say whether the step went through. */
  decision_needed?: string | null;
}

export interface FlowParam {
  type: 'string' | 'number' | 'boolean';
  label?: string;
  required?: boolean;
}

export interface Flow {
  id: string;
  lane: string;
  /** A workflow's count of saved versions. A recorded routine's content hash, which an approval binds to. */
  version: number | string;
  params: Record<string, FlowParam>;
  effect: Effect;
  last_run: Pick<Run, 'id' | 'status' | 'started_at' | 'cost'> | null;
  /** Newest first, at most 20. */
  recent_runs?: { id: string; status: RunStatus; at: string }[];
  /** "Ask me first" (api-for-rebuild.md 20). Its own switch. Absent from an older cloud. */
  ask_first?: boolean;
  ask_first_by?: 'person' | 'agent' | null;
  ask_first_set_by?: AskFirstSetBy | null;
  ask_first_set_at?: string | null;
  /** Why the computer turned it on itself, in its words: `whyWords` makes them plain. Text only. */
  ask_first_why?: string | null;
  /** The account's switch, which makes this routine ask too. */
  account_asks_first?: boolean;
  /** A payment or something that cannot be undone: it asks whatever the switch says. */
  always_asks?: boolean;
  /** What really happens on its next run. */
  asks_first?: boolean;
  /** `computer`: its computer has no switches yet and asks before every commit. */
  asks_because?: 'routine' | 'account' | 'always' | 'computer' | null;
  /** False: its computer has no switches (`hello.supports`). */
  ask_first_supported?: boolean;
  /** False: its computer is away, so a change cannot reach it. */
  ask_first_online?: boolean;
  /** The routine itself. A computer that only keeps recordings sends none of these. */
  name?: string | null;
  trigger?: RoutineTrigger | null;
  steps?: RoutineStep[] | null;
  /** Every site it touches, first step first. */
  sites?: string[];
  /** Off: nothing starts it, though someone can still run it by hand. */
  enabled?: boolean;
  /** A newer version is saved over one that is on, and is not on until someone allowed to turn it on does. */
  needs_review?: boolean;
  next_at?: string | null;
  /** What its newest finished run came to, in a few words. */
  latest?: { at: string; result: string; status: RunStatus } | null;
  /** Its newest run stopped and nobody has dealt with it. */
  stuck?: { step: string; label: string; words: string; run: string; lane?: string | null; fix?: string | null } | null;
  /** Its newest run is waiting for a person. */
  waiting?: { run: string; words: string; approval_id?: string | null } | null;
  always_asks_steps?: RoutineAlwaysAsks[];
  send_limits?: RoutineSendLimit[];
  /** False: its computer has an older agentos, which runs a routine but cannot change its steps. */
  steps_supported?: boolean;
}

/** Who set a switch last: a member of this org (by user id), an agent, or the computer's owner on the computer. */
export interface AskFirstSetBy {
  kind: 'user' | 'agent' | 'computer';
  id: string | null;
  /** How a person set it, when it was not here: `cli` is the computer's own command line. */
  via?: 'cli' | null;
}

/** An account's "Ask me first", for what an agent does there outside any routine. */
export interface LaneAskFirst {
  lane: string;
  ask_first: boolean;
  /** What really happens: true on a computer without the switch, which asks about everything. */
  asks_first: boolean;
  ask_first_set_by: AskFirstSetBy | null;
  ask_first_set_at: string | null;
  ask_first_supported: boolean;
  ask_first_online: boolean;
}

export interface Schedule {
  id: string;
  /** The routine it starts. Empty for a schedule with no routine, which the computer keeps and nothing a person knows starts. */
  flow: string;
  /** The routine's own schedule, made with it: on and off with the routine, and its time is set in the routine. */
  routine?: boolean;
  cron?: string | null;
  watch?: string | null;
  params: Record<string, unknown>;
  deadman_minutes?: number | null;
  /** The same instant as next_fire_at, in UTC. */
  next_run?: string | null;
  enabled?: boolean;
  /** The next slot in the node's timezone. Null when paused, or for a watch. */
  next_fire_at?: string | null;
  lane?: string | null;
  last_run?: { id: string; status: RunStatus } | null;
}

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

/**
 * A form the computer serves for something only a person may type (a vault passphrase, a password, a
 * workspace's passphrase, a file). The app starts it and opens `form_url` on the computer; the app never
 * holds the value. `stage` is how far it got: waiting, done, or ended (it ran out or was closed).
 */
export type FormKind = 'vault_unlock' | 'vault_entry' | 'proxy' | 'workspace_export' | 'workspace_import' | 'login';
export type FormStage = 'waiting' | 'done' | 'ended';
export interface FormTrip {
  id: string;
  kind: FormKind;
  stage: FormStage;
  subject: string | null;
  form_url: string | null;
  expires_at: string;
}

/** One sign-in, as the lane's login service reports it (`GET /api/orgs/{org}/lanes/{ws}/{name}/login`). */
export type LoginState = 'none' | 'working' | 'ask' | 'waiting' | 'stopped' | 'paused' | 'signed_in' | 'cancelled' | 'done';
export type LoginStepKind = 'sign_in' | 'code' | 'approve' | 'choose' | 'confirm' | 'captcha' | 'stuck' | 'signed_in';
export interface LoginField {
  name: string;
  label: string;
  secret: boolean;
  length: number | null;
  autocomplete: string | null;
}
export interface LoginChoice {
  id: string;
  label: string;
  kind: string;
}
export interface LoginStep {
  id: string;
  kind: LoginStepKind;
  heading: string;
  message: string;
  number: string | null;
  fields: LoginField[];
  choices: LoginChoice[];
  submit: string | null;
  reason: string | null;
}
export interface LaneLogin {
  id?: string;
  lane: string;
  state: LoginState;
  site?: string | null;
  site_name?: string | null;
  host?: string | null;
  step?: LoginStep | null;
  note?: string;
  error?: string | null;
  version?: number;
  can_save?: boolean;
  saved?: string | null;
  by?: string | null;
}

/** A browser device: one fingerprint and one proxy, shared by a company's lanes. Listed by the org's browser devices route. */
export interface BrowserDevicePin {
  os: string;
  screen: string;
  window: string;
  gpu: string | null;
  timezone: string;
  locale: string;
  firefox: number;
  pinned_at: string | null;
  pinned_exit_ip: string | null;
  pinned_country: string | null;
}
export interface BrowserDevice {
  name: string;
  proxy: string | null;
  created_at: string;
  from_lane: string | null;
  note: string | null;
  pinned: boolean;
  pin: BrowserDevicePin | null;
  lanes: { id: string; state: string; proxy: string | null; pin_copy: boolean }[];
  workspaces: string[];
}

/** What moving a lane to another device changes, in plain words (`device-move`). */
export interface DeviceMovePlan {
  lane: string;
  from: string | null;
  to: string | null;
  changes: string[];
  warnings: string[];
  confirm: boolean;
  proxy: { from: string | null; to: string | null };
  done?: boolean;
}

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

export interface QuietHours {
  enabled: boolean;
  from: string;
  to: string;
  timezone: string;
  /** Payments and irreversible actions still come through. */
  urgent_breaks_through: boolean;
}

export interface NotificationSettings {
  /** The zone in use: the person's own, else the org's, else UTC. */
  timezone?: string;
  timezone_source?: 'person' | 'org' | 'default';
  /** Ready to show: "08:00, Vilnius time". */
  digest?: { time: string; timezone: string; label: string };
  push: boolean;
  email_digest: 'off' | 'daily' | 'weekly';
  /** Email at once for payments and irreversible actions. */
  email_urgent: boolean;
  quiet_hours: QuietHours;
  /** Routine approvals wait this long, so several arrive as one push. */
  batch_minutes: number;
}

export interface PushConfig {
  available: boolean;
  reason: 'not_configured' | 'plan' | null;
  public_key: string | null;
}

export interface PushDevice {
  id: string;
  label: string;
  created_at: string;
  last_sent_at: string | null;
}

export type WebhookEvent = 'approval.requested' | 'approval.decided' | 'approval.expired' | 'run.failed';

export interface WebhookDelivery {
  id: string;
  kind: WebhookEvent | 'ping';
  state: 'pending' | 'sending' | 'delivered' | 'failed' | 'dropped';
  attempts: number;
  /** The receiver's HTTP status, when it answered. */
  status: number | null;
  error: string | null;
  next_attempt_at: string | null;
  created_at: string;
  delivered_at: string | null;
}

export interface Webhook {
  id: string;
  /** Scheme, host and port only. The full URL never comes back. */
  origin: string;
  events: WebhookEvent[];
  enabled: boolean;
  created_at: string;
  last_delivery: WebhookDelivery | null;
}

export interface Retention {
  /** A computer has no plan, so it sends none. */
  plan?: string;
  retention_days: number;
  /** Longer than `retention_days` while a downgrade's hold lasts. */
  kept_days: number;
  hold_until: string | null;
  oldest_at: string | null;
  can_export: boolean;
}

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
    /** Engagement plans waiting for the owner's yes. */
    plans?: number;
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

export interface LaneMetrics {
  range: string;
  bucket_seconds: number;
  /** A computer keeps no signal history per bar, so it sends none: a bar with no signal reads as plain runs. */
  buckets: { at: string; runs: number; failed: number; signal?: Signal }[];
}

export interface QueueLane {
  id: string;
  workspace: string;
  name: string;
  platform: string;
  readiness: Readiness;
  note: string | null;
  login_url: string | null;
}

/** What is left to log in. Counts cover every lane in scope, including ready ones. */
export interface LoginQueue {
  summary: string;
  ready: number;
  total: number;
  needs_login: number;
  needs_attention: number;
  workspaces: { id: string; name: string; summary: string; ready: number; total: number }[];
  lanes: QueueLane[];
}

export interface TemplateInfo {
  id: string;
  title: string;
  description: string;
  surfaces: { name: string; platform: string }[];
}

export interface PlanStep {
  lane: string;
  workspace: string;
  surface: string;
  platform: string;
  engine: string;
  proxy: string | null;
  action: 'create' | 'exists' | 'conflict';
  reasons: string[];
  warnings: string[];
}

export interface TemplatePlan {
  template: string;
  workspaces: string[];
  ok: boolean;
  counts: { create: number; exists: number; conflict: number };
  steps: PlanStep[];
}

/** `partial` is a 200: what is listed in `created` is real, and a second run finishes the rest. */
export interface ApplyResult {
  status: 'complete' | 'partial';
  created: string[];
  existing: string[];
  failed: { lane: string; code: string; message: string } | null;
  remaining: string[];
  warnings: string[];
}

// The fleet map and node depth: every computer, what runs on it, and how it is doing.

export type EntityType = 'agent' | 'cloud' | 'node' | 'workspace' | 'lane' | 'site' | 'brain' | 'proxy' | 'schedule';
export type EntityState = 'ok' | 'attention' | 'blocked' | 'idle' | 'down';
export type LinkKind = 'attached' | 'runs_on' | 'belongs_to' | 'acts_on' | 'exits_via' | 'uses_brain' | 'triggers';

export interface TopologyEntity {
  id: string;
  type: EntityType;
  label: string;
  state: EntityState;
  ref: { href: string } | null;
  /** Free-form and node-supplied, so read with care and render as text. */
  stats: Record<string, unknown>;
}

export interface TopologyLink {
  from: string;
  to: string;
  kind: LinkKind;
  calls: number;
  failed: number;
  last_at: string | null;
}

export interface TopologyActive {
  lane: string;
  principal: string;
  tool: string;
  since: string;
  run_id: string | null;
}

/** A call in the last ten minutes, newest first (api-for-rebuild.md section 6). */
export interface TopologyRecent {
  at: string;
  lane: string;
  principal: string;
  kind: 'agent' | 'flow' | 'human';
  tool: string;
  status: RunStatus;
  /** The cloud's own line: "A routine checked Meridian Travel's Reddit" (section 11). */
  sentence?: string;
  /** "Aster Coffee's Instagram". */
  place?: string;
  site?: string;
  action?: string;
}

/** While you slept: `GET /topology/night` (api-for-rebuild.md section 11). */
export interface TopologyNight {
  timezone: string;
  from: string;
  to: string;
  /** "11 pm to 7 am". */
  label: string;
  /** False while the night is still under way; the numbers then run to now. */
  over: boolean;
  actions: number;
  accounts: number;
  failed: number;
  posts: number;
  outages: { node: string; from: string; to: string | null; minutes: number; caught_up: boolean; sentence: string }[];
  stopped: { lane: string; place: string; at: string; face: string; sentence: string }[];
  hours: { at: string; actions: number; failed: number }[];
  sentence: string;
}

export interface Topology {
  generated_at: string;
  window_seconds: number;
  entities: TopologyEntity[];
  links: TopologyLink[];
  active: TopologyActive[];
  /** Older clouds do not send it. */
  recent?: TopologyRecent[];
}

export interface TopologyEvent {
  at: string;
  lane: string;
  principal: string;
  tool: string;
  effect: Effect;
  status: RunStatus;
}

export interface NodeSystem {
  os: string;
  arch: string;
  kernel: string | null;
  cpu_model: string | null;
  cores: number | null;
  mem_total_mb: number | null;
  disk_total_mb: number | null;
  gpu: string | null;
  timezone: string | null;
  uptime_s: number | null;
  version: string;
  enrolled_at: string | null;
  enrolled_by: string | null;
  key_fingerprint: string | null;
  protocol: number;
  reported_at: string | null;
}

export type ProgramScope = 'node' | 'lane' | 'base_image';

export interface NodeProgram {
  name: string;
  version: string;
  source: string;
  scope: ProgramScope;
  lanes: string[];
  pinned_hash: string | null;
  /** Not every node can say whether a program is signed. Absent means not reported, not unsigned. */
  signed?: boolean;
  installed_at: string | null;
}

export interface NodeService {
  name: string;
  lane: string | null;
  state: string;
  restarts: number;
  started_at: string | null;
  /** Not every node keeps a service's ports. Absent means not reported. */
  ports?: number[];
  cpu_pct: number | null;
  mem_mb: number | null;
}

export interface ToolDef {
  name: string;
  description?: string | null;
  effect?: Effect | null;
}

export type CheckStatus = 'ok' | 'warn' | 'fail';

export interface HealthCheck {
  name: string;
  status: CheckStatus;
  detail: string;
  at: string;
  /** A stable id for the check. Absent on a node that predates it. */
  id?: string;
  /** What to do about it, the exact command or edit. Null when nothing needs doing. */
  fix?: string | null;
}

/** Every check, run on request, with the proxies probed too. Read-only. */
export interface HealthRun {
  ran_at: string;
  checks: HealthCheck[];
}

/** The node's own health round, as its daemon last ran it. Null when the daemon does not answer. */
export interface HealthRound {
  at: string | null;
  level: 'ok' | 'warn' | 'critical' | null;
  findings: { key: string; kind: string; level: string; title: string; detail: string }[];
}

export type BackupState = 'verified' | 'failed' | 'not_checked';

export interface BackupCheck {
  state: BackupState;
  at: string | null;
  error: string | null;
}

export interface BackupItem {
  name: string;
  created_at: string;
  size: number;
  check: BackupCheck;
}

/** A backup or a check, running (or ended) in this node process. */
export interface BackupJob {
  id: string;
  kind: 'backup' | 'check';
  name: string | null;
  state: 'running' | 'done' | 'failed';
  message: string;
  started_at: string;
  finished_at: string | null;
}

export interface NodeBackups {
  /** Where the node keeps them, on its own disk. */
  folder: string;
  /** How many of this node's backups are kept. */
  keep: number;
  /** Newest first. */
  items: BackupItem[];
  job: BackupJob | null;
}

/** One trip to the node's own passphrase form. Holds no passphrase: the form is where it is typed. */
export interface PassphraseTrip {
  id: string;
  kind: 'backup' | 'check';
  name: string | null;
  stage: 'waiting' | 'running';
  form_url: string | null;
}

export interface NodeUpgrade {
  running: string;
  /** The newer version this node knows of. It cannot see one, so this is null. */
  newer: string | null;
  command: string;
  python: string;
}

export interface EnvLaneRow {
  id: string;
  kind: string;
  preset: string;
  state: string;
  isolation: string | null;
  network: string;
  base_image: string | null;
  last_used: string | null;
  quarantine_reason: string | null;
}

export interface EnvServiceRow {
  lane: string;
  name: string;
  state: string;
  desired: string;
  health: string;
  restarts: number;
  last_exit: number | null;
  started_at: string | null;
}

export interface ServiceView {
  lane: string;
  desired: string;
  state: string;
  health: string;
  restarts: number;
  last_exit: number | null;
  started_at: string | null;
}

export interface ServiceLog {
  lane: string;
  log: string;
}

export interface NodeEnvironments {
  lanes: EnvLaneRow[];
  services: EnvServiceRow[];
  images: {
    templates: { name: string; description: string; placeholder: boolean }[];
    built: { ref: string; name: string; version: number; built_at: string | null; size_bytes: number }[];
  };
}

export type IdentityState = 'none' | 'off' | 'signing' | 'stale' | 'unreadable';

export interface SignSite {
  origin: string;
  reason: string;
  added_by: string | null;
  added_at: string | null;
}

export interface IdentityAccount {
  lane: string;
  state: IdentityState;
  /** The public key's id. The private key never leaves the node and is never sent. */
  keyid: string | null;
  signature_agent: string | null;
  hosts: SignSite[];
}

export interface NodeIdentity {
  accounts: IdentityAccount[];
}

export interface SettingRow {
  name: string;
  value: string | number | boolean | string[] | null;
  source: string;
  description: string;
}

export interface ScheduleFire {
  at: string;
  status: RunStatus | 'missed';
  run_id?: string | null;
}

export interface NodeSchedule extends Schedule {
  recent_fires: ScheduleFire[];
  next_fires: string[];
}

export type ConnectionKind = 'connected' | 'disconnected' | 'degraded' | 'recovered' | 'backlog_flushed';

export interface NodeConnections {
  uptime_pct: number;
  days: { day: string; up_pct: number; incidents: number }[];
  events: { at: string; kind: ConnectionKind; detail: string | null }[];
}

export type LogSource = 'agentosd' | 'scheduler' | 'channel' | 'lane' | 'service';
export type LogLevel = 'debug' | 'info' | 'warn' | 'error';

export interface LogLine {
  at: string;
  source: LogSource;
  level: LogLevel;
  message: string;
  lane: string | null;
}

export type TopologyWindow = '1h' | '24h';

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

/** The SSE stream. Payloads are shaped like the matching resource. */
export type LiveEvent =
  | { type: 'lane'; payload: Lane }
  | { type: 'control'; payload: { lane: string; control: LaneControl } }
  | { type: 'approval'; payload: Approval }
  | { type: 'question'; payload: Question }
  | { type: 'code'; payload: CodeRequest }
  | { type: 'freeze'; payload: { state: 'frozen' | 'unfrozen'; scope: FreezeScope; target: string } }
  | { type: 'run'; payload: Run }
  | { type: 'trace'; payload: TraceEvent }
  | { type: 'node'; payload: FleetNode }
  | { type: 'schedule'; payload: Schedule & { deleted?: boolean } }
  | { type: 'brain'; payload: Brain }
  | { type: 'task'; payload: unknown }
  /** A limit, a pace or a cutoff changed. `{}` for a member or a grant, who refetch the meters. */
  | { type: 'meter'; payload: { scope?: 'lane' | 'workspace' | 'grant'; id?: string; kind?: string } }
  | { type: 'resync'; payload: unknown };

export const LIVE_EVENT_TYPES = [
  'lane',
  'control',
  'approval',
  'question',
  'code',
  'freeze',
  'run',
  'trace',
  'node',
  'schedule',
  'brain',
  'task',
  'meter',
  'resync',
] as const;

// History as stories (api-for-rebuild.md, section 9): `GET /api/orgs/{org}/history`.

export type HistoryAction = 'post' | 'publish' | 'reply' | 'draft' | 'read' | 'pay' | 'change' | 'task';
export type HistoryVerdict = 'checked' | 'not_checked' | 'partial' | 'failed' | 'not_run' | 'running';
export type HistoryShow = 'everything' | 'posts' | 'problems' | 'spending';

export interface HistoryWho {
  kind: Principal['kind'];
  id: string;
  /** "Claude Code", a routine as "Instagram post", a person by name. */
  name: string;
  brand: string | null;
}

/**
 * What kind of work a story was, for History's filters: social posting is one use case of many.
 * Optional: a story without it is placed from its `action`.
 */
export type StoryKind = 'sent' | 'money' | 'forms' | 'research' | 'shop' | 'files';

/** The thing itself, shown as the proof: what was sent, saved, paid, submitted, found or run. */
export type StoryArtifact =
  | {
      type: 'email';
      from: string;
      to: string[];
      cc?: string[];
      subject: string;
      body: string;
      attachments?: { name: string; size: number }[];
    }
  | { type: 'files'; folder?: string | null; files: { name: string; size: number; url?: string | null }[] }
  | { type: 'payment'; amount: string; payee: string; for?: string | null; from_account?: string | null; reference?: string | null }
  | {
      type: 'form';
      title: string;
      fields: { label: string; value: string; before?: string | null; changed?: boolean }[];
      confirmation?: string | null;
    }
  | { type: 'table'; title: string; columns: string[]; rows: { cells: string[]; change?: 'up' | 'down' | 'new' | null }[]; summary?: string | null }
  | {
      type: 'order';
      number: string;
      customer?: string | null;
      items: { name: string; quantity: number; price: string }[];
      total: string;
      refund?: boolean;
      note?: string | null;
    }
  | { type: 'message'; thread: { from: string; text: string; at?: string | null; ours?: boolean }[]; rating?: number | null }
  | { type: 'command'; command: string; output?: string | null; computer?: string | null };

export interface HistoryStory {
  /** The run id, or the trace id for a call a real node made without a run. */
  id: string;
  source: 'run' | 'call';
  at: string;
  started_at: string;
  lane: string;
  workspace: string | null;
  workspace_name: string | null;
  /** "Aster Coffee's Instagram". */
  place: string;
  site: { key: string; label: string };
  action: HistoryAction;
  /** "Posted a photo on Harbor Supply's Instagram". */
  sentence: string;
  verdict: HistoryVerdict;
  verdict_text: string;
  reason: string | null;
  /** The node's own words, for Technical details. */
  detail: string | null;
  status: RunStatus;
  proof: { url: string | null; screenshot: string | null } | null;
  who: HistoryWho;
  /** `on_its_own`: nobody was asked, so it ran on its own (api-for-rebuild.md 20). */
  why: { trigger: string | null; text: string | null; approval: { by: string | null; at: string; state: string; method?: ConfirmMethodId | null; level?: ConfirmLevel | null; where?: string | null } | null; on_its_own?: boolean };
  steps: { id: string; at: string; label: string; status: RunStatus }[];
  technical: { run_id: string | null; flow?: string | null; tool?: string | null; effect: Effect | null; cost: Cost };
  /** Working now only: "Claude Code is posting on Aster Coffee's Instagram". */
  headline?: string;
  /** What kind of work, for the filters. Placed from `action` when the cloud does not say. */
  kind?: StoryKind | null;
  /** Concept stage: a worker did the steps, and these are the steps. */
  worker?: WorkerThing | null;
  /** What was sent, saved, paid, submitted, found or run, as the proof. */
  artifact?: StoryArtifact | null;
  /** The same structured preview an approval carries, when the cloud sends that instead. */
  preview?: { kind?: PreviewKind | null; data?: PreviewData | null } | null;
}

export interface HistoryReads {
  times: number;
  accounts: number;
  /** "Also checked 28 accounts 398 times." */
  sentence: string;
  by_account: { lane: string; place: string; site: { key: string; label: string }; times: number; last_at: string }[];
}

export interface HistoryDay {
  /** The day in the person's time zone, `2026-11-04`. */
  date: string;
  label: string;
  /** "12 things done · 11 checked · 1 payment · 2 problems". */
  summary: string;
  done: number;
  checked: number;
  problems: number;
  payments: number;
  stories: HistoryStory[];
  reads: HistoryReads | null;
}

export interface HistoryPage {
  timezone: string;
  counts: Record<HistoryShow, number>;
  working_now: HistoryStory[];
  days: HistoryDay[];
  next_before: string | null;
  retention_days: number;
}

/** The first job (startup.md, step 1). `goal` on an org and on each workspace. */
export type GoalKey = 'post' | 'email' | 'invoices' | 'research' | 'forms' | 'other';

/** One line of Getting started. Each is true on the server, not because a wizard was clicked. */
export type SetupItemKey = 'goal' | 'computer' | 'account' | 'ai' | 'approval' | 'phone' | 'invite';

export interface GettingStartedItem {
  key: SetupItemKey;
  title: string;
  done: boolean;
  /** One plain sentence: what to do, or what is true ("It is connected."). */
  hint: string;
  minutes: number;
}

/** `GET /api/orgs/{org}/getting-started`, worked out from real data on every call. */
export interface GettingStarted {
  goal: { key: GoalKey; title: string } | null;
  items: GettingStartedItem[];
  done: number;
  total: number;
  complete: boolean;
}

/**
 * What a routine is made of: a trigger, then steps, then what happens with the result. A routine
 * and a workflow are the same thing, so these hang off `Flow`. The computer holds it and runs it,
 * and the cloud mirrors it (the computer's copy wins). Everything a computer
 * or a person outside wrote stays text here, and `untrusted` says so.
 */
export type RoutineFieldType = 'text' | 'number' | 'date' | 'list' | 'yes_no';

/** One value a trigger or a step produces, for the steps after it. */
export interface RoutineField {
  key: string;
  label: string;
  type: RoutineFieldType;
  /** Written by someone outside (an email, a page, a webhook): it can fill text, never decide. */
  untrusted?: boolean;
}

export type RoutineTriggerKind = 'schedule' | 'webhook' | 'email' | 'page_change' | 'site_change' | 'manual';

export interface RoutineTrigger {
  kind: RoutineTriggerKind;
  /** The words a person reads, when a template or a person chose them. */
  label?: string | null;
  cron?: string | null;
  /** The mail account or the account a page is watched from. */
  lane?: string | null;
  from?: string | null;
  subject?: string | null;
  url?: string | null;
  timezone?: string | null;
  /** A site_change: the endpoint to read, as `<endpoint>@<surface>`, what it is read with, which output
   *  holds the rows, which field says which row it is, and how often to read it. */
  operation?: string | null;
  params?: Record<string, unknown> | null;
  list?: string | null;
  key?: string | null;
  every_minutes?: number | null;
  /** Where the cloud listens. A computer never has a public address. */
  hook?: RoutineHook | null;
}

export interface RoutineHook {
  url: string;
  /** Requests are signed: the secret is shown once, and only its hint afterwards. */
  secret_hint: string | null;
  last_received_at: string | null;
}

export interface RoutineCondition {
  /** A `{{step.field}}` reference. */
  left: string | null;
  op: 'is' | 'is_not' | 'contains' | 'more_than' | 'less_than' | 'is_empty' | 'not_empty';
  right: string;
}

export interface RoutineSend {
  /** A list the owner supplied. Nothing else can be sent to. */
  list: string | null;
}

interface RoutineStepBase {
  id: string;
  /** A short phrase that fits the sentence: "check the stock". */
  label: string;
}

export interface RoutineActionStep extends RoutineStepBase {
  kind: 'action';
  /** A routine, or a connector operation, on one of the workspace's accounts. */
  operation: string | null;
  inputs: Record<string, string>;
  send?: RoutineSend | null;
}

export interface RoutineBranchStep extends RoutineStepBase {
  kind: 'branch';
  when: RoutineCondition;
  then: RoutineStep[];
  otherwise: RoutineStep[];
}

export interface RoutineWaitStep extends RoutineStepBase {
  kind: 'wait';
  amount: number;
  unit: 'minutes' | 'hours' | 'days';
}

export interface RoutineLoopStep extends RoutineStepBase {
  kind: 'loop';
  /** A `{{step.field}}` reference to a list. */
  over: string | null;
  /** What one thing in the list is called: "invoice". */
  item: string;
  body: RoutineStep[];
}

export interface RoutineAskStep extends RoutineStepBase {
  kind: 'ask';
  question: string;
}

export interface RoutineNotifyStep extends RoutineStepBase {
  kind: 'notify';
  message: string;
}

/** Stops chasing whoever answered an earlier email in this run. `after` is that email's step. */
export interface RoutineStopIfRepliedStep extends RoutineStepBase {
  kind: 'stop_if_replied';
  after: string;
}

export type RoutineStep = RoutineActionStep | RoutineBranchStep | RoutineWaitStep | RoutineLoopStep | RoutineAskStep | RoutineNotifyStep | RoutineStopIfRepliedStep;
export type RoutineStepKind = RoutineStep['kind'];

/** The part a person or an agent writes, and what the file for developers holds. */
export interface RoutineDefinition {
  name: string;
  workspace: string | null;
  trigger: RoutineTrigger;
  steps: RoutineStep[];
}

/** One step that asks whatever anyone sets, and why. */
export interface RoutineAlwaysAsks {
  step: string;
  because: 'pay' | 'irreversible' | 'ask_step' | 'install_base';
}

/** What a send step can do today, worked out by the computer. These are hard rules. */
export interface RoutineSendLimit {
  step: string;
  list: string | null;
  list_size: number;
  daily_limit: number;
  sent_today: number;
  suppressed: number;
  /** False for a step that counts against the limit without sending mail, with nothing to unsubscribe from. */
  unsubscribe: boolean;
  counted?: boolean;
}

/** Something a routine made, as it looks in the world: the email, the invoice, the post. */
export interface RoutineOutput {
  kind: 'email' | 'invoice' | 'post' | 'order' | 'form' | 'digest' | 'payment' | 'files';
  /** The subject, the invoice number, the post's first words. */
  title: string;
  /** Who it went to or who it is about. */
  who?: string | null;
  body?: string | null;
  amount?: number | null;
  currency?: string | null;
  image?: string | null;
  /** More than one went out: "3 reminders". */
  more?: string | null;
  /** Where agentos saw it afterwards. Absent: nobody checked. */
  proof?: { site: string; url?: string | null } | null;
}

export type RoutineStepStatus = RunStatus | 'skipped' | 'waiting' | 'stopped';

export interface RoutineStepRun {
  step: string;
  /** The label when it ran, so an old run still reads true after an edit. */
  label: string;
  /** What it came to, in a few words: "3 overdue". */
  short?: string | null;
  kind: RoutineStepKind | 'trigger';
  status: RoutineStepStatus;
  started_at: string | null;
  finished_at: string | null;
  input: Record<string, unknown> | null;
  output: Record<string, unknown> | null;
  /** Keys in input and output that came from outside. */
  untrusted?: string[];
  /** Why it failed or was skipped, in plain words. */
  note?: string | null;
  attempts: number;
  /** A person decides this in Needs you. */
  approval_id?: string | null;
  /** One entry per item in a loop. */
  items?: { label: string; status: RoutineStepStatus }[];
}

export interface RoutineRun {
  id: string;
  flow: string;
  /** A workflow's revision. A recorded routine's content hash. */
  version: number | string;
  status: RunStatus;
  /** What set it off, in plain words: "Order #1042 arrived". */
  cause: string;
  /** What came of it, in plain words: "3 reminders sent". */
  result: string | null;
  by: 'trigger' | 'person' | 'retry' | 'agent';
  started_at: string;
  finished_at: string | null;
  output: RoutineOutput | null;
  steps: RoutineStepRun[];
  cost: Cost;
}

/** What a run did across the routines, for the line through now on the list. */
export interface RoutineDayMark {
  flow: string;
  at: string;
  site: string;
  /** Null for what is still to come. */
  status: RunStatus | null;
}

export interface RoutineDay {
  marks: RoutineDayMark[];
  ran_today: number;
  checked_today: number;
}

export interface RoutineOperation {
  id: string;
  name: string;
  lane: string;
  workspace: string;
  effect: Effect;
  params: Record<string, FlowParam>;
  produces: RoutineField[];
  /** Sends to people, so a list, a daily limit, suppression and an unsubscribe all apply. */
  sends: boolean;
  /** What a template looks for: `mail.send`, `shop.stock`. */
  tags: string[];
}

export interface RoutineList {
  id: string;
  name: string;
  workspace: string;
  size: number;
  suppressed: number;
}

export interface RoutineTemplate {
  id: string;
  name: string;
  /** The job, as one sentence. */
  sentence: string;
  /** Who it is for, and what it does, in a few words. */
  note: string;
  icon: 'newsletter' | 'outreach' | 'invoice' | 'order' | 'webhook' | 'post' | 'new_row';
  developer?: boolean;
  trigger: RoutineTrigger;
  /** A step's `operation` may name a tag here: the panel fills in what the workspace has. */
  steps: RoutineStep[];
  /** What must be connected on this computer for it to run. A template that sends or reads mail needs a mailbox. */
  needs?: 'mailbox'[];
}

export interface RoutineCatalog {
  /** How a routine can start on this computer. A cloud leaves it out and offers every way. */
  triggers?: RoutineTriggerKind[];
  operations: RoutineOperation[];
  lists: RoutineList[];
  templates: RoutineTemplate[];
  /** What each trigger hands the steps after it. */
  trigger_fields: Record<RoutineTriggerKind, RoutineField[]>;
}

/*
 * Site APIs. Concept stage: only the mock answers these, and they
 * will move when the node's report is pinned. `pay` and `irreversible` are refused at save, so they
 * are not in this type at all.
 */
export type SiteApiEffect = Extract<Effect, 'read' | 'draft' | 'commit'>;
export type SiteApiStage = 'proposed' | 'active' | 'paused';
/** What a person needs to know right now. Each has its own words and its one next step. */
export type SiteApiHealth = 'good' | 'changed' | 'sign_in' | 'site_check' | 'resting' | 'paused' | 'empty' | 'unread';
export type SiteFieldKind = 'text' | 'money' | 'date' | 'count' | 'status' | 'link';

export interface SiteField {
  key: string;
  label: string;
  kind: SiteFieldKind;
}

/** A column on the page. `field` is null when it was left out; `values` then holds what it said. */
export interface SitePageColumn {
  label: string;
  field: string | null;
  values?: string[];
}

/** Another thing the computer noticed on the page, for Teach it. */
export interface SitePageRegion {
  id: string;
  /** "a table of 42 orders" */
  words: string;
  sure: 'sure' | 'likely' | 'unsure';
  kind: 'main' | 'side' | 'nav';
}

/**
 * The page as the computer saw it: its structure and its text, never its HTML. Text is shown as
 * text, and nothing here carries a cookie or a session.
 */
export interface SitePage {
  url: string;
  wordmark: string;
  nav: string[];
  heading: string;
  count_words: string;
  layout: 'table' | 'cards' | 'thread';
  columns: SitePageColumn[];
  /** The short notes down the side of the page, when it has them. */
  side: string[];
  seen_at: string;
}

export type SiteRow = Record<string, string>;

export interface SiteInputField {
  key: string;
  label: string;
  type: 'choice' | 'text' | 'number' | 'date';
  options?: { value: string; label: string }[];
  default?: string;
  required?: boolean;
  hint?: string;
}

export interface SiteEndpoint {
  key: string;
  title: string;
  description: string;
  effect: SiteApiEffect;
  input: SiteInputField[];
  fields: SiteField[];
  /** The last good result. */
  rows: SiteRow[];
  /** How many there were on the page, which can be more than `rows` holds. Null when the node does not know. */
  total: number | null;
  /** Row indexes that are new since the read before. */
  new_rows?: number[];
  cache_s: number;
  pace_per_hour: number;
  expect_min: number | null;
  calls_7d: number;
  /** Null when no call was made in the week. */
  cached_pct: number | null;
  /** The median call time over the hours with calls, or null when there were none. */
  p50_ms: number | null;
  /** The page as the computer last saw it. Null until a read has kept one. */
  page: SitePage | null;
}

export type SiteSample =
  | { kind: 'rows'; columns: string[]; rows: string[][] }
  | { kind: 'aging'; total: string; note: string; items: { who: string; amount: string; days: number }[] }
  | { kind: 'levels'; items: { label: string; left: number; reorder: number; unit: string }[] }
  | { kind: 'journey'; parcel: string; stops: string[]; at: number; note: string }
  | { kind: 'quotes'; items: { who: string; text: string; when: string }[] };

/** Why the API is not answering, in facts. The words are the panel's (`features/site-apis/words.ts`). */
export interface SiteProblem {
  since: string;
  /** The field that moved or went missing. */
  field?: string;
  /** What the page says now, where the field was. */
  now_label?: string;
  now_values?: string[];
  /** When the site's rest ends. */
  resumes_at?: string;
  expected?: number;
  found?: number;
  /** Who paused it, as a name. */
  by?: string;
}

export interface SiteProposal {
  /** Who proposed it, as a name. */
  by: string;
  at: string;
  /** How many rows the agent read to try it, when the node kept that. */
  tried_rows?: number;
  /** What the node recorded it would do, in the node's words. */
  steps: string[];
}

export interface SiteApi {
  key: string;
  /** The job, as a person would say it. */
  title: string;
  site: { name: string; domain: string };
  /** One plain sentence of what it gives you. */
  words: string;
  workspace: string;
  /** The one account it runs on, and only that one. */
  lane: string;
  account: string;
  stage: SiteApiStage;
  health: SiteApiHealth;
  problem: SiteProblem | null;
  proposal: SiteProposal | null;
  sample: SiteSample;
  /** Calls by six hours over the week: cached, fresh, errors. */
  spark: { cached: number; fresh: number; errors: number }[];
  calls_7d: number;
  /** Null when no call was made in the week. */
  cached_pct: number | null;
  read_at: string | null;
  version: string;
}

export interface SitePace {
  per_hour: number;
  /** Reads of this Site API in the last hour. */
  used: number;
  /** What else used this account's pace in the last hour. */
  others: { label: string; used: number }[];
  /** Reads in each of the last twelve hours, oldest first. */
  hours: number[];
}

export interface SiteApiDetail extends SiteApi {
  endpoints: SiteEndpoint[];
  pace: SitePace;
  computer: string;
  /** How often it is read for a person who asked to keep it updated. */
  keep_updated: 'off' | 'hourly' | 'morning';
}

export interface SiteUsageHour {
  at: string;
  /** Per endpoint key. */
  calls: Record<string, { cached: number; fresh: number; errors: number }>;
}

export interface SiteKey {
  id: string;
  label: string;
  prefix: string;
  apis: string[];
  /** Null reaches every endpoint of those APIs. */
  endpoints: string[] | null;
  ceiling: SiteApiEffect;
  rate_per_min: number;
  expires_at: string | null;
  last_used_at: string | null;
  created_at: string;
}

export interface SiteKeyInput {
  label: string;
  apis: string[];
  endpoints: string[] | null;
  ceiling: SiteApiEffect;
  rate_per_min: number;
  expires_at: string | null;
}

/** Shown once. Only the hash is kept after this. */
export interface SiteKeyCreated extends SiteKey {
  secret: string;
}

/** What a person can do to a Site API. Only a person: an agent can propose, never these. */
export type SiteAction =
  | { kind: 'read' }
  | { kind: 'pause' }
  | { kind: 'resume' }
  | { kind: 'activate' }
  | { kind: 'decline' }
  | { kind: 'repair' }
  | { kind: 'accept-empty' }
  | { kind: 'keep-updated'; every: SiteApiDetail['keep_updated'] };

export interface SiteTryResult {
  status: number;
  ms: number;
  headers: Record<string, string>;
  body: unknown;
}

export interface SiteDiscovery {
  lane: string;
  /** The Site API this page already belongs to on this account, or empty when none does. */
  api: string;
  site: { name: string; domain: string };
  page: SitePage;
  regions: SitePageRegion[];
  rows: SiteRow[];
  total: number;
  /** Pages this account visits most, to start from. */
  suggestions: { url: string; words: string }[];
}

/** The setting, as the contract writes it. `when` on money stays `ask`: it is a floor. */
export interface ConfirmationSetting {
  commit: { when: 'auto' | 'ask'; level: ConfirmLevel };
  pay: { when: 'ask'; level: ConfirmLevel };
  irreversible: { when: 'ask'; level: ConfirmLevel };
  after_untrusted: { level: ConfirmLevel };
  /** Off means a yes in chat counts for nothing, wherever it comes from. */
  chat: boolean;
}

/** What a business asks for beyond the org. Levels only rise down the chain. */
export type ConfirmationOverlay = Partial<Pick<ConfirmationSetting, 'commit' | 'pay' | 'irreversible' | 'after_untrusted'>>;

export type ConfirmRow = 'commit' | 'marked' | 'after_untrusted' | 'pay' | 'irreversible';

/** One line of a day that ran on its own: a look, or something that went out. */
export interface ConfirmDayTick {
  /** Minutes after midnight. */
  minute: number;
  business: string;
  kind: 'look' | 'commit';
}

/** One thing that stopped at a person. */
export interface ConfirmDayStop {
  id: string;
  minute: number;
  row: Exclude<ConfirmRow, 'commit'>;
  business: string;
  site: string;
  title: string;
  who: string;
  said: 'yes' | 'no';
  method: ConfirmMethodId;
  /** The chat app, or the device, by name. */
  where: string | null;
}

export interface ConfirmDay {
  /** "Yesterday". */
  label: string;
  date: string;
  ticks: ConfirmDayTick[];
  stops: ConfirmDayStop[];
}

export interface ConfirmationView {
  setting: ConfirmationSetting;
  /** The system default, for the mark on each row and the way back to it. */
  system: ConfirmationSetting;
  overlays: Record<string, ConfirmationOverlay>;
  day: ConfirmDay;
  /** The chat apps that have answered here, by name. */
  clients: { name: string; agent: string; answers: number; last_at: string }[];
  /** A review link as a chat would show it, for the concept page. */
  sample_review?: string | null;
}

/** One place a person can say yes, as the registry describes it. */
export interface ConfirmMethodView {
  id: ConfirmMethodId;
  level: ConfirmLevel;
  status: 'ready' | 'planned';
  /** Set up for this person, so it would reach them today. */
  yours: boolean;
  /** What it is on this person's side: "Demo's MacBook Pro", "Claude Code, ChatGPT". */
  detail: string | null;
  last_used_at: string | null;
}

/*
 * Email, at concept stage: answered by the mock only. A password has
 * no field anywhere in these types, on purpose: the hosted panel never holds one.
 */
/** `unchecked` is a mailbox the node has not read yet: nothing is said about it until it has. */
export type MailboxHealth = 'good' | 'unchecked' | 'cannot_send' | 'cannot_read' | 'needs_password';

export interface MailProblem {
  since: string;
  title: string;
  body: string;
  /** The one next step. */
  next: string;
}

export interface MailArrival {
  at: string;
  kind: 'reply' | 'unsubscribe' | 'mail';
}

export interface MailboxDay {
  at: string;
  sent: number;
  replies: number;
  unsubscribes: number;
}

/**
 * A mailbox as the node knows it. Null is an honest "not known here yet": the node does not keep
 * arrivals, a week of replies, or a plan for today, so those come back null until it does.
 */
export interface Mailbox {
  id: string;
  address: string;
  /** A domain, which is also what the mark is taken from. */
  site: string;
  provider: string | null;
  workspace: string;
  computer: string;
  health: MailboxHealth;
  problem: MailProblem | null;
  checked_at: string | null;
  new_count: number | null;
  arrivals: MailArrival[] | null;
  sent_today: number;
  /** What routines will still send today. */
  planned_today: number | null;
  daily_limit: number;
  week: MailboxDay[] | null;
  routines: { id: string; name: string }[] | null;
  incoming: string;
  outgoing: string;
}

export type MailDiscoveryResult = 'found' | 'needs_a_look' | 'not_found' | 'later';

export interface MailComputer {
  id: string;
  word: string;
  /** A server has no screen to open a form on, so its door is the terminal. */
  has_screen: boolean;
}

export interface MailDiscovery {
  address: string;
  domain: string;
  result: MailDiscoveryResult;
  provider: string | null;
  site: string | null;
  /** Gmail and Workspace ask for an app password. Others take the normal one. */
  password: 'app_password' | 'normal' | null;
  incoming: string | null;
  outgoing: string | null;
  /** Where the names came from, in plain words. */
  source: string | null;
  /** The part of a low-trust host that is not the address's own name. */
  odd_part?: string | null;
  computers: MailComputer[];
}

export type MailBlock = { kind: 'text'; text: string } | { kind: 'image'; host: string } | { kind: 'link'; text: string; url: string };

export interface MailDid {
  at: string;
  words: string;
  /** A routine that acted, with its page when it has one. */
  routine?: { id: string; name: string } | null;
  /** Something now waits for a person. */
  waits?: boolean;
}

/** A message in a list has its clip and no body: one message, read in full, has its body. */
export interface MailMessage {
  id: string;
  mailbox: string;
  kind: 'reply' | 'unsubscribe' | 'automatic' | 'mail';
  from_name: string;
  from_address: string;
  subject: string;
  clip: string;
  at: string;
  unread: boolean;
  body: MailBlock[];
  /** Named, not opened. The node gives the type, not the size. */
  attachments: { name: string; type?: string | null; size?: number }[];
  /** What a routine did with it. The node does not keep that per message yet: null. */
  did: MailDid[] | null;
  list?: string | null;
}

/** A link to the node's own form for one mailbox. It opens on the computer, and holds no field. */
export interface MailFormLink {
  url: string;
  expires_at: string;
  computer: string;
  /** The token the url carries, for asking whether the form is still open and for cancelling it. Nothing else reads it. */
  token: string;
}

/** A server a person typed. Its port and security may be left out, to keep the ones found with it. */
export interface MailServerFields {
  host: string;
  port?: number;
  security?: 'tls' | 'starttls';
}

/** The link to the node's form. `imap` and `smtp` are only the names a person typed, to be checked there. */
export interface MailFormLinkBody {
  address: string;
  id: string;
  imap?: MailServerFields;
  smtp?: MailServerFields;
}

/** Whether a link to the node's form is still open. An ended link and one that never was look the same. */
export interface MailFormState {
  state: 'open' | 'ended';
  expires_at?: string | null;
}

export type LeadLook = 'personal_address' | 'other_name' | 'shared_inbox';

export interface Lead {
  id: string;
  name: string;
  /** The node does not keep a role or a country for a lead: null until it does. */
  role: string | null;
  company: string;
  address: string;
  country: string | null;
  source: 'linkedin' | 'directory';
  source_word: string;
  source_url: string | null;
  found_at: string;
  /** Why the agent picked them, in its words. Shown as text. */
  why: string;
  look: LeadLook | null;
}

export interface SuppressedLead {
  id: string;
  name: string;
  company: string;
  since: string;
  why: 'unsubscribed' | 'asked' | 'bounced';
}

export interface LeadBatch {
  id: string;
  list: string;
  list_name: string;
  workspace: string;
  /** Null: a list does not say which mailbox sends it on the node. */
  mailbox: string | null;
  by: string;
  at: string;
  found: number;
  sources: { key: Lead['source']; word: string; note: string }[];
  leads: Lead[];
  /** Who was left out for the do-not-write list. The node counts them and does not name them here: null. */
  suppressed: SuppressedLead[] | null;
  status: 'waiting' | 'taken' | 'dropped';
}

export interface ListPerson {
  id: string;
  name: string;
  company: string;
  address: string;
  source_word: string;
  /** Where they are in a send. The node does not keep that per person on a list yet: null. */
  state: 'planned' | 'sent' | 'replied' | 'followed' | 'unsubscribed' | null;
}

export interface ListDay {
  at: string;
  first: number;
  follow: number;
  replies: number;
  unsubscribes: number;
}

/**
 * A list as the node keeps it. Its size is known; the people are read in person. What a list does
 * on a mailbox (its pace, its days, its letter) is not kept by the node yet, so those are null.
 */
export interface MailList {
  id: string;
  name: string;
  workspace: string;
  size: number;
  mailbox: string | null;
  address: string | null;
  mailbox_limit: number | null;
  /** What the mailbox sends on a normal day for other reasons. */
  other_per_day: number | null;
  state: 'ready' | 'sending' | 'paused' | null;
  pace: number | null;
  /** Null on a list in the lists page: the people come with one list, read in person. */
  people: ListPerson[] | null;
  days: ListDay[] | null;
  /** Who is on the do-not-write list, by name. The node counts them and does not name them here: null. */
  suppressed: SuppressedLead[] | null;
  /** The owner has not made a list for this business before, so the note is due. */
  first_list: boolean;
  countries: string[] | null;
  routine: { id: string; name: string } | null;
  letter: { subject: string; body: string; sender: string; postal: string; source_line: string } | null;
  batch_waiting: string | null;
}

export interface ChasePerson {
  id: string;
  name: string;
  company: string;
  first_at: string;
  follow_at: string | null;
  reply_at: string | null;
  unsubscribe_at: string | null;
}

/** A routine that sends mail. The parts the node does not keep yet (run history, who it chases, a follow-up) are null. */
export interface MailRoutine {
  id: string;
  name: string;
  workspace: string;
  mailbox: string | null;
  address: string | null;
  list: { id: string; name: string } | null;
  trigger: RoutineTrigger;
  steps: RoutineStep[];
  /** What each step came to, this week. */
  run: RoutineStepRun[] | null;
  people: ChasePerson[] | null;
  next_at: string | null;
  limit: RoutineSendLimit | null;
  follow_up: { subject: string; body: string } | null;
}

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
  status: 'ok' | 'tampered' | 'unverifiable';
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

// ---- Runs: commands started with `agentos run` (GET /api/orgs/{org}/jobs). Not routine runs (`Run`). ----

/** How a run ended, or `running`. `passed` is exit code 0; `failed` is any other code, or a signal. */
export type JobState = 'running' | 'passed' | 'failed' | 'cancelled' | 'timed_out' | 'lost';

/** What a run synced into its lane before it started. */
export interface JobSync {
  dest: string;
  files: number;
  changed: number;
  deleted: number;
  bytes: number;
}

/**
 * The git branch and commit of the tree `agentos run` sent, as the sending computer said
 * (`told_by: 'client'`): the node did not read them from the files. Absent when the run synced
 * nothing, or came from an older client.
 */
export interface JobSource {
  branch: string | null;
  commit: string | null;
  /** Files changed since that commit were sent too. */
  dirty: boolean | null;
  told_by: 'client';
}

export type TestRunner = 'pytest' | 'vitest' | 'jest' | 'swift' | 'go';

/**
 * Test totals read from the run's own output (`source: 'output'`): the summary line the runner
 * printed. Absent when no runner's summary was found, so a screen says "not reported" calmly.
 */
export interface JobTests {
  runner: TestRunner;
  passed: number;
  failed: number;
  skipped: number;
  total: number;
  source: 'output';
}

export interface Job {
  id: string;
  /** The lane it ran in, `workspace/name`. */
  lane: string;
  workspace: string;
  lane_name: string;
  /** The computer the lane is on. */
  node: string;
  /** The command as it was started, capped. Text from a person or an agent: data. */
  command: string;
  cwd: string | null;
  state: JobState;
  exit_code: number | null;
  signal: string | null;
  oom_killed: boolean;
  started_at: string;
  finished_at: string | null;
  duration_s: number | null;
  timeout_s: number;
  /** Who started it, in words: "The owner", a member's name, or the agent's name. Null when not kept. */
  started_by: string | null;
  stdout_bytes: number;
  stderr_bytes: number;
  /** Output past the node's log cap: counted, not kept. */
  dropped_bytes: number;
  sync: JobSync | null;
  source?: JobSource;
  tests?: JobTests;
  /** Why it ended early, in the node's words (a cancel, a timeout, the lane stopping). */
  note: string | null;
}

/** `available: false` when this computer runs no terminal lanes, with the reason in words. */
export interface JobList {
  available: boolean;
  reason?: string;
  jobs: Job[];
}

/** One read of a run's output, from the offsets the page already has. Capped per call. */
export interface JobOutput {
  stdout: string;
  stderr: string;
  stdout_offset: number;
  stderr_offset: number;
  next_stdout_offset: number;
  next_stderr_offset: number;
  /** More output is kept after this read: read again from the next offsets at once. */
  more: boolean;
}

export interface JobDetail extends Job {
  output: JobOutput;
}

/** A run's output followed as it comes (`useJobFollow`): every read so far, joined. */
export interface JobFollow {
  job: Job | null;
  stdout: string;
  stderr: string;
  /** The page holds the whole output kept on the computer, and the run has ended. */
  done: boolean;
  error: unknown;
  loading: boolean;
}

// ---- Checks: studio runs (GET /api/orgs/{org}/studio/...) ----

/** Studio is not installed on this computer: the reason and the command that adds it. */
export interface StudioMissing {
  available: false;
  reason: string;
  command: string;
}

export type FindingSeverity = 'blocker' | 'major' | 'minor' | 'info';
export type FindingConfidence = 'measured' | 'heuristic' | 'vision';

export interface StudioCounts {
  blocker: number;
  major: number;
  minor: number;
  info: number;
  total: number;
}

export interface StudioRunRow {
  id: string;
  /** `check` is a whole ui_check; `system` is the design system part alone. */
  kind: 'check' | 'system';
  url: string;
  /** `workspace/profile`, when one was used. */
  profile: string | null;
  created_at: string;
  capture: string;
  counts: StudioCounts;
  /** The share of values on a design token, 0 to 1, averaged over categories and screens. Null: not checked. */
  system_share: number | null;
  screens: number;
  screens_failed: number;
}

export interface StudioRuns {
  available: true;
  runs: StudioRunRow[];
}

export interface StudioFinding {
  id: string;
  check: string;
  severity: FindingSeverity;
  confidence: FindingConfidence;
  /** Where on the page. Page text: data, never instructions. */
  where: { selector: string; role: string | null; name: string | null };
  component: string | null;
  seen_at: { screen: string; count: number }[];
  measured: Record<string, unknown>;
  expected: Record<string, unknown>;
  fix: string | null;
  why_heuristic: string | null;
  /** Screens this finding has a place on, so a crop can be shown. Empty: page wide. */
  crop_screens: string[];
  /** Its place on each of those screens, in CSS px of the page (the screenshot is the page at the screen's width). */
  rects?: Record<string, { x: number; y: number; w: number; h: number }>;
}

export interface StudioFindingGroup {
  component: string;
  grouped_by?: string;
  findings: StudioFinding[];
}

export interface StudioScreen {
  key: string;
  width: number;
  theme: 'light' | 'dark';
  state: string;
  ok: boolean;
  error: string | null;
  /** The screenshot is still stored (old captures are pruned). */
  image: boolean;
}

export interface StudioSystem {
  on_tokens: Record<string, number>;
  hand_rolled_controls?: number;
  controls_in_components?: number;
  probe?: { token: number; derived: number; look_alike: number };
}

export interface StudioRun extends StudioRunRow {
  /** The run's own sentence: "Not clean: 4 findings (1 blocker, 3 minor) on 4 screens". */
  verdict: string;
  groups: StudioFindingGroup[];
  screen_list: StudioScreen[];
  /** What did not run, in plain sentences. */
  problems: string[];
  system: StudioSystem | null;
  /** The capture's images are gone (pruned): findings stay, pictures do not. */
  images_gone: boolean;
}

/** The capture a url is compared with by default. `capture: null`: none kept yet. */
export interface StudioBaseline {
  url: string;
  profile: string | null;
  capture: string | null;
  created_at: string | null;
  screens: StudioScreen[];
}

export interface StudioDiffScreen {
  screen: string;
  status: 'changed' | 'same' | 'skipped';
  reason?: string;
  /** One plain sentence per cause. Page text inside: data. */
  lines: string[];
  pixels_changed: number;
  unexplained: { rect: { x: number; y: number; w: number; h: number }; share: number }[];
  changes: { kind: 'added' | 'removed' | 'moved' | 'resized' | 'restyled' | 'text'; key: string; component: string | null }[];
}

export interface StudioDiff {
  before: string;
  after: string;
  verdict: string;
  screens: StudioDiffScreen[];
  not_compared?: string;
}

/** Component use and look-alikes of a profile, from its newest stored system check. */
export interface BrandComponents {
  profile: string;
  /** The run they come from; null when no system check of this profile is stored. */
  run: string | null;
  url: string | null;
  checked_at: string | null;
  /** Elements per known component, counted on the run's screens. */
  components: { name: string; uses: number; screens: number; findings: number }[];
  /** Controls drawn outside every known component. */
  hand_rolled: number;
  /** Values that only look like a token (the provenance probe). Null: not measured. */
  look_alikes: { token: number; derived: number; look_alike: number } | null;
  look_alike_findings: StudioFinding[];
  on_tokens: Record<string, number>;
}

// ---- Brand: product and business profiles (GET /api/orgs/{org}/brand/...) ----

export type BrandKind = 'product' | 'business';

export interface BrandProfileRow {
  /** `workspace/profile`. */
  ref: string;
  workspace: string;
  profile: string;
  kind: BrandKind;
  display_name: string;
  headline: string | null;
  version: number;
  state: 'draft' | 'approved' | 'rejected' | 'superseded';
  /** The version of a draft an agent proposed, waiting for the owner. */
  draft_waiting: number | null;
}

export interface BrandProfiles {
  available: boolean;
  profiles: BrandProfileRow[];
}

export interface BrandToken {
  /** The token's path: `color.brand.primary`. */
  name: string;
  type: string | null;
  /** The value as text: a hex colour, `16px`, or an alias such as `{color.ink}`. */
  value: string;
}

export interface BrandSection {
  tokens: BrandToken[];
  fonts: { family: string; role: string | null; weights: number[] }[];
  logo: string | null;
  components: { name: string; attr: string | null; class_pattern: string | null }[];
  theme: { how: string; name: string | null; light: string | null; dark: string | null } | null;
  widths: number[];
  engine: string | null;
}

export interface BrandGuideline {
  id: string;
  text: string;
  check: string | null;
  threshold: number | null;
  advisory: boolean;
}

export interface BrandReference {
  name: string;
  url: string;
  note: string | null;
}

export interface BrandDraft {
  version: number;
  created_at: string;
  /** Who proposed it, as the node keeps it (an agent's name). */
  created_by: string | null;
  /** Why the agent proposes it, in its own words: data. */
  reason: string | null;
  /** What approving it changes, one plain line each. */
  diff: string[];
  /** The draft's own tokens, flattened as the profile's are, so each change is shown beside today's value. */
  tokens?: BrandToken[];
  /** The draft's guidelines. */
  guidelines?: BrandGuideline[];
}

export interface BrandProfile extends BrandProfileRow {
  website: string | null;
  created_at: string;
  decided_at: string | null;
  brand: BrandSection | null;
  guidelines: BrandGuideline[];
  references: BrandReference[];
  environments: { name: string; url: string }[];
  draft: BrandDraft | null;
}

/**
 * A brand proposal for Needs you, beside approvals: decided with approve or reject on the
 * profile (owner in person), never through the approval routes.
 */
export interface BrandProposal {
  /** `brand:workspace/profile@version`: unique beside approval ids. */
  id: string;
  kind: 'brand';
  ref: string;
  workspace: string;
  display_name: string;
  version: number;
  created_at: string;
  created_by: string | null;
  reason: string | null;
  diff: string[];
}

export interface BrandDecision {
  ref: string;
  version: number;
  state: 'approved' | 'rejected';
}

// ---- Disposable VMs (GET /api/orgs/{org}/vms), read only ----

export interface Vm {
  id: string;
  lane: string;
  state: 'creating' | 'booting' | 'ready' | 'deleting' | 'deleted' | 'failed' | string;
  profile: string;
  server_type: string;
  location: string;
  created_at: string;
  ready_at: string | null;
  expires_at: string;
  minutes_left: number | null;
  price_hourly_eur: number | null;
  cost_eur_so_far: number | null;
  /** What the agent said it is for: data. */
  purpose: string;
  started_by: string | null;
}

export interface VmList {
  available: boolean;
  reason?: string;
  vms: Vm[];
}

// Workers, at concept stage: answered by the mock only. Nothing here can carry a key.

export type WorkerKind = 'browser' | 'android' | 'desktop';
export type WorkerJob = 'decide' | 'write' | 'rescue';
/** Who thought at this step. `you` is a stop at a person, and `yes` a step waiting for a person's yes. */
export type WorkerStepKind = 'decide' | 'write' | 'rescue' | 'you' | 'yes';

export interface WorkerStep {
  kind: WorkerStepKind;
  /** What happened, in plain words. */
  what: string;
  /** For Technical details only. */
  model: string | null;
  ms: number;
  usd: number;
}

/** Why a worker stopped at a person. */
export type WorkerStop = 'unsure' | 'robot_check' | 'new_device' | 'limit';

/** One thing a worker did or is doing: what it was told, and each step it took. */
export interface WorkerThing {
  id: string;
  worker: WorkerKind;
  lane: string;
  node: string;
  said_by: Principal;
  /** What the agent told it, as the agent said it. */
  told: string;
  state: 'working' | 'done' | 'asked' | 'stopped';
  steps: WorkerStep[];
  stop: WorkerStop | null;
  /** The most steps it may take on one thing. The picture is this long. */
  limit: number;
  started_at: string;
  ended_at: string | null;
  run_id: string | null;
}

export interface WorkerPlace {
  node: string;
  state: NodeState;
  /** On, off, or what stands in the way. Only `on` and `off` get a switch. */
  worker: 'on' | 'off' | 'needs_service';
  /** The AI service this computer lacks, when that is what stands in the way. */
  missing: string | null;
  things_today: number;
  /** The accounts it works on here, so not the ones in `accounts_off`. */
  accounts: number;
  /** Lanes a person took it off. An agent works there directly, and a person can take over. */
  accounts_off: string[];
  /** Of `accounts`, how many ask first. It is the account's own Ask me first, never a second switch. */
  accounts_asking: number;
  /** A post that follows a page asks whatever an account says, and the worker always reads a page first. */
  after_a_page_asks: boolean;
  /** An agent asked for it. Only a person can turn it on. */
  suggested_by: Principal | null;
  /** It is on, and starts the next time agentos starts on this computer. */
  restart_needed?: boolean;
}

/** The worker on one account. Null when there is nothing to say: no worker runs on its computer. */
export interface WorkerAccount {
  lane: string;
  on: boolean;
  /** Its computer answers, so the switch would do something. */
  online: boolean;
  /** As on `WorkerPlace`: while true, "Runs on its own" does not send the worker's posts alone. */
  after_a_page_asks: boolean;
}

export interface WorkerAssignment {
  job: WorkerJob;
  /** An AI service's name, or null when nothing can do this job yet. */
  brain: string | null;
  /** The model under it, for Technical details. */
  model: string | null;
}

export interface WorkerLimits {
  steps_per_day: number;
  help_per_day: number;
  steps_per_thing: number;
  usd_per_day: number;
}

export interface WorkerToday {
  things: number;
  steps: number;
  helped: number;
  asked: number;
  /** Of those it asked, how many still wait for an answer. */
  waiting: number;
  longest: number;
  usd: number;
}

/** A worker that cannot run yet, said calmly. `places` says why, for each computer. */
export interface WorkerLater {
  kind: WorkerKind;
  sentence: string;
  places: { node: string; why: string; fact: string }[];
}

export interface WorkerOverview {
  plugin: string;
  version: string;
  /** The computers it could run on. Empty when the org has none. */
  places: WorkerPlace[];
  jobs: WorkerAssignment[];
  limits: WorkerLimits;
  today: WorkerToday;
  /** What it is doing now, else the last thing it did. */
  latest: WorkerThing | null;
  later: WorkerLater[];
}

/** What a computer runs and cannot run, for its own page. */
export interface WorkerOnComputer {
  node: string;
  runs: { kind: WorkerKind; on: boolean; latest: WorkerThing | null; things_today: number } | null;
  cannot: { kind: WorkerKind; why: string }[];
  keeps: string[];
}

/** One trip of a key from a person to a computer. The panel only ever hears where it got to. */
export interface BrainHandover {
  id: string;
  service: string;
  computer: string;
  stage: 'waiting' | 'checking' | 'done' | 'failed';
  /** The computer's own form for this trip, on a computer that serves one. */
  form_url?: string | null;
}

// Memory: what agents remember about a workspace's work. The owner in person reads, confirms, reviews and
// forgets it (src/agentos/api/routes_memory.py, shaped by src/agentos/api/web/memory.py). Its text was written
// by agents after reading pages, so it is always shown as plain text and never as markup or a link.

/** Who wrote it, and how far it is trusted: the owner, agentos itself, an agent, or an agent that read a page. */
export type MemoryTrust = 'owner' | 'node' | 'agent' | 'untrusted';
export type MemoryType = 'fact' | 'document' | 'decision' | 'preference' | 'contact' | 'run_summary';
export type MemoryState = 'live' | 'expired' | 'forgotten';

/** One workspace that has memory, against the caps, with what waits for a look. */
export interface MemoryWorkspace {
  workspace: string;
  live_entries: number;
  live_entries_cap: number;
  content_bytes: number;
  content_bytes_cap: number;
  /** Agent and untrusted entries nobody has looked at yet. */
  waiting: number;
  /** The record's hash chain checks out. False means it was changed outside agentos: say so, plainly. */
  chain_ok: boolean;
  /** Entries erased for good. Their titles and text are gone; the record of the erasing stays. */
  forgotten: number;
}

export interface MemoryWorkspaces {
  /** False on a computer without the memory service: the screen says so, and offers nothing to change. */
  available: boolean;
  workspaces: MemoryWorkspace[];
}

/** An entry in a list or a search, with its title and a snippet, never its whole text. */
export interface MemoryEntry {
  id: string;
  workspace: string;
  type: MemoryType;
  /** Null once it is forgotten. */
  title: string | null;
  trust: MemoryTrust;
  state: MemoryState;
  suspicious: boolean;
  pinned: boolean;
  labels: string[];
  /** The account it was written on, when a lane wrote it. */
  lane: string | null;
  writer: string;
  at: string;
  version: number;
  snippet?: string | null;
  /** Not in a search hit: whether somebody has looked at it yet. */
  reviewed?: boolean | null;
  expires_at?: string | null;
  /** Why the node will not show it here (for example, it is private to another grant). */
  withheld?: string;
}

export interface MemoryList {
  available: boolean;
  /** Null when the list spans every workspace. */
  workspace: string | null;
  /** Newest first, across the workspaces asked for. */
  entries: MemoryEntry[];
}

export interface MemoryHistoryItem {
  version: number;
  at: string;
  trust: MemoryTrust;
  title: string | null;
  forgotten: boolean;
}

/** What an agent read before it wrote the entry, as the node recorded it. */
export interface MemoryEvidence {
  at: string | null;
  tool: string | null;
  summary: string | null;
}

/** One entry in full. `body` and `title` are text, and null once the entry is forgotten. */
export interface MemoryDetail {
  id: string;
  workspace: string;
  type: MemoryType;
  state: MemoryState;
  version: number;
  current: number;
  trust: MemoryTrust;
  labels: string[];
  pinned: boolean;
  suspicious: boolean;
  flagged: string[];
  writer: string;
  lane: string | null;
  created_at: string;
  updated_at: string;
  reviewed_at: string | null;
  expires_at: string | null;
  title: string | null;
  body: string | null;
  attrs: Record<string, unknown> | null;
  /** The node's own note for an untrusted entry. The app says it in its own words instead. */
  warning?: string | null;
  withheld?: string;
  forgotten: { at: string; by: string; reason: string } | null;
  confirmed_by: string | null;
  evidence: MemoryEvidence[];
  history: MemoryHistoryItem[];
}

export interface MemoryConfirmed {
  id: string;
  workspace: string;
  version: number;
  trust: MemoryTrust;
}

export interface MemoryReviewed {
  id: string;
  reviewed_at: string;
}

export interface MemoryForgotten {
  id: string;
  state: 'forgotten';
  already: boolean;
  at: string | null;
}

// The day plan: one view across accounts and programs, and a draft of the day (src/agentos/api/routes_plan.py).
// A draft queues nothing and sends nothing.

export type PlanSection = 'blocked' | 'queue' | 'due' | 'programs' | 'upkeep';

export interface PlanItem {
  section: PlanSection;
  /** The account it is about, or null when it is about the whole computer or a question. */
  lane: string | null;
  /** The node's own sentence. It is written for the command line, so a screen says a blocked item from `readiness`. */
  text: string;
  /** A blocked item's readiness key (`planReadiness`), set on blocked items only. */
  readiness?: string | null;
}

export interface DayPlan {
  day: string;
  status: 'draft';
  /** True while engagement sessions are off: the plan says what is planned, and acts on nothing. */
  dry_run: boolean;
  items: PlanItem[];
  content: { surface: string; kind: string; text: string; status: 'draft' }[];
  unavailable: string[];
}

export interface PlanProgram {
  id: string;
  surface: string;
  state: 'draft' | 'approved';
  phase: string;
  planned: boolean;
  quiet: boolean;
  due: Record<string, number>;
  done: Record<string, number>;
}

export interface PlanAccount {
  lane: string;
  workspaces: string[];
  readiness: string;
  detail: string | null;
  last_signal: string | null;
  signal_at: string | null;
  signal_stale: boolean;
  programs: PlanProgram[];
  pending: { approvals: number; runs: number; broken: string[] };
  calls_24h: number;
  errors_24h: number;
}

export interface PlanStatus {
  at: string;
  today: string;
  sessions_on: boolean;
  lanes: PlanAccount[];
  unattached_approvals: Record<string, number>;
  unavailable: string[];
}

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

// Engagement: an account that likes, follows, comments and posts by a plan (docs/ENGAGEMENT.md,
// agentos.api.routes_engage and agentos.api.web.engage). Fields are only ever added by the node.

export type EngageKind = 'post' | 'comment' | 'like' | 'follow' | 'join' | 'story_view' | 'profile_edit';

/** A pause on an account, or on one kind of action. Only the owner clears it, once checked. */
export interface EngageHold {
  id: number;
  scope: 'surface' | 'kind' | 'drafts';
  kind: EngageKind | null;
  signal: string | null;
  reason: string;
  detail: string | null;
  since: string;
  until: string | null;
  cleared_at: string | null;
  created_that_day: boolean;
  active_now: boolean;
}

/** What one kind of action did on one day, in the counts the engine keeps. */
export interface EngageOperation {
  kind: EngageKind;
  planned: number;
  moved_in: number;
  attempted: number;
  verified: number;
  unverified: number;
  failed: number;
  awaiting_approval: number;
  no_outcome: number;
  held: number;
  skipped: number;
  skip_reasons: Record<string, number>;
  pending: number;
  failed_reasons: Record<string, number>;
  held_reasons: Record<string, number>;
}

/** One planned visit to the account, with how it went. */
export interface EngageWindow {
  idx: number;
  origin: string;
  planned_start: string;
  minutes: number;
  started_at: string | null;
  ended_at: string | null;
  status: 'scheduled' | 'running' | 'done' | 'ended_early' | 'aborted' | 'refused' | 'missed';
  end_reason: string | null;
  operations: Record<string, number>;
}

export interface EngageSessions {
  planned: number;
  rescheduled: number;
  run: number;
  scheduled: number;
  running: number;
  done: number;
  ended_early: number;
  aborted: number;
  refused: number;
  missed: number;
  windows: EngageWindow[];
}

/** One day's cap, as used against the daily limit and the hour's limit. */
export interface EngageCapUse {
  kind: EngageKind;
  used: number;
  cap: number | null;
  ceiling: number;
  hourly: number | null;
}

/** A draft the provider wrote, waiting for the owner's yes. Its words are never sent to a member. */
export interface EngageDraftWaiting {
  id: string;
  kind: EngageKind;
  approval_id: string | null;
  target: string | null;
  created_at: string;
  run_id: string | null;
  preview: string | null;
  day: string;
}

/** One account's day (`DayRecord` in docs/API.md). */
export interface EngageDay {
  schema: number;
  surface: string;
  site: string;
  day: string;
  timezone: string;
  timezone_fallback: string | null;
  program: { id: string; version: number; approved: boolean } | null;
  sessions_on: boolean;
  plan_saved: boolean;
  quiet: boolean;
  phase: string | null;
  sessions: EngageSessions;
  totals: { planned: number; attempted: number; verified: number; failed: number; awaiting_approval: number; skipped: number };
  operations: EngageOperation[];
  caps: EngageCapUse[];
  drafts: { awaiting_approval: number; created: number; failed: number; expired: number; provider_paused: boolean; waiting: EngageDraftWaiting[] };
  awaiting_now: { count: number; days: Record<string, number>; drafts: EngageDraftWaiting[] };
  items: { submitted: number; used: number; failed: number; expired: number; withdrawn: number; queued_now: number; awaiting_now: number };
  holds: EngageHold[];
  signals: { signal: string; at: string; kind: string | null; session: number | null }[];
  notices: { at: string; kind: string; text: string }[];
}

/** One day of an account, as a bar in the week picker. */
export interface EngageWeekDay {
  day: string;
  sessions_planned: number;
  sessions_rescheduled: number;
  sessions_run: number;
  attempted: number;
  verified: number;
  awaiting_approval: number;
  holds: number;
  signals: number;
}

/** `GET /engage/digest`: every account the person may see, on one day. */
export interface EngageDays {
  available: boolean;
  schema: number;
  sessions_on: boolean | null;
  surfaces: EngageDay[];
}

/** `GET /engage/digest/{ws}/{name}`: one account's day and its last seven days. */
export interface EngageAccountDay {
  available: boolean;
  record: EngageDay;
  week: EngageWeekDay[];
}

/** One kind of action on the account's site: the engine's table, with the owner's own limit. */
export interface EngageCapRow {
  kind: EngageKind;
  careful_start: number;
  careful_ceiling: number;
  normal_start: number;
  normal_ceiling: number;
  hard_ceiling: number;
  hourly: number;
  /** The owner's own daily limit, or null for the ramp. */
  manual: number | null;
  /** What today's plan allows, or null when no plan is saved for today. */
  today: number | null;
  hourly_today: number | null;
}

export interface EngageVersion {
  program: string;
  version: number;
  state: 'draft' | 'approved' | 'superseded';
  hash: string;
  created_at: string;
  created_by: string | null;
  approved_at: string | null;
  approved_by: string | null;
}

/** An account's program: what it does and how much. `may_change` is the owner in person. */
export interface EngageProgram {
  available: boolean;
  surface: string;
  site: string | null;
  state: 'approved' | 'draft' | null;
  program: {
    id: string;
    version: number;
    hash: string;
    state: 'draft' | 'approved' | 'superseded';
    approved_at: string | null;
    approved_by: string | null;
    risk: 'careful' | 'normal' | 'off';
    summary: string;
    yaml: string;
  } | null;
  versions: EngageVersion[];
  caps: EngageCapRow[];
  items: Record<string, number>;
  may_change: boolean;
}

/** An item the owner queued for a session to send. Its words show to the owner in person only. */
export interface EngageItem {
  id: string;
  surface: string;
  kind: 'post' | 'comment';
  status: 'queued' | 'reserved' | 'awaiting' | 'used' | 'failed' | 'expired' | 'withdrawn';
  title: string | null;
  targets: string[];
  attachments: unknown[];
  files: unknown[];
  expires_at: string;
  submitted_by: string;
  submitted_at: string;
  requested_by: string | null;
  used_run_id: string | null;
  settled_at: string | null;
  text: string | null;
}

export interface EngageItems {
  available: boolean;
  surface: string;
  items: EngageItem[];
}

export interface EngageCapSet {
  surface: string;
  kind: EngageKind;
  cap: number | null;
  hard_ceiling: number;
}

/** What an account acts as: a business's own account, a Page, a company, or the lane's own login. */
export interface SurfaceActsAs {
  kind: 'account' | 'fb_page' | 'li_company' | 'ig_business' | 'lane';
  name: string | null;
  id: string | null;
  user_id: string | null;
}

/** A surface, the identity an account acts as through a lane (docs/design/connectors.md). */
export interface Surface {
  id: string;
  workspace: string;
  name: string;
  site: string;
  lane: string;
  acts_as: SurfaceActsAs;
  account_language: string;
  title: string | null;
}

export interface NewSurface {
  id: string;
  site: string;
  lane: string;
  acts_as: { kind: SurfaceActsAs['kind']; name?: string | null; id?: string | null; user_id?: string | null };
  account_language?: string;
  title?: string | null;
}

/** A connector operation's health on one surface: healthy, broken, held or unknown. */
export interface ConnectorSurface {
  surface: string;
  acts_as: string;
  health: 'healthy' | 'broken' | 'held' | 'unknown';
  lane?: string;
  health_detail?: Record<string, string | number>;
}

/** What an account can do: an installed connector operation, and the surfaces it runs on. */
export interface ConnectorOperation {
  operation: string;
  tool: string;
  title: string;
  effect: Effect;
  acts_as: string | null;
  version: string | null;
  contract: string | null;
  surfaces: ConnectorSurface[];
}

export interface ConnectorRun {
  id: string;
  flow: string;
  status: string;
  duplicate: boolean;
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

/** Every lane and live grant meter, for the caller's view. */
export interface Meters {
  timezone: string;
  day: string;
  resets_at: string;
  warn_at: number[];
  grants: Meter[];
  lanes: Meter[];
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

/**
 * Environments (docs/design/environments.md, "Web routes"): a cloud computer an agent rents for one
 * chat, with a terminal, a browser and the repository, deleted when the work is done. "working" is a
 * call in the last 2 minutes, "idle" none for 10.
 */
export type EnvState = 'starting' | 'ready' | 'working' | 'idle' | 'closing' | 'closed' | 'failed';

export interface EnvMachine {
  provider: string;
  server_type: string;
  cpu: number;
  memory_gb: number;
  location: string;
}

export interface EnvSummary {
  id: string;
  title: string;
  state: EnvState;
  repo?: string | null;
  branch?: string | null;
  created_at: string;
  last_call_at?: string | null;
  closed_at?: string | null;
  expires_at: string;
  machine: EnvMachine;
  price_hourly_eur: number;
  cost_eur: number;
  agent: { name: string; client?: string | null };
  lanes: { terminal: string; browser: string };
}

export interface EnvList {
  environments: EnvSummary[];
}

export interface EnvPort {
  port: number;
  process?: string | null;
  /** A link the node forwards to the port, behind the app's sign-in. None when it cannot forward it. */
  link?: string | null;
}

export interface EnvPush {
  branch: string;
  commit: string;
  at: string;
}

export interface EnvGit {
  branch: string | null;
  /** Commits on the branch that are not pushed. */
  ahead: number;
  /** Files changed and not committed. */
  dirty: number;
  pushed: EnvPush[];
}

export interface EnvCall {
  at: string;
  tool: string;
  summary: string;
  ok: boolean;
}

/** A command on the environment's terminal: a job (`lane_job_start`, it has a run page) or an exec. */
export interface EnvCommand {
  id: string;
  cmd: string;
  state: string;
  started_at: string;
  exit_code?: number | null;
}

/** What closing would lose: files changed and not committed, commits on any branch no remote has, and stashes. */
export interface EnvUnsaved {
  dirty_files: string[];
  unpushed: string[];
  stashes?: string[];
}

export interface EnvDetail extends EnvSummary {
  ports: EnvPort[];
  /** Null when the environment has no repository. */
  git: EnvGit | null;
  /** Newest first, at most 50. */
  calls: EnvCall[];
  commands: EnvCommand[];
  /** The browser's live view, as an account's. Null while it has none (starting, closed). */
  viewer: { path: string } | null;
  closed_reason?: string | null;
  unsaved: EnvUnsaved | null;
}

