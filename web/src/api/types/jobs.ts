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
