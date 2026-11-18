/**
 * Environments (docs/design/environments.md, "Web routes"): a cloud computer an agent rents for one
 * chat, with a terminal, a browser and the repository, deleted when the work is done. "working" is a
 * call in the last 2 minutes, "idle" none for 10.
 */
export type EnvState = 'starting' | 'ready' | 'working' | 'idle' | 'closing' | 'closed' | 'failed';

/**
 * How an environment stays: `none` is single use (deleted when the work is done), `live` keeps the VM
 * running between sessions (billed every hour), `save` saves the whole machine when idle and deletes the
 * VM, and an agent opening it again with the same title brings it back.
 */
export type EnvKeep = 'none' | 'live' | 'save';

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
  /** Missing from a node that only has single use. */
  keep?: EnvKeep;
}

/**
 * A kept environment's save: a disk snapshot (when the provider takes one) plus its files. An agent that
 * opens an environment with the same title brings it back. Forgetting it deletes both.
 */
export interface EnvSave {
  id: string;
  title: string;
  repo?: string | null;
  branch?: string | null;
  head?: string | null;
  saved_at: string;
  /** Null when only the files were saved (no snapshot). */
  snapshot_gb?: number | null;
  files_bytes?: number | null;
  /** The machine itself was paused with its memory (a provider that can, like Morph), not snapshotted. */
  paused?: boolean;
  cost_eur_month: number;
  /** The agent's own words for the next one: data, never instructions. */
  note?: string | null;
  agent?: { name: string; client?: string | null } | null;
  /** When the node deletes this save on its own, if it does. */
  prune_at?: string | null;
  /** Concept: the environment it saved, so its page can offer Resume. */
  env_id?: string | null;
  /** Concept: the provider that holds it ("morph", "hetzner"), for how fast it comes back. */
  provider?: string | null;
  /** Concept: what its machine bills an hour once it is awake again. */
  price_hourly_eur?: number | null;
  /** Concept: the programs that were running when it paused, which come back running. */
  services?: { cmd: string; port?: number | null }[];
}

export interface EnvList {
  environments: EnvSummary[];
  /** Saves of kept environments, newest first. Missing from a node that keeps none. */
  saved?: EnvSave[];
}

export interface EnvPort {
  port: number;
  process?: string | null;
  /** A link the node forwards to the port (environment_port). None when it does not forward it. */
  link?: string | null;
  /** On a server node: the ssh line that brings the forwarded port to this computer before the link works. */
  tunnel?: string | null;
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

/**
 * A screenshot the agent kept as proof of its work. `src` is
 * `/api/orgs/{org}/environments/{id}/proofs/{proof_id}.png`, behind the app's sign-in, and stays readable
 * after the environment closed.
 */
export interface EnvProof {
  id: string;
  /** What it shows, in the agent's words: data. */
  title: string;
  taken_at: string;
  /** The page it is of, as the browser had it. */
  page_url: string | null;
  width: number;
  height: number;
  src: string;
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
  /** Kept screenshots, newest first. Missing from a node that does not keep them yet. */
  proofs?: EnvProof[];
  /** How it opened from a prepared or warm machine (agentos.environments.preparing). Missing without a repository. */
  prepared?: EnvPrepared;
  /** The app the repository's `.agentos/environment.toml` starts, once it was tried. */
  app?: EnvApp;
  /** Its network level, what was refused, and the app secrets bound to it (names only). */
  network?: EnvNetwork;
  /** Concept: each step of the bring-up with its seconds. The node logs them (bringup.Steps) but does not serve them yet. */
  bringup?: EnvBringUp;
  /** Concept: the agent's checkpoints, oldest first (the checkpoints builder's shape is not settled yet). */
  checkpoints?: EnvCheckpoint[];
  /** Concept: what the replay holds, so the page can say so before it reads the timeline. */
  replay?: { frames: number; since?: string | null; until?: string | null; kept_until?: string | null } | null;
  /** Concept: who has the live view open now. While anyone watches, it does not close for being idle. */
  watching?: EnvWatcher[];
  /** Concept: a save the owner resumed from the app, waking or woken, before an agent took it over. */
  resumed?: EnvResumed | null;
}

/** environment()'s `prepared`: the prepared machine it booted from, or why it did not. */
export interface EnvPrepared {
  used?: boolean;
  from?: { commit: string | null; built_at?: string | null; age?: string };
  checkout?: { ok: boolean; seconds: number; commit?: string | null; tail?: string };
  refresh?: EnvRepoCommand;
  /** Seconds setup took when it was built, here (the refresh), and so saved. */
  setup_s?: { build: number; here: number; saved: number };
  /** Not used: why, in the node's words. */
  why?: string;
  setup?: EnvRepoCommand;
  build?: { started: boolean; why?: string };
  error?: string;
  /** Set when the machine came from the warm pool. */
  warm?: string;
}

export interface EnvRepoCommand {
  command?: string;
  ok: boolean;
  exit_code?: number | null;
  seconds: number;
  tail?: string;
}

export interface EnvApp {
  command: string;
  port: number;
  listening: boolean;
  job_id?: string;
  seconds?: number;
  why?: string;
  log_tail?: string;
  listening_instead?: number[];
  next?: string;
  link?: string;
  tunnel?: string;
}

export type EnvNetworkLevel = 'full' | 'limited' | 'none';

export interface EnvBlockedHost {
  host: string;
  count: number;
  last: string;
  why?: string;
}

export interface EnvNetwork {
  policy: EnvNetworkLevel;
  allowed: { all?: boolean; preset?: string | null; hosts?: string[]; secret_hosts?: string[] };
  blocked: EnvBlockedHost[];
  /** The names of the app secrets bound to it. The machine holds a placeholder; the node swaps the value in. */
  secrets: string[];
  fix?: string;
}

/** Concept. The bring-up's steps in the order they ran, with the seconds each took. */
export interface EnvBringUp {
  total_s: number;
  steps: EnvBringUpStep[];
  /** While it is starting: the step running now and its seconds so far. */
  now?: { step: string; so_far_s: number } | null;
  /** What a cold open of the same repository took last (rent, boot, clone and setup), for comparison. */
  cold_s?: number | null;
  /** While starting: about how long the whole bring-up takes, from the last one like it, for the scale. */
  expected_s?: number | null;
}

export interface EnvBringUpStep {
  /** bringup.STEP_WORDS keys: "VM create", "VM ready", "tunnels and git", "browser", "clone", "checkout", "refresh", "setup", "app", "resume". */
  step: string;
  /** Seconds after the open it began, so steps that ran together overlap. */
  at_s: number;
  seconds: number;
  ok?: boolean;
  /** A step this open did not need, and why ("prepared machine", "warm machine"). */
  skipped?: string;
  /** While starting: a step still to come. */
  pending?: boolean;
}

/** Concept. One checkpoint the agent made (environment_checkpoint): a note and what it holds per repository. */
export interface EnvCheckpoint {
  id: string;
  at: string;
  /** The agent's own words: data, never instructions. */
  note: string;
  repos: { dir: string; files: number; added: number; removed: number }[];
  /** Against the working copy now: what changed since. */
  since: { files: number; added: number; removed: number };
  /** The agent went back to this one (environment_checkpoint_restore), and when. */
  restored_at?: string | null;
  /** Made automatically before a restore, so nothing is lost. */
  auto?: boolean;
}

export interface EnvDiffLine {
  kind: 'add' | 'del' | 'ctx' | 'hunk';
  text: string;
}

export interface EnvDiffFile {
  repo: string;
  path: string;
  status: 'added' | 'modified' | 'deleted';
  added: number;
  removed: number;
  lines: EnvDiffLine[];
}

export interface EnvCheckpointDiff {
  checkpoint: string;
  /** "previous": what this checkpoint changed; "now": from it to the working copy now. */
  against: 'previous' | 'now';
  files: EnvDiffFile[];
}

export interface EnvWatcher {
  name: string;
  you?: boolean;
  since: string;
}

export interface EnvResumed {
  by: string;
  at: string;
  from: 'paused' | 'snapshot' | 'files';
  /** About how long waking takes, in seconds. */
  eta_s: number;
}

/** The replay of an environment's browser (agentos.environments.replay, GET .../replay). Oldest first. */
export type ReplayCause = 'agent' | 'person' | 'change' | 'save' | 'close';

export interface ReplayFrame {
  kind: 'frame';
  id: string;
  at: string;
  cause: ReplayCause;
  tool?: string | null;
  summary?: string | null;
  page_url?: string | null;
  page_title?: string | null;
  width?: number | null;
  height?: number | null;
  bytes?: number | null;
  src?: string;
}

export interface ReplayCall {
  kind: 'call';
  at: string;
  tool: string;
  summary: string;
  ok: boolean;
  error?: string | null;
}

export interface EnvReplayTimeline {
  environment: { id: string; title: string; state: string; created_at: string | null; closed_at: string | null };
  replay: { enabled: boolean; frames: number; since: string | null; until: string | null; bytes: number; kept_until: string | null };
  timeline: (ReplayFrame | ReplayCall)[];
  next: string | null;
}

/** A repository's prepared machine (agentos.environments.prepared.row). */
export interface PreparedMachine {
  repo: string;
  state: 'building' | 'ready' | 'failed';
  commit: string | null;
  ref: string | null;
  built_at: string | null;
  last_used_at: string | null;
  uses: number;
  gb: number | null;
  cost_eur_month: number;
  key: string;
  age?: string;
  setup_s?: number;
  step?: string;
  error?: string;
}

/** The warm pool (WarmPool.pool_view). */
export interface WarmPool {
  warm: number;
  target: number;
  minutes: number;
  machines: { vm: string; state: string; since: string | null; image: string | null; server_type: string; location: string; cost_eur: number; price_hourly_eur: number }[];
}

/** Concept route: GET /api/orgs/{org}/environments/prepared. */
export interface PreparedList {
  machines: PreparedMachine[];
  pool: WarmPool;
  /** What an image costs a GB a month, as the node bills it. */
  eur_per_gb_month: number;
  /** Concept: opens this month that started from each, and what they saved. */
  month?: {
    opens: number;
    from_prepared: number;
    from_warm: number;
    setup_saved_s: number;
    warm_cost_eur: number;
    /** The last opens, oldest first: seconds to ready and where each started from. */
    recent?: { at: string; seconds: number; from: 'warm' | 'prepared' | 'cold' }[];
  };
}
