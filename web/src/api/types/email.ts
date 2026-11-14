import type { RoutineSendLimit, RoutineStep, RoutineStepRun, RoutineTrigger } from './routines';

/*
 * Email, at concept stage: answered by the mock only. A password has
 * no field anywhere in these types, on purpose: the hosted panel never holds one.
 */
/** `unchecked` is a mailbox the node has not read yet: nothing is said about it until it has. */
export type MailboxHealth = 'good' | 'unchecked' | 'cannot_send' | 'cannot_read' | 'needs_password';

export interface MailProblem {
  since: string;
  title: string;
  body: string;
  /** The one next step. */
  next: string;
}

export interface MailArrival {
  at: string;
  kind: 'reply' | 'unsubscribe' | 'mail';
}

export interface MailboxDay {
  at: string;
  sent: number;
  replies: number;
  unsubscribes: number;
}

/**
 * A mailbox as the node knows it. Null is an honest "not known here yet": the node does not keep
 * arrivals, a week of replies, or a plan for today, so those come back null until it does.
 */
export interface Mailbox {
  id: string;
  address: string;
  /** A domain, which is also what the mark is taken from. */
  site: string;
  provider: string | null;
  workspace: string;
  computer: string;
  health: MailboxHealth;
  problem: MailProblem | null;
  checked_at: string | null;
  new_count: number | null;
  arrivals: MailArrival[] | null;
  sent_today: number;
  /** What routines will still send today. */
  planned_today: number | null;
  daily_limit: number;
  week: MailboxDay[] | null;
  routines: { id: string; name: string }[] | null;
  incoming: string;
  outgoing: string;
}

export type MailDiscoveryResult = 'found' | 'needs_a_look' | 'not_found' | 'later';

export interface MailComputer {
  id: string;
  word: string;
  /** A server has no screen to open a form on, so its door is the terminal. */
  has_screen: boolean;
}

export interface MailDiscovery {
  address: string;
  domain: string;
  result: MailDiscoveryResult;
  provider: string | null;
  site: string | null;
  /** Gmail and Workspace ask for an app password. Others take the normal one. */
  password: 'app_password' | 'normal' | null;
  incoming: string | null;
  outgoing: string | null;
  /** Where the names came from, in plain words. */
  source: string | null;
  /** The part of a low-trust host that is not the address's own name. */
  odd_part?: string | null;
  computers: MailComputer[];
}

export type MailBlock = { kind: 'text'; text: string } | { kind: 'image'; host: string } | { kind: 'link'; text: string; url: string };

export interface MailDid {
  at: string;
  words: string;
  /** A routine that acted, with its page when it has one. */
  routine?: { id: string; name: string } | null;
  /** Something now waits for a person. */
  waits?: boolean;
}

/** A message in a list has its clip and no body: one message, read in full, has its body. */
export interface MailMessage {
  id: string;
  mailbox: string;
  kind: 'reply' | 'unsubscribe' | 'automatic' | 'mail';
  from_name: string;
  from_address: string;
  subject: string;
  clip: string;
  at: string;
  unread: boolean;
  body: MailBlock[];
  /** Named, not opened. The node gives the type, not the size. */
  attachments: { name: string; type?: string | null; size?: number }[];
  /** What a routine did with it. The node does not keep that per message yet: null. */
  did: MailDid[] | null;
  list?: string | null;
}

/** A link to the node's own form for one mailbox. It opens on the computer, and holds no field. */
export interface MailFormLink {
  url: string;
  expires_at: string;
  computer: string;
  /** The token the url carries, for asking whether the form is still open and for cancelling it. Nothing else reads it. */
  token: string;
}

/** A server a person typed. Its port and security may be left out, to keep the ones found with it. */
export interface MailServerFields {
  host: string;
  port?: number;
  security?: 'tls' | 'starttls';
}

/** The link to the node's form. `imap` and `smtp` are only the names a person typed, to be checked there. */
export interface MailFormLinkBody {
  address: string;
  id: string;
  imap?: MailServerFields;
  smtp?: MailServerFields;
}

/** Whether a link to the node's form is still open. An ended link and one that never was look the same. */
export interface MailFormState {
  state: 'open' | 'ended';
  expires_at?: string | null;
}

export type LeadLook = 'personal_address' | 'other_name' | 'shared_inbox';

export interface Lead {
  id: string;
  name: string;
  /** The node does not keep a role or a country for a lead: null until it does. */
  role: string | null;
  company: string;
  address: string;
  country: string | null;
  source: 'linkedin' | 'directory';
  source_word: string;
  source_url: string | null;
  found_at: string;
  /** Why the agent picked them, in its words. Shown as text. */
  why: string;
  look: LeadLook | null;
}

export interface SuppressedLead {
  id: string;
  name: string;
  company: string;
  since: string;
  why: 'unsubscribed' | 'asked' | 'bounced';
}

export interface LeadBatch {
  id: string;
  list: string;
  list_name: string;
  workspace: string;
  /** Null: a list does not say which mailbox sends it on the node. */
  mailbox: string | null;
  by: string;
  at: string;
  found: number;
  sources: { key: Lead['source']; word: string; note: string }[];
  leads: Lead[];
  /** Who was left out for the do-not-write list. The node counts them and does not name them here: null. */
  suppressed: SuppressedLead[] | null;
  status: 'waiting' | 'taken' | 'dropped';
}

export interface ListPerson {
  id: string;
  name: string;
  company: string;
  address: string;
  source_word: string;
  /** Where they are in a send. The node does not keep that per person on a list yet: null. */
  state: 'planned' | 'sent' | 'replied' | 'followed' | 'unsubscribed' | null;
}

export interface ListDay {
  at: string;
  first: number;
  follow: number;
  replies: number;
  unsubscribes: number;
}

/**
 * A list as the node keeps it. Its size is known; the people are read in person. What a list does
 * on a mailbox (its pace, its days, its letter) is not kept by the node yet, so those are null.
 */
export interface MailList {
  id: string;
  name: string;
  workspace: string;
  size: number;
  mailbox: string | null;
  address: string | null;
  mailbox_limit: number | null;
  /** What the mailbox sends on a normal day for other reasons. */
  other_per_day: number | null;
  state: 'ready' | 'sending' | 'paused' | null;
  pace: number | null;
  /** Null on a list in the lists page: the people come with one list, read in person. */
  people: ListPerson[] | null;
  days: ListDay[] | null;
  /** Who is on the do-not-write list, by name. The node counts them and does not name them here: null. */
  suppressed: SuppressedLead[] | null;
  /** The owner has not made a list for this business before, so the note is due. */
  first_list: boolean;
  countries: string[] | null;
  routine: { id: string; name: string } | null;
  letter: { subject: string; body: string; sender: string; postal: string; source_line: string } | null;
  batch_waiting: string | null;
}

export interface ChasePerson {
  id: string;
  name: string;
  company: string;
  first_at: string;
  follow_at: string | null;
  reply_at: string | null;
  unsubscribe_at: string | null;
}

/** A routine that sends mail. The parts the node does not keep yet (run history, who it chases, a follow-up) are null. */
export interface MailRoutine {
  id: string;
  name: string;
  workspace: string;
  mailbox: string | null;
  address: string | null;
  list: { id: string; name: string } | null;
  trigger: RoutineTrigger;
  steps: RoutineStep[];
  /** What each step came to, this week. */
  run: RoutineStepRun[] | null;
  people: ChasePerson[] | null;
  next_at: string | null;
  limit: RoutineSendLimit | null;
  follow_up: { subject: string; body: string } | null;
}
