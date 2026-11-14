import type { Approval, Effect, Flow, LaneAskFirst, StandingApproval } from '@/api/types';
import { effectLabel, laneName } from '@/lib/labels';

/** Never offered: a person decides these every time, and the API refuses them. */
export const NEVER_STANDING: ReadonlySet<Effect> = new Set(['pay', 'irreversible', 'install_base']);

/** What a form may offer. `read` and `draft` never ask, so there is nothing to stand in for. */
export const STANDING_EFFECTS: Effect[] = ['commit', 'secret', 'exec', 'service', 'brain', 'install_overlay'];

/** What a computer on its own takes: a commit only (a standing approval's `effect` is `commit` there). */
export const NODE_STANDING_EFFECTS: Effect[] = ['commit'];

/** The covers to offer: the node's one, or every one the hosted service keeps. */
export function standingEffects(alone: boolean): Effect[] {
  return alone ? NODE_STANDING_EFFECTS : STANDING_EFFECTS;
}

/** `flow:aster.weekly-post@4` reads as "flow aster.weekly-post, version 4". */
export function targetWords(target: string): string {
  const flow = /^flow:(.+)@(.+)$/.exec(target);
  if (flow) return `routine ${flow[1]}, version ${flow[2]}`;
  return target.startsWith('tool:') ? `tool ${target.slice(5)}` : target;
}

export function describeStanding(item: Pick<StandingApproval, 'effect' | 'targets'>): string {
  const what = effectLabel[item.effect];
  if (!item.targets.length) return what;
  const [first, ...rest] = item.targets;
  return `${what} with ${targetWords(first ?? '')}${rest.length ? ` and ${rest.length} more` : ''}`;
}

export function lanesWords(lanes: string[] | null, workspaceName: string): string {
  if (!lanes?.length) return `Every account in ${workspaceName}`;
  const names = lanes.map((lane) => (lane.endsWith('*') ? `${lane} (pattern)` : laneName(lane.split('/')[1] ?? lane)));
  return names.length > 3 ? `${names.slice(0, 3).join(', ')} and ${names.length - 3} more` : names.join(', ');
}

/** Does this standing approval reach this account? */
export function covers(item: Pick<StandingApproval, 'workspace' | 'lanes'>, lane: string): boolean {
  if (lane.split('/')[0] !== item.workspace) return false;
  if (!item.lanes?.length) return true;
  return item.lanes.some((pattern) => new RegExp(`^${pattern.replace(/[.+?^${}()|[\]\\]/g, '\\$&').replaceAll('*', '[^/]*')}$`).test(lane));
}

export interface LimitGap {
  /** Routines it is bound to, or else the accounts it covers, that run on their own. */
  routines: string[];
  lanes: string[];
}

/**
 * With Ask me first off, a commit runs on its own and uses no standing approval, so "20 posts until
 * Friday" caps nothing there (the core's ask-first-default audit, finding 3). Null where the limit
 * holds: it is not a commit, it is over, or everything it covers asks first. `asks_first` is the
 * cloud's word, so a computer that always asks is never flagged. Unknown switches claim nothing.
 */
export function limitGap(item: StandingApproval, asks: LaneAskFirst[] | undefined, flows: Flow[] | undefined): LimitGap | null {
  if (item.state !== 'active' || item.effect !== 'commit' || !asks || !flows) return null;
  const bound = item.targets.flatMap((target) => flows.filter((flow) => target.replace(/^flow:/, '').replace(/@.*$/, '') === flow.id));
  const routines = bound.filter((flow) => flow.asks_first === false).map((flow) => flow.id);
  const lanes = bound.length ? [] : asks.filter((ask) => !ask.asks_first && covers(item, ask.lane)).map((ask) => ask.lane);
  return routines.length || lanes.length ? { routines, lanes } : null;
}

/**
 * Whether a routine can be named as a standing approval's target. The node matches a target against
 * what a run asks. A recorded routine asks as `flow:<id>@<its content hash>`, and for publishing a
 * target alone covers only a run with no inputs. A routine built in the builder has a count of its
 * saves as its version, and its posting step asks for the recorded routine it runs, never the
 * builder's own id. So a builder routine never matches, and nor does one with inputs.
 */
export function standsAsTarget(flow: Pick<Flow, 'version' | 'params' | 'effect'>): boolean {
  if (typeof flow.version !== 'string' || flow.version === '') return false;
  if (flow.effect === 'commit') return Object.keys(flow.params ?? {}).length === 0;
  return true;
}

/**
 * Why some of a workspace's routines are not offered as a target, in a sentence that says what to do
 * instead: allow one of their approvals again from Needs you, which covers that exact post. Every reason
 * a routine is left out is said. Null when every routine can be named.
 */
export function standingNote(routines: Pick<Flow, 'version' | 'params' | 'effect'>[]): string | null {
  const skipped = routines.filter((flow) => !standsAsTarget(flow));
  const built = skipped.some((flow) => typeof flow.version !== 'string' || flow.version === '');
  const withInputs = skipped.some((flow) => typeof flow.version === 'string' && flow.version !== '');
  if (built && withInputs) return 'Routines built in the builder and routines with inputs ask each time. Allow one of their approvals again from Needs you.';
  if (built) return 'Routines built in the builder ask each time. Allow one of their approvals again from Needs you.';
  if (withInputs) return 'A routine with inputs asks each time. Allow one of its approvals again from Needs you.';
  return null;
}

/** Whether "Allow this again" can be offered for this approval. On a computer on its own only a commit stands. */
export function canAllowAgain(approval: Approval, alone = false): boolean {
  return (
    approval.kind !== 'question' &&
    standingEffects(alone).includes(approval.effect) &&
    !NEVER_STANDING.has(approval.effect) &&
    Boolean(approval.content_digest) &&
    Boolean(approval.lane) &&
    Boolean(approval.flow_target || approval.tool) &&
    (approval.state === 'pending' || approval.state === 'approved')
  );
}
