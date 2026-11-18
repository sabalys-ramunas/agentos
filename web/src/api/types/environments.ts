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
  ready_at?: string | null;
  /** Why it failed to start, in the node's words, and the error's code. */
  error?: string | null;
  error_code?: string | null;
  /** Its newest save (keep `save`). */
  last_save?: { id: string; at: string | null } | null;
  save_error?: string | null;
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
  /** `save`, or `rescue`: the node saved unsaved work before it had to delete a single-use machine. */
  kind?: string;
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
  /** The environment it saved, so its page can offer Resume. */
  env_id?: string | null;
  /** The provider that holds it ("morph", "hetzner", "docker"), for how fast it comes back. */
  provider?: string | null;
  server_type?: string | null;
  /** What its machine bills an hour once it is awake again. Null when the node no longer knows. */
  price_hourly_eur?: number | null;
  /** The programs that were running when it was saved: a paused machine keeps them running, a snapshot starts them again. */
  services?: { cmd: string; port?: number | null }[];
}

export interface EnvList {
  environments: EnvSummary[];
  /** Saves of kept environments, newest first. Missing from a node that keeps none. */
  saved?: EnvSave[];
  /** What every kept snapshot costs a month. */
  snapshots_eur_month?: number;
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
  /** A screenshot (the default), or a video of the browser (browser_record). */
  kind?: 'screenshot' | 'video';
  /** What it shows, in the agent's words: data. */
  title: string;
  taken_at: string;
  /** The page it is of, as the browser had it. */
  page_url: string | null;
  width: number;
  height: number;
  /** A screenshot's PNG, or a video's MP4 (served with range requests, so a player seeks). */
  src: string;
  /** A video's length. */
  duration_s?: number | null;
  bytes?: number | null;
  /** How a video's recording ended ("stopped", "time limit"). */
  ended?: string | null;
  /** A video's GIF preview, when the node made one. */
  gif?: { width: number; height: number; bytes?: number } | null;
  gif_src?: string;
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
  /** Each step of the bring-up with its seconds, while it starts and once it is up. Null on an older record. */
  bringup?: EnvBringUp | null;
  /** The agent's checkpoints, newest first (agentos.environments.checkpoints). */
  checkpoints?: EnvCheckpoint[];
  /** What the replay holds, so the page can say so before it reads the timeline. Null when the node keeps none. */
  replay?: { frames: number; since?: string | null; bytes?: number; kept_until?: string | null } | null;
  /** Live views of its browser and connections to a shared port open now. While any is open, it does not close for being idle. */
  watched?: EnvWatched | null;
  /** What a resume brought back (environment_open's `resumed`), while it is that save's environment. */
  resumed?: EnvResumed | null;
  /** "owner": the owner resumed it from a save, held by no chat. */
  resumed_by?: string | null;
  /** Resumed by the owner and not taken by any chat yet: the next open of its title takes it over. */
  owner_woken?: boolean;
  /** A save or close an agent started goes on in the background. */
  background?: string;
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

/** The bring-up's steps in the order they ran, with the seconds each took (agentos.environments.bringup). */
export interface EnvBringUp {
  /** Seconds from the open to ready (or failed), or so far while it starts. */
  total_s: number | null;
  /** Null while it starts. */
  ok: boolean | null;
  steps: EnvBringUpStep[];
  /** While it starts: the steps running now, with their seconds so far. Several run at once. */
  running?: { step: string; at_s: number; so_far_s: number }[];
  /** On a resume: what it wakes from. */
  restore_from?: 'snapshot' | 'paused' | 'files' | null;
}

export interface EnvBringUpStep {
  /** bringup.STEP_WORDS keys: "VM create", "VM ready", "tunnels and git", "browser", "clone", "checkout", "refresh", "setup", "app", "restore". */
  step: string;
  /** Seconds after the open it began, so steps that ran together overlap. */
  at_s: number;
  seconds: number;
  ok?: boolean;
  /** A step this open did not need, and why ("prepared machine", "warm machine"). */
  skipped?: string;
  /** What a skipped step took the last time it ran, when the node knows. */
  saved_s?: number | null;
}

/** What changed since a checkpoint in one repository, against the work tree now. */
export interface EnvCheckpointChange {
  files_added: number;
  files_changed: number;
  files_removed: number;
  lines_added: number;
  lines_removed: number;
}

/** One checkpoint the agent made (environment_checkpoint): git's, on the machine, per repository. */
export interface EnvCheckpoint {
  /** Its number, 1 up. */
  id: number;
  /** The agent's own words: data, never instructions. */
  note: string | null;
  at: string | null;
  /** Made by agentos before a rollback: "before rollback to 2". */
  auto: string | null;
  repos: {
    dir: string;
    /** The commit it sits on, 12 characters. */
    head: string | null;
    /** Files it had changed from that commit. */
    files: number;
    /** From it to now. Null when the machine did not answer. */
    diff?: EnvCheckpointChange | null;
  }[];
}

/** One checkpoint against the work tree now, per repository (capped). */
export interface EnvCheckpointDiff {
  checkpoint: number;
  max_bytes: number;
  max_files: number;
  repos: {
    dir: string;
    files_total: number;
    /** git's status from the checkpoint to now: A a file made since (a rollback removes it), M changed, D deleted, T its type changed. Line counts are null for a binary file. */
    files: { path: string; status: 'A' | 'M' | 'D' | 'T'; lines_added: number | null; lines_removed: number | null }[];
    patch: string;
    patch_bytes: number;
    truncated: boolean;
  }[];
}

export interface EnvWatched {
  since: string | null;
  viewers: number;
  live_views: number;
  port_connections: number;
}

export interface EnvResumed {
  save_id?: string;
  saved_at?: string | null;
  from?: 'snapshot' | 'paused' | 'files';
  state?: string;
  note?: string | null;
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

/** The warm pool (WarmPool.pool_view), and what it costs. */
export interface WarmPool {
  warm: number;
  target: number;
  minutes: number;
  machines: { vm: string; state: string; since: string | null; image: string | null; server_type: string; location: string; cost_eur: number | null; price_hourly_eur: number | null }[];
  /** What the machine the pool rents bills an hour. Null when the node does not know. */
  price_hourly_eur?: number | null;
  /** The most the pool can cost a month if no open ever claims a machine. */
  worst_eur_month?: number | null;
  /** What the pool's machines cost this month so far. */
  month_eur?: number | null;
}

/** GET /api/orgs/{org}/environments/prepared: the owner's prepared machines and warm pool. */
export interface PreparedList {
  machines: PreparedMachine[];
  /** What the images cost together a month. */
  eur_month?: number;
  /** What an image costs a GB a month, as the node bills it. */
  eur_per_gb_month: number;
  pool: WarmPool;
  /** This month's opens: how many started from each, and what they saved. */
  month?: {
    opens: number;
    from_prepared: number;
    from_warm: number;
    setup_saved_s: number;
    /** The last opens, oldest first: seconds to ready and where each started from. */
    recent?: { at: string; seconds: number; from: 'warm' | 'prepared' | 'cold' }[];
  };
}

/** What the owner's resume answers: the summary, how it opened, and what it brought back. */
export interface EnvResumeAnswer extends EnvSummary {
  /** `already_open`: its title was open, and that one is returned unchanged. */
  opened: 'resumed' | 'already_open';
  resumed?: EnvResumed | null;
}
