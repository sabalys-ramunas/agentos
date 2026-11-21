import type { Approval, CodeRequest, Question } from './approvals';
import type { FreezeScope } from './freeze';
import type { Lane, LaneControl } from './lanes';
import type { Brain, FleetNode } from './nodes';
import type { Run, Schedule, TraceEvent } from './runs';

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
  /** Where the agent acts on a screen whose live view this person has open, or that its pointer should hide. */
  | { type: 'pointer'; payload: AgentPointerEvent }
  | { type: 'resync'; payload: unknown };

/**
 * A point on the lane's screen (`x`, `y` in the screen's pixels of `w` by `h`) and what the agent did there, or
 * `hide` (it works elsewhere: its terminal, a window that is not the browser). Sent only while a live view of the
 * lane is open, never replayed.
 */
export interface AgentPointerEvent {
  lane: string;
  x?: number;
  y?: number;
  w?: number;
  h?: number;
  kind?: 'move' | 'press' | 'scroll' | 'type';
  hide?: string;
}

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
  'pointer',
  'resync',
] as const;
