/**
 * The four states a person reads at once, plus Working now:
 *
 *   All good · Waiting for you · Stuck · Sleeping · Working now (pink, live)
 *
 * Every lane, computer, approval and question condition maps to exactly one, with one plain
 * sentence that says what is going on. Screens show `StateBadge` (a shaped glyph and the word)
 * and the sentence; they never invent their own word for a condition.
 */
import type { Approval, FleetNode, Lane, NodeState, Question } from '@/api/types';
import { principalName } from './principal';
import type { PeopleLookup } from './principal';
import { clock } from './format';
import { FREEZE_WORDS } from './vocabulary';

export type PersonState = 'good' | 'waiting' | 'stuck' | 'sleeping' | 'working';

export const STATE_WORDS: Record<PersonState, string> = {
  good: 'All good',
  waiting: 'Waiting for you',
  stuck: 'Stuck',
  sleeping: 'Sleeping',
  working: 'Working now',
};

/** Heaviest first. A roll-up shows the first state any of its parts is in. */
export const STATE_ORDER: PersonState[] = ['stuck', 'waiting', 'working', 'good', 'sleeping'];

export interface Condition {
  state: PersonState;
  /** One plain sentence, without a full stop, that fits a row: "Signed in as someone else". */
  sentence: string;
  /** The word to show in place of the state's own, when the state alone would not say what it is: "Frozen". */
  label?: string;
}

const condition = (state: PersonState, sentence: string): Condition => ({ state, sentence });

export interface LaneContext extends PeopleLookup {
  /** An agent is acting on it right now (from the live feed or topology `active`). */
  acting?: boolean;
  /** The state of the computer it runs on, when known. */
  node?: NodeState | null;
}

/**
 * One lane, in the person's words. Order matters: what stops the account outright comes first,
 * then what a person has to do, then what is going on, then rest.
 */
export function laneCondition(lane: Lane, context: LaneContext = {}): Condition {
  if (context.node === 'down') return condition('stuck', 'Its computer is offline');
  if (lane.missing_since) return condition('stuck', 'Its computer stopped reporting it');
  if (lane.account?.status === 'wrong_account') return condition('stuck', 'Signed in as someone else');
  // A freeze stops every agent on the account, whatever else looks fine. The computer's own word for it is Frozen.
  if (lane.frozen === true) return { state: 'stuck', label: FREEZE_WORDS.frozen.word, sentence: 'Nothing runs on it until the freeze is lifted' };
  if (lane.state === 'quarantined') return condition('stuck', 'Switched off for safety');
  if (lane.signal === 'blocked') return condition('stuck', 'The site is blocking it');
  // The account's own last check failed (its profile is gone, its sign-in is broken): not safe to use, whatever else looks fine.
  if (lane.check?.safe === false || lane.check?.level === 'fail') return condition('stuck', 'Not safe to use right now');

  // A hold with an end clears by itself, so it is rest, not a job for anyone.
  if (lane.hold_until && lane.signal !== 'ok') return condition('sleeping', `Held off until ${clock(lane.hold_until)}, then it carries on`);
  if (lane.signal === 'logged_out') return condition('waiting', 'Needs you to sign in');
  // An agent asked for a login that nobody has started: Needs you lists it as a hand, so the account says so too.
  if (lane.login_request) return condition('waiting', 'An agent asked you to log in');
  if (lane.signal === 'captcha') return condition('waiting', 'Asked a robot check');
  if (lane.signal === 'checkpoint') return condition('waiting', 'The site wants to confirm it is you');
  if (lane.control.takeback_requested) {
    const who = lane.control.takeback_by ? principalName(lane.control.takeback_by, context) : 'An agent';
    return condition('waiting', `${who} is asking for control back`);
  }
  if (lane.control.state === 'paused') return condition('waiting', 'Agents are paused until someone lets them carry on');
  if (lane.control.state === 'human') {
    const mine = context.me && lane.control.by_id === context.me.id;
    return condition('good', mine ? 'You have control' : `${lane.control.by ?? 'A person'} has control`);
  }

  if (context.acting || lane.state === 'active') return condition('working', 'An agent is working on it now');
  if (lane.signal === 'rate_limited') return condition('sleeping', 'The site asked it to slow down. It carries on by itself');
  if (lane.state === 'warming') return condition('good', 'Waking up');
  if (lane.state === 'cold') return condition('sleeping', 'Resting until the next job');
  return condition('good', 'Ready');
}

export function nodeCondition(node: Pick<FleetNode, 'state'>): Condition {
  switch (node.state) {
    case 'down':
      return condition('stuck', 'Offline');
    case 'degraded':
      return condition('waiting', 'Struggling, and still running more slowly');
    default:
      return condition('good', 'Online');
  }
}

const ASK: Partial<Record<Approval['effect'], string>> = {
  commit: 'Wants to go ahead, and waits for your yes',
  pay: 'Wants to pay, and waits for your yes',
  irreversible: 'Wants to do something that cannot be undone',
  secret: 'Wants to use a saved login',
  draft: 'Wants to save a draft',
};

/**
 * An approval. A pending one always waits for a person; when its account cannot act, the
 * sentence says so, because a yes then only queues it.
 */
export function approvalCondition(approval: Pick<Approval, 'state' | 'effect'>, lane?: Lane | null): Condition {
  if (approval.state === 'pending') {
    const blocker = lane ? laneCondition(lane) : null;
    if (blocker && (blocker.state === 'stuck' || blocker.state === 'waiting')) {
      return condition('waiting', `Waits for you, but the account is not ready: ${blocker.sentence.charAt(0).toLowerCase()}${blocker.sentence.slice(1)}`);
    }
    return condition('waiting', ASK[approval.effect] ?? 'Waits for your yes');
  }
  if (approval.state === 'expired') return condition('sleeping', 'Nobody answered in time, so nothing happened');
  if (approval.state === 'denied') return condition('good', 'You said no, so nothing happened');
  return condition('good', 'You said yes');
}

export function questionCondition(question: Pick<Question, 'state' | 'asked_by'>, people: PeopleLookup = {}): Condition {
  if (question.state === 'open') return condition('waiting', `${principalName(question.asked_by, people)} asked you something`);
  if (question.state === 'expired') return condition('sleeping', 'Nobody answered in time');
  return condition('good', 'Answered');
}

/**
 * A roll-up for a workspace or any group: Stuck beats Waiting beats All good, and Sleeping only
 * when every part sleeps. Working now is activity, not health, so it never becomes the group's
 * state (visual.md P3); its parts still show it.
 */
export function rollUp(states: PersonState[], waitingForYou = 0): PersonState {
  if (states.includes('stuck')) return 'stuck';
  if (waitingForYou > 0 || states.includes('waiting')) return 'waiting';
  if (states.length > 0 && states.every((state) => state === 'sleeping')) return 'sleeping';
  return 'good';
}
