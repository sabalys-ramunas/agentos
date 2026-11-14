/**
 * How a person says yes, in plain words. Levels and method
 * ids stay on the wire: a person reads "Just a tap" and "Prove it's you", never `signed_in`.
 *
 * Pure. A row is one line of the setting, a rung is how far along the ladder it sits, and the floor
 * is the lowest rung it may ever take. Everything here only decides what to show: the node holds the
 * floors, so a screen can't lower one by getting this wrong.
 */
import type { Approval, ConfirmationOverlay, ConfirmationSetting, ConfirmDay, ConfirmDayStop, ConfirmDayTick, ConfirmLevel, ConfirmMethodId, ConfirmRow, Effect } from '@/api/types';

/** Said in the step-up, because this makes a real payment easier to approve. */
export const LOWERING_A_YES = 'This changes how strong a yes has to be for everyone here, so agentos checks it is you first.';

export type Rung = 'auto' | 'chat' | 'signed_in' | 'present';

/** The ladder, weakest first. `device_key` reads as the top rung: to a person it is the same act. */
export const RUNGS: readonly Rung[] = ['auto', 'chat', 'signed_in', 'present'];

export const rungIndex = (rung: Rung) => RUNGS.indexOf(rung);

export const rungOfLevel = (level: ConfirmLevel): Exclude<Rung, 'auto'> => (level === 'device_key' ? 'present' : level);

export const RUNG_WORDS: Record<Rung, { word: string; line: string }> = {
  auto: { word: 'On its own', line: 'Nobody is asked.' },
  chat: { word: 'Just a tap', line: 'Anywhere you can answer, even in a chat.' },
  signed_in: { word: 'A tap in agentos', line: 'Here or in the Mac app. A chat does not count.' },
  present: { word: "Prove it's you", line: 'Your finger, your face or your password, just now.' },
};

/** With chat answers off, the first tap can only happen where the second one does. */
export const rungLine = (rung: Rung, chat: boolean) => (rung === 'chat' && !chat ? 'Here or in the Mac app.' : RUNG_WORDS[rung].line);

export interface RowWords {
  key: ConfirmRow;
  title: string;
  /** What it covers, in things a person recognises. */
  covers: string;
  /** Why it can go no lower, said beside the lock. */
  because?: string;
  /** The sites on this row, as marks. */
  sites: string[];
}

export const ROWS: readonly RowWords[] = [
  {
    key: 'commit',
    title: 'Posts, emails and replies',
    covers: 'What goes out for your businesses, every day.',
    sites: ['instagram', 'gmail', 'linkedin'],
  },
  {
    key: 'marked',
    title: 'What you marked Ask me first',
    covers: 'The routines and accounts you want to read before they go.',
    sites: ['instagram', 'reddit'],
  },
  {
    key: 'after_untrusted',
    title: 'After reading something from outside',
    covers: 'A review, an email or a page that nobody here wrote.',
    because: 'Words from outside can steer an agent, and the chat is where it lives. So a yes in chat never counts here.',
    sites: ['tripadvisor', 'gmail'],
  },
  {
    key: 'pay',
    title: 'Money',
    covers: 'Payments, refunds and paid ads.',
    because: "Always asks. Money sent can't be taken back from here, so only how strong the yes is can change.",
    sites: ['quickbooks', 'shopify'],
  },
  {
    key: 'irreversible',
    title: "Things that can't be undone",
    covers: 'Deleting for good, and filing a return.',
    because: "Always asks. Once it's gone it's gone, so only how strong the yes is can change.",
    sites: ['blog', 'gov'],
  },
];

export type Rungs = Record<ConfirmRow, Rung>;

export const rungsOf = (setting: ConfirmationSetting): Rungs => ({
  commit: setting.commit.when === 'auto' ? 'auto' : rungOfLevel(setting.commit.level),
  marked: rungOfLevel(setting.commit.level),
  after_untrusted: rungOfLevel(setting.after_untrusted.level),
  pay: rungOfLevel(setting.pay.level),
  irreversible: rungOfLevel(setting.irreversible.level),
});

/** The lowest rung a row may ever take, wherever it is set. These are the contract's floors. */
export const FLOORS: Record<ConfirmRow, Rung> = {
  commit: 'auto',
  marked: 'chat',
  after_untrusted: 'signed_in',
  pay: 'signed_in',
  irreversible: 'signed_in',
};

const higher = (a: Rung, b: Rung) => (rungIndex(a) >= rungIndex(b) ? a : b);

/** What a business asks for: the org's rung, raised by its own overlay. It can never come out lower. */
export function businessRungs(setting: ConfirmationSetting, overlay: ConfirmationOverlay | undefined): Rungs {
  const org = rungsOf(setting);
  if (!overlay) return org;
  const own = rungsOf({ ...setting, ...overlay });
  return {
    commit: higher(org.commit, own.commit),
    marked: higher(org.marked, own.marked),
    after_untrusted: higher(org.after_untrusted, own.after_untrusted),
    pay: higher(org.pay, own.pay),
    irreversible: higher(org.irreversible, own.irreversible),
  };
}

/** A setting with one row moved. Moving the posts row off "On its own" makes everything ask at that rung. */
export function withRung(setting: ConfirmationSetting, row: ConfirmRow, rung: Rung): ConfirmationSetting {
  const level: ConfirmLevel = rung === 'auto' ? setting.commit.level : rung;
  switch (row) {
    case 'commit':
      return { ...setting, commit: rung === 'auto' ? { ...setting.commit, when: 'auto' } : { when: 'ask', level } };
    case 'marked':
      return { ...setting, commit: { ...setting.commit, level } };
    case 'after_untrusted':
      return { ...setting, after_untrusted: { level } };
    case 'pay':
      return { ...setting, pay: { when: 'ask', level } };
    case 'irreversible':
      return { ...setting, irreversible: { when: 'ask', level } };
  }
}

/** The same move, for a business: only what it asks for beyond the org is kept. */
export function withBusinessRung(setting: ConfirmationSetting, overlay: ConfirmationOverlay | undefined, row: ConfirmRow, rung: Rung): ConfirmationOverlay {
  const moved = withRung({ ...setting, ...overlay }, row, rung);
  const org = rungsOf(setting);
  const next = rungsOf(moved);
  const kept: ConfirmationOverlay = {};
  if (rungIndex(next.commit) > rungIndex(org.commit) || rungIndex(next.marked) > rungIndex(org.marked)) kept.commit = moved.commit;
  if (rungIndex(next.after_untrusted) > rungIndex(org.after_untrusted)) kept.after_untrusted = moved.after_untrusted;
  if (rungIndex(next.pay) > rungIndex(org.pay)) kept.pay = moved.pay;
  if (rungIndex(next.irreversible) > rungIndex(org.irreversible)) kept.irreversible = moved.irreversible;
  return kept;
}

/** Which rung a stop sits on, given the rows as they are now. Marked items follow posts once posts ask. */
export function stopRung(row: ConfirmDayStop['row'], rungs: Rungs): Rung {
  if (row === 'marked') return rungs.commit === 'auto' ? rungs.marked : higher(rungs.commit, rungs.marked);
  return higher(rungs[row], row === 'after_untrusted' ? rungs.commit : 'auto');
}

export interface PlacedStop extends ConfirmDayStop {
  rung: Rung;
}

export interface DayShape {
  total: number;
  own: number;
  stopped: number;
  /** How many sit on each rung. */
  lanes: Record<Rung, number>;
  stops: PlacedStop[];
  /** What runs on its own, and what the rows now send to a person. */
  flowing: ConfirmDayTick[];
  held: ConfirmDayTick[];
  /** The rung what is held sits on. */
  heldRung: Rung | null;
}

/** The same day, read through the rows as they are now, so moving a row reshapes it at once. */
export function shapeOf(day: ConfirmDay, rungs: Rungs, business: string | null = null): DayShape {
  const mine = <T extends { business: string }>(items: T[]) => (business ? items.filter((item) => item.business === business) : items);
  const ticks = mine(day.ticks);
  const stops = mine(day.stops).map((stop) => ({ ...stop, rung: stopRung(stop.row, rungs) }));
  const held = rungs.commit === 'auto' ? [] : ticks.filter((tick) => tick.kind === 'commit');
  const flowing = rungs.commit === 'auto' ? ticks : ticks.filter((tick) => tick.kind === 'look');
  const lanes: Record<Rung, number> = { auto: flowing.length, chat: 0, signed_in: 0, present: 0 };
  for (const stop of stops) lanes[stop.rung] += 1;
  if (rungs.commit !== 'auto') lanes[rungs.commit] += held.length;
  return { total: ticks.length + stops.length, own: flowing.length, stopped: stops.length + held.length, lanes, stops, flowing, held, heldRung: rungs.commit === 'auto' ? null : rungs.commit };
}

/** The node's own check: is this yes strong enough for what it covers? */
export const reaches = (level: ConfirmLevel, requires: ConfirmLevel) => order(level) >= order(requires);
function order(level: ConfirmLevel) {
  return (['chat', 'signed_in', 'present', 'device_key'] as ConfirmLevel[]).indexOf(level);
}

/** A tap isn't enough for this one: the panel steps up before it decides. */
export const needsProof = (approval: Pick<Approval, 'requires'>) => Boolean(approval.requires) && reaches(approval.requires!, 'present');

const HEAVY_WHY: Partial<Record<Effect, string>> = {
  pay: "Money can't be taken back from here, so a tap isn't enough.",
  irreversible: "This can't be undone, so a tap isn't enough.",
};

/** Why this one takes more than a tap, in one plain sentence. */
export const whyProof = (approval: Pick<Approval, 'effect'>) => HEAVY_WHY[approval.effect] ?? 'Someone asked for more than a tap here, so this takes a little more.';

export interface MethodWords {
  /** The name of the place. */
  word: string;
  /** How a yes feels there. */
  feel: string;
  /** In a sentence, after "said yes": "in a chat", "with Touch ID". */
  said: string;
}

export const METHOD_WORDS: Record<ConfirmMethodId, MethodWords> = {
  chat: { word: 'In a chat', feel: 'Your chat app shows a yes and a no, in words agentos wrote.', said: 'in a chat' },
  panel: { word: 'Here in the panel', feel: 'A tap in any browser you are signed in to.', said: 'in the panel' },
  passkey: { word: 'Your passkey', feel: 'Your finger or face, or your device PIN, right in the browser.', said: 'with a passkey' },
  mac: { word: 'The Mac app', feel: 'A click for most things. Touch ID or your Mac password when it matters.', said: 'on a Mac' },
  cli: { word: 'The computer itself', feel: 'A command on the computer, for whoever runs it.', said: 'at the computer' },
  ios: { word: 'iPhone', feel: 'Face ID, from a notification.', said: 'on an iPhone' },
  android: { word: 'Android', feel: 'Your fingerprint, from a notification.', said: 'on an Android phone' },
  watch: { word: 'Apple Watch', feel: 'A tap on your wrist, for what can wait for a tap.', said: 'on a watch' },
  totp: { word: 'Authenticator app', feel: 'A six-digit code, for when nothing else is near.', said: 'with a code' },
};

/** The short form for a row in a list: "Ines Okafor said yes in a chat". */
export const yesShort = (who: string, method: ConfirmMethodId) => `${who} said yes ${METHOD_WORDS[method].said}`;

/** "Ines Okafor said yes in a chat in Claude Code". */
export function yesSentence(who: string, method: ConfirmMethodId, where: string | null): string {
  const said = METHOD_WORDS[method].said;
  if (method === 'chat' && where) return `${who} said yes in a chat in ${where}`;
  if (method === 'mac' && where) return `${who} said yes on ${where}`;
  if (method === 'passkey' && where) return `${who} said yes with a passkey on ${where}`;
  return `${who} said yes ${said}`;
}
