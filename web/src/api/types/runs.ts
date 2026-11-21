import type { ControlAction, Cost, Effect, Principal, RunStatus } from './lanes';
import type { RoutineAlwaysAsks, RoutineSendLimit, RoutineStep, RoutineTrigger } from './routines';

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
  /** False: its computer has an older openajar, which runs a routine but cannot change its steps. */
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
