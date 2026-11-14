/** Rules about lanes that more than one screen needs, kept in one place. */
import type { Lane } from '@/api/types';
import type { StatusLabel, Tone } from './labels';
import type { LaneContext, PersonState } from './states';
import { laneCondition, STATE_WORDS } from './states';

/** Signals only a person can clear. A rate limit is left out: it ends by itself. */
const HUMAN_SIGNALS = new Set<Lane['signal']>(['captcha', 'checkpoint', 'logged_out', 'blocked']);

/** An account a person must help with: a challenge, a sign-in, an agent asking for it back or for a login. */
export function needsHand(lane: Lane): boolean {
  return (
    HUMAN_SIGNALS.has(lane.signal) ||
    lane.state === 'quarantined' ||
    lane.account?.status === 'wrong_account' ||
    lane.control.takeback_requested === true ||
    Boolean(lane.login_request)
  );
}

/** The tone of each state's word on a status line: stuck is red, waiting amber, working the live colour. */
const TONE: Record<PersonState, Tone> = { good: 'ok', waiting: 'warn', stuck: 'bad', sleeping: 'idle', working: 'info' };

/**
 * One lane in one word, from the rule Accounts uses (`laneCondition`): the state's own word, or the word a
 * freeze gets. Every screen that names a lane's state reads it here, so a lane never says Ready on one
 * screen and Stuck on another. Pass the same context as the screen's page (its computer's state, the person).
 */
export function laneStatus(lane: Lane, context: LaneContext = {}): StatusLabel {
  const condition = laneCondition(lane, context);
  return { label: condition.label ?? STATE_WORDS[condition.state], tone: TONE[condition.state], pulse: condition.state === 'working' };
}

/** A trace or a run may carry either form of principal. */
export { principalId } from './principal';

export const idleControl = {
  state: 'agent' as const,
  by: null,
  since: null,
  takeback_requested: false,
  takeback_reason: null,
  takeback_by: null,
  last_human_input: null,
};
