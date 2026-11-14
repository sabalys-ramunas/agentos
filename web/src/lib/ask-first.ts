/**
 * "Ask me first" in words, and what a routine or an account does next. One place, so every screen
 * says the same thing. The cloud works out what really happens (`asks_first`, `asks_because`); this
 * only turns that into a sentence and says whether a switch here would do anything. A switch that
 * cannot change anything is never drawn: it says why instead.
 */
import type { AskFirstSetBy, Effect, Flow, LaneAskFirst, WorkerAccount } from '@/api/types';
import { routineName } from './principal';

export const RUNS = 'Runs on its own';
export const ASKS = 'Asks you first';
/** A routine that publishes, started by someone who may not decide here: a person who may decides in Needs you. */
export const WAITS = 'Waits for a yes';
const WAITS_IN_NEEDS_YOU = 'It waits in Needs you for a yes each time before it acts.';
export const ALWAYS_ASKS = "Money and things that can't be undone always ask you, whatever this switch says.";
/**
 * A publish by clicking is refused only where the lane's guard recognises it (ROUTING.md, "Clicks");
 * one no rule matches still goes through. So an account's switch never promises more than this.
 */
export const ACCOUNT_LIMIT = 'This switch stops posts on sites agentos recognises. A routine is the surest way to control what goes out.';
export const ALWAYS_ASKS_NO_SWITCH = "Money and things that can't be undone always ask you.";
/** Only ever asked when turning a switch off: it is the moment a person stops being asked. */
export const TURNING_OFF = "It will act without waiting for anyone, so agentos checks it is you first. Money and things that can't be undone still ask. You will not be asked to confirm again for 10 minutes.";

const OLDER_COMPUTER = 'This computer runs an older version, so it asks you before it acts. Update it to choose what runs on its own.';
export const AWAY = 'Its computer is offline, so this can change once it is back.';

/** Asks whatever any switch says. */
const ALWAYS: Partial<Record<Effect, { sentence: string; short: string }>> = {
  pay: { sentence: 'It spends money, so it asks you every time.', short: 'Money always asks' },
  irreversible: { sentence: "It can't be undone, so it asks you every time.", short: "Can't be undone" },
  install_base: { sentence: 'It changes every account on the computer, so it asks you every time.', short: 'Changes the computer' },
};

/** Has nothing to ask about, so a switch would do nothing. */
const QUIET: Partial<Record<Effect, { sentence: string; short: string }>> = {
  read: { sentence: 'It only looks, so there is nothing to ask you about.', short: 'Only looks' },
  draft: { sentence: 'It only writes drafts, so there is nothing to ask you about.', short: 'Only drafts' },
};

export interface AskView {
  /** What really happens next time. */
  asks: boolean;
  word: typeof RUNS | typeof ASKS | typeof WAITS;
  sentence: string;
  /** A few words for a row, when the row needs more than the word. */
  short: string | null;
  /** The switch's own state, when a switch here would change something. */
  switchable: boolean;
  checked: boolean;
  /** Never changes, whatever anyone does: money, can't-undo, or only looking. */
  fixed: boolean;
  /** Its account decides: the place to change it. */
  account: string | null;
}

const view = (asks: boolean, sentence: string, rest: Partial<AskView> = {}): AskView => ({
  asks,
  word: asks ? ASKS : RUNS,
  sentence,
  short: null,
  switchable: false,
  checked: asks,
  fixed: false,
  account: null,
  ...rest,
});

/**
 * Null from an older cloud, which says nothing about it: then neither do we. `canDecide` is whether the person
 * looking may decide in this business: a publishing run started by someone who may not waits in Needs you for
 * a yes whatever the switch says, so a member is never told it runs on its own.
 */
export function routineAsk(flow: Flow, canDecide = true): AskView | null {
  if (flow.ask_first === undefined) return null;
  const always = ALWAYS[flow.effect];
  if (flow.always_asks) return view(true, always?.sentence ?? 'It always asks you first.', { short: always?.short ?? null, fixed: true });
  const quiet = QUIET[flow.effect];
  if (quiet) return view(false, quiet.sentence, { short: quiet.short, fixed: true });
  if (flow.asks_because === 'computer' || flow.ask_first_supported === false) return view(true, OLDER_COMPUTER, { short: 'Older computer' });
  if (flow.ask_first_online === false) return view(Boolean(flow.asks_first), AWAY, { short: 'Computer offline' });
  if (flow.asks_because === 'account') {
    return view(true, 'Its account asks you first, so this routine does too.', { short: 'Its account asks', account: flow.lane });
  }
  if (flow.asks_first) {
    return view(true, canDecide ? 'It waits for your yes each time before it acts.' : WAITS_IN_NEEDS_YOU, { switchable: true, checked: true });
  }
  // Not a yes it can give: a member who may not decide runs a publishing routine, and that waits in Needs you.
  if (!canDecide && flow.effect === 'commit') {
    return view(true, 'It waits in Needs you for a yes each time before it publishes.', { word: WAITS, short: 'Needs you', switchable: true, checked: false });
  }
  return view(false, 'It does the whole job without waiting for anyone.', { switchable: true, checked: false });
}

/**
 * The browser worker's posts go by this same switch, so the sentence names them, but only while it
 * really works on this account. A computer that asks after a page holds them whatever the switch
 * says, and the worker always reads a page first, so the sentence never promises otherwise.
 */
function workerPosts(asks: boolean, worker: WorkerAccount | null | undefined): string {
  if (!worker?.on) return '';
  if (asks) return " The browser worker's posts wait for it too.";
  return worker.after_a_page_asks ? ' The browser worker reads a page first, so its posts still ask you.' : " The browser worker's posts go out on their own too.";
}

export function accountAsk(ask: LaneAskFirst | undefined, worker?: WorkerAccount | null): AskView | null {
  if (!ask) return null;
  if (!ask.ask_first_supported) return view(true, OLDER_COMPUTER, { short: 'Older computer' });
  if (!ask.ask_first_online) return view(ask.asks_first, AWAY, { short: 'Computer offline' });
  return ask.asks_first
    ? view(true, `Routines and agents here wait for your yes before they act.${workerPosts(true, worker)}`, { switchable: true, checked: true })
    : view(false, `Outside a routine, agents act here without waiting for you.${workerPosts(false, worker)}`, { switchable: true, checked: false });
}

/** What a routine says the moment its switch moves, before the computer has answered. */
export function flowSet(flow: Flow, on: boolean): Flow {
  const because = flow.always_asks ? 'always' : on ? 'routine' : flow.account_asks_first ? 'account' : null;
  return { ...flow, ask_first: on, asks_first: because !== null, asks_because: because };
}

/** The same when its account's switch moves. A routine's own switch still wins the reason. */
export function flowAccountSet(flow: Flow, on: boolean): Flow {
  if (flow.always_asks || flow.ask_first_supported === false || flow.effect === 'read' || flow.effect === 'draft') return { ...flow, account_asks_first: on };
  const because = flow.ask_first ? 'routine' : on ? 'account' : null;
  return { ...flow, account_asks_first: on, asks_first: because !== null, asks_because: because };
}

/**
 * The node's `why` for a person. It names ids ("a person set routine aster.gmail.newsletter on
 * aster/gmail to ask first, so a new routine ..."), which become the routine's name; anything else
 * is said as it came, as a sentence. Whatever it is, it stays text.
 */
export function whyWords(why: string | null | undefined): string | null {
  const said = why?.trim();
  if (!said) return null;
  const copied = /^a person set routine (\S+) on \S+ to ask first\b/i.exec(said);
  if (copied?.[1]) return `A person set ${routineName(copied[1])} to ask first, so this new routine on the same account started that way.`;
  return `${said.charAt(0).toUpperCase()}${said.slice(1)}${/[.!?]$/.test(said) ? '' : '.'}`;
}

export interface WhoSet {
  by: AskFirstSetBy | null | undefined;
  on: boolean;
  /** Why it started on, already in plain words (`whyWords`). */
  why?: string | null;
}

/**
 * Who set a switch and how long ago, as a sentence. An agent may turn one on and never off, so
 * that line says only a person can undo it, unless the screen says who can (`said`). `name` turns
 * a user or an agent into a name; `ago` is lowercase.
 */
export function whoSetIt({ by, on, why }: WhoSet, name: (by: AskFirstSetBy) => string, ago: string | null, said = false): string | null {
  if (!by) return why ?? null;
  const when = ago ? `, ${ago}` : '';
  const done = on ? 'turned this on' : 'turned this off';
  const because = why ? ` ${why}` : '';
  if (by.kind === 'computer') return `Set on the computer itself${when}.${because}`;
  if (by.kind === 'agent') return `${name(by)} ${done}${when}.${because}${on && !said ? ' Only a person can turn it off.' : ''}`;
  return `${name(by)} ${done}${fromWhere(by)}${when}.${because}`;
}

/** ", from the command line" when a person set it on the computer's own command line. */
const fromWhere = (by: AskFirstSetBy) => (by.via === 'cli' ? ', from the command line' : '');

/** The same in a few words, for a row. */
export function whoSetItShort({ by, on }: WhoSet, name: (by: AskFirstSetBy) => string): string | null {
  if (!by || !on) return null;
  return by.kind === 'computer' ? 'Set on the computer' : `Set by ${name(by)}${fromWhere(by)}`;
}
