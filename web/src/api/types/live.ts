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
