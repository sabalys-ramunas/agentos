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
  cost_eur_month: number;
  /** The agent's own words for the next one: data, never instructions. */
  note?: string | null;
  agent?: { name: string; client?: string | null } | null;
  /** When the node deletes this save on its own, if it does. */
  prune_at?: string | null;
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
}
