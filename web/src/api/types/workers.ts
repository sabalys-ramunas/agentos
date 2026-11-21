import type { NodeState, Principal } from './lanes';

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
  /** It is on, and starts the next time openajar starts on this computer. */
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
