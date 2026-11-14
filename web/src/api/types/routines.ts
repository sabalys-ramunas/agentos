import type { Cost, Effect, RunStatus } from './lanes';
import type { FlowParam } from './runs';

/**
 * What a routine is made of: a trigger, then steps, then what happens with the result. A routine
 * and a workflow are the same thing, so these hang off `Flow`. The computer holds it and runs it,
 * and the cloud mirrors it (the computer's copy wins). Everything a computer
 * or a person outside wrote stays text here, and `untrusted` says so.
 */
export type RoutineFieldType = 'text' | 'number' | 'date' | 'list' | 'yes_no';

/** One value a trigger or a step produces, for the steps after it. */
export interface RoutineField {
  key: string;
  label: string;
  type: RoutineFieldType;
  /** Written by someone outside (an email, a page, a webhook): it can fill text, never decide. */
  untrusted?: boolean;
}

export type RoutineTriggerKind = 'schedule' | 'webhook' | 'email' | 'page_change' | 'site_change' | 'manual';

export interface RoutineTrigger {
  kind: RoutineTriggerKind;
  /** The words a person reads, when a template or a person chose them. */
  label?: string | null;
  cron?: string | null;
  /** The mail account or the account a page is watched from. */
  lane?: string | null;
  from?: string | null;
  subject?: string | null;
  url?: string | null;
  timezone?: string | null;
  /** A site_change: the endpoint to read, as `<endpoint>@<surface>`, what it is read with, which output
   *  holds the rows, which field says which row it is, and how often to read it. */
  operation?: string | null;
  params?: Record<string, unknown> | null;
  list?: string | null;
  key?: string | null;
  every_minutes?: number | null;
  /** Where the cloud listens. A computer never has a public address. */
  hook?: RoutineHook | null;
}

export interface RoutineHook {
  url: string;
  /** Requests are signed: the secret is shown once, and only its hint afterwards. */
  secret_hint: string | null;
  last_received_at: string | null;
}

export interface RoutineCondition {
  /** A `{{step.field}}` reference. */
  left: string | null;
  op: 'is' | 'is_not' | 'contains' | 'more_than' | 'less_than' | 'is_empty' | 'not_empty';
  right: string;
}

export interface RoutineSend {
  /** A list the owner supplied. Nothing else can be sent to. */
  list: string | null;
}

interface RoutineStepBase {
  id: string;
  /** A short phrase that fits the sentence: "check the stock". */
  label: string;
}

export interface RoutineActionStep extends RoutineStepBase {
  kind: 'action';
  /** A routine, or a connector operation, on one of the workspace's accounts. */
  operation: string | null;
  inputs: Record<string, string>;
  send?: RoutineSend | null;
}

export interface RoutineBranchStep extends RoutineStepBase {
  kind: 'branch';
  when: RoutineCondition;
  then: RoutineStep[];
  otherwise: RoutineStep[];
}

export interface RoutineWaitStep extends RoutineStepBase {
  kind: 'wait';
  amount: number;
  unit: 'minutes' | 'hours' | 'days';
}

export interface RoutineLoopStep extends RoutineStepBase {
  kind: 'loop';
  /** A `{{step.field}}` reference to a list. */
  over: string | null;
  /** What one thing in the list is called: "invoice". */
  item: string;
  body: RoutineStep[];
}

export interface RoutineAskStep extends RoutineStepBase {
  kind: 'ask';
  question: string;
}

export interface RoutineNotifyStep extends RoutineStepBase {
  kind: 'notify';
  message: string;
}

/** Stops chasing whoever answered an earlier email in this run. `after` is that email's step. */
export interface RoutineStopIfRepliedStep extends RoutineStepBase {
  kind: 'stop_if_replied';
  after: string;
}

export type RoutineStep = RoutineActionStep | RoutineBranchStep | RoutineWaitStep | RoutineLoopStep | RoutineAskStep | RoutineNotifyStep | RoutineStopIfRepliedStep;
export type RoutineStepKind = RoutineStep['kind'];

/** The part a person or an agent writes, and what the file for developers holds. */
export interface RoutineDefinition {
  name: string;
  workspace: string | null;
  trigger: RoutineTrigger;
  steps: RoutineStep[];
}

/** One step that asks whatever anyone sets, and why. */
export interface RoutineAlwaysAsks {
  step: string;
  because: 'pay' | 'irreversible' | 'ask_step' | 'install_base';
}

/** What a send step can do today, worked out by the computer. These are hard rules. */
export interface RoutineSendLimit {
  step: string;
  list: string | null;
  list_size: number;
  daily_limit: number;
  sent_today: number;
  suppressed: number;
  /** False for a step that counts against the limit without sending mail, with nothing to unsubscribe from. */
  unsubscribe: boolean;
  counted?: boolean;
}

/** Something a routine made, as it looks in the world: the email, the invoice, the post. */
export interface RoutineOutput {
  kind: 'email' | 'invoice' | 'post' | 'order' | 'form' | 'digest' | 'payment' | 'files';
  /** The subject, the invoice number, the post's first words. */
  title: string;
  /** Who it went to or who it is about. */
  who?: string | null;
  body?: string | null;
  amount?: number | null;
  currency?: string | null;
  image?: string | null;
  /** More than one went out: "3 reminders". */
  more?: string | null;
  /** Where agentos saw it afterwards. Absent: nobody checked. */
  proof?: { site: string; url?: string | null } | null;
}

export type RoutineStepStatus = RunStatus | 'skipped' | 'waiting' | 'stopped';

export interface RoutineStepRun {
  step: string;
  /** The label when it ran, so an old run still reads true after an edit. */
  label: string;
  /** What it came to, in a few words: "3 overdue". */
  short?: string | null;
  kind: RoutineStepKind | 'trigger';
  status: RoutineStepStatus;
  started_at: string | null;
  finished_at: string | null;
  input: Record<string, unknown> | null;
  output: Record<string, unknown> | null;
  /** Keys in input and output that came from outside. */
  untrusted?: string[];
  /** Why it failed or was skipped, in plain words. */
  note?: string | null;
  attempts: number;
  /** A person decides this in Needs you. */
  approval_id?: string | null;
  /** One entry per item in a loop. */
  items?: { label: string; status: RoutineStepStatus }[];
}

export interface RoutineRun {
  id: string;
  flow: string;
  /** A workflow's revision. A recorded routine's content hash. */
  version: number | string;
  status: RunStatus;
  /** What set it off, in plain words: "Order #1042 arrived". */
  cause: string;
  /** What came of it, in plain words: "3 reminders sent". */
  result: string | null;
  by: 'trigger' | 'person' | 'retry' | 'agent';
  started_at: string;
  finished_at: string | null;
  output: RoutineOutput | null;
  steps: RoutineStepRun[];
  cost: Cost;
}

/** What a run did across the routines, for the line through now on the list. */
export interface RoutineDayMark {
  flow: string;
  at: string;
  site: string;
  /** Null for what is still to come. */
  status: RunStatus | null;
}

export interface RoutineDay {
  marks: RoutineDayMark[];
  ran_today: number;
  checked_today: number;
}

export interface RoutineOperation {
  id: string;
  name: string;
  lane: string;
  workspace: string;
  effect: Effect;
  params: Record<string, FlowParam>;
  produces: RoutineField[];
  /** Sends to people, so a list, a daily limit, suppression and an unsubscribe all apply. */
  sends: boolean;
  /** What a template looks for: `mail.send`, `shop.stock`. */
  tags: string[];
}

export interface RoutineList {
  id: string;
  name: string;
  workspace: string;
  size: number;
  suppressed: number;
}

export interface RoutineTemplate {
  id: string;
  name: string;
  /** The job, as one sentence. */
  sentence: string;
  /** Who it is for, and what it does, in a few words. */
  note: string;
  icon: 'newsletter' | 'outreach' | 'invoice' | 'order' | 'webhook' | 'post' | 'new_row';
  developer?: boolean;
  trigger: RoutineTrigger;
  /** A step's `operation` may name a tag here: the panel fills in what the workspace has. */
  steps: RoutineStep[];
  /** What must be connected on this computer for it to run. A template that sends or reads mail needs a mailbox. */
  needs?: 'mailbox'[];
}

export interface RoutineCatalog {
  /** How a routine can start on this computer. A cloud leaves it out and offers every way. */
  triggers?: RoutineTriggerKind[];
  operations: RoutineOperation[];
  lists: RoutineList[];
  templates: RoutineTemplate[];
  /** What each trigger hands the steps after it. */
  trigger_fields: Record<RoutineTriggerKind, RoutineField[]>;
}
