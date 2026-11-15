/**
 * The everyday words. One place holds what a person reads and the technical term behind it, as
 * decided with the design of the app (web/DESIGN.md).
 *
 * Rules:
 * - Screens read words from here (or from `labels.ts`, which reads from here). A new screen never
 *   invents its own word for a thing that has one.
 * - The technical term is what the API, the MCP tools and Technical details say. It may appear
 *   only behind the Technical details switch, in mono ids, and in agent-facing text.
 */
import type { Effect, LaneState, NodeState, RunStatus, Signal } from '@/api/types';

export interface Term {
  /** What a person reads. */
  word: string;
  /** One of them, where it differs. */
  singular?: string;
  /** The engine's name for it, shown only with Technical details on. */
  technical: string;
}

/** The places in the product. The rail, the command bar and the page titles use these. */
export const PLACES = {
  home: { word: 'Home', technical: 'Home' },
  environments: { word: 'Environments', singular: 'Environment', technical: 'Environments (cloud VMs)' },
  runs: { word: 'Runs', singular: 'Run', technical: 'Jobs (agentos run)' },
  checks: { word: 'Checks', singular: 'Check', technical: 'Studio runs' },
  machines: { word: 'Machines', singular: 'Machine', technical: 'Nodes' },
  brand: { word: 'Brand', technical: 'Product profiles' },
  accounts: { word: 'Accounts', singular: 'Account', technical: 'Lanes' },
  routines: { word: 'Routines', singular: 'Routine', technical: 'Flows' },
  email: { word: 'Email', singular: 'Mailbox', technical: 'Mail' },
  siteApis: { word: 'Site APIs', singular: 'Site API', technical: 'Site APIs' },
  needsYou: { word: 'Needs you', technical: 'Approvals and questions' },
  history: { word: 'History', technical: 'Activity' },
  settings: { word: 'Settings', technical: 'Settings' },
  /**
   * The old name of Machines, kept as the same word so a screen that still reads it says Machines.
   * New code reads `machines`; this goes when nothing uses it.
   */
  computers: { word: 'Machines', singular: 'Machine', technical: 'Nodes' },
} as const satisfies Record<string, Term>;

/**
 * The rail's groups, in order: what is being worked on, where it runs, who it is for, what runs by
 * itself, and what is yours. The owner approved this structure (Mono, 2026-11-13).
 */
export const PLACE_GROUPS = {
  work: { word: 'Work', technical: 'Work' },
  where: { word: 'Where it runs', technical: 'Machines' },
  who: { word: 'Who it is for', technical: 'Brand and accounts' },
  automations: { word: 'Automations', technical: 'Routines, mail and Site APIs' },
  you: { word: 'You', technical: 'Approvals, history and settings' },
} as const satisfies Record<string, Term>;

/** Things, as nouns in sentences. */
export const THINGS = {
  lane: { word: 'accounts', singular: 'account', technical: 'lane' },
  tool: { word: 'tools', singular: 'tool', technical: 'lane (shell, service)' },
  workspace: { word: 'workspaces', singular: 'workspace', technical: 'workspace' },
  org: { word: 'organization', technical: 'org' },
  node: { word: 'machines', singular: 'machine', technical: 'node' },
  flow: { word: 'routines', singular: 'routine', technical: 'flow' },
  run: { word: 'things done', singular: 'thing done', technical: 'run' },
  event: { word: 'things done', singular: 'thing done', technical: 'event' },
  principal: { word: 'who did it', technical: 'principal' },
  brain: { word: 'AI models', singular: 'AI model', technical: 'brain' },
  provider: { word: 'AI services', singular: 'AI service', technical: 'provider' },
  plugin: { word: 'Workers', singular: 'Worker', technical: 'plugin' },
  workerStep: { word: 'steps', singular: 'step', technical: 'action' },
  spend: { word: 'AI usage', technical: 'model spend' },
  tokens: { word: '', technical: 'tokens' },
  grant: { word: 'Agent access', technical: 'grant' },
  vault: { word: 'Saved logins', technical: 'vault' },
  webhooks: { word: 'Webhooks', technical: 'webhooks' },
  audit: { word: 'Who changed what', technical: 'audit log' },
  retention: { word: 'How long history is kept', technical: 'retention' },
  dangerZone: { word: 'Delete this organization', technical: 'danger zone' },
  devices: { word: 'Phones and browsers', technical: 'devices' },
  /** A browser with its own fingerprint and connection, shared by a company's accounts. Not a phone. */
  browser: { word: 'Browsers', singular: 'browser', technical: 'browser device' },
  /** How a browser reaches the internet: a proxy, its address behind Technical details. */
  connection: { word: 'Connections', singular: 'connection', technical: 'proxy' },
  standing: { word: 'Always allowed', technical: 'standing approvals' },
  contentHash: { word: 'Content ID', technical: 'content hash' },
  inspect: { word: 'Technical details', technical: 'Inspect' },
  newLane: { word: 'Add an account', technical: 'New lane' },
  takeOver: { word: 'Take over', technical: 'take over' },
  handBack: { word: 'Hand back', technical: 'hand back' },
} as const satisfies Record<string, Term>;

/**
 * The jobs a worker hands to AI models, in the order a step goes through them. "Job" only ever
 * means one of these: a piece of work a worker finishes is a thing done, as everywhere else.
 * "Ladder" means the rungs of a yes (`lib/confirm.ts`), so it is not a word for asking for help.
 */
export const WORKER_JOBS = {
  decide: { word: 'Decides each step', technical: 'decide' },
  write: { word: 'Writes text', technical: 'write' },
  rescue: { word: 'Helps when it is unsure', technical: 'rescue' },
} as const satisfies Record<string, Term>;

/** The short name of each job, where a row label has little room. `you` is the person, last of all. */
export const WORKER_ROWS = {
  decide: { word: 'Decides', technical: 'decide' },
  write: { word: 'Writes', technical: 'write' },
  rescue: { word: 'Helps', technical: 'rescue' },
  you: { word: 'You', technical: 'escalation to a person' },
} as const satisfies Record<string, Term>;

/** Asked once, at the moment a person turns a worker on: only a person can. */
export const TURNING_ON_WORKER = 'A worker acts on the accounts on this machine, so agentos checks it is you first. An agent can never do this for you.';
/** Clearing taint lets an agent's next post go without asking, so it is the owner's, asked in person. */
export const CLEARING_TAINT = 'An agent that read outside text would stop asking before its next post, so agentos checks it is you first.';
/** The same for one account the worker was taken off. */
export const TURNING_ON_ACCOUNT_WORKER = 'The worker will act on this account again, so agentos checks it is you first. An agent can never do this for you.';

/** Asked once, before a freeze is lifted. Lifting one restores nothing, so the words say so. */
/** Said in the step-up dialog before a repository token goes to the node. */
export const GIVING_A_TOKEN = 'A token opens your code to this computer, so agentos checks it is you first.';
export const LIFTING_A_FREEZE = 'Lifting the freeze lets agents start again, and agentos checks it is you first. Nothing comes back by itself: grants stay revoked and paused schedules stay paused.';

/** What a freeze is, in the words a person reads. Its technical name is the break-glass freeze. */
export const FREEZE_WORDS = {
  name: { word: 'Freeze', technical: 'freeze (break glass)' },
  frozen: { word: 'Frozen', technical: 'frozen' },
  unfreeze: { word: 'Unfreeze', technical: 'unfreeze' },
} as const satisfies Record<string, Term>;

/** A run's outcome. Honest: "checked" means agentos saw it on the site afterwards. */
export const RUN_WORDS: Record<RunStatus, Term> = {
  queued: { word: 'Queued', technical: 'queued' },
  running: { word: 'Working now', technical: 'running' },
  waiting_approval: { word: 'Waiting for you', technical: 'waiting_approval' },
  succeeded_verified: { word: 'Checked', technical: 'verified' },
  succeeded_unverified: { word: 'Not checked', technical: 'unverified' },
  partial: { word: 'Partly done', technical: 'partial' },
  failed: { word: 'Did not work', technical: 'failed' },
  cancelled: { word: 'Stopped', technical: 'cancelled' },
};

/** An account's state, in at most 18 characters so it fits a list. Sleeping is the word Accounts uses for it too (lib/states.ts). */
export const LANE_STATE_WORDS: Record<LaneState, Term> = {
  cold: { word: 'Sleeping', technical: 'cold' },
  warming: { word: 'Waking up', technical: 'warming' },
  warm: { word: 'Ready', technical: 'warm' },
  active: { word: 'Working now', technical: 'active' },
  quarantined: { word: 'Locked for safety', technical: 'quarantined' },
};

/** What the site is saying about the account. */
export const SIGNAL_WORDS: Record<Signal, Term> = {
  ok: { word: 'Fine', technical: 'ok' },
  captcha: { word: 'Robot check', technical: 'captcha' },
  checkpoint: { word: "Confirm it's you", technical: 'checkpoint' },
  rate_limited: { word: 'Slowed down', technical: 'rate limited' },
  blocked: { word: 'Blocked by site', technical: 'blocked' },
  logged_out: { word: 'Signed out', technical: 'logged out' },
  unknown: { word: 'Not known yet', technical: 'unknown' },
};

export const NODE_STATE_WORDS: Record<NodeState, Term> = {
  up: { word: 'Online', technical: 'up' },
  degraded: { word: 'Struggling', technical: 'degraded' },
  down: { word: 'Offline', technical: 'down' },
};

/** What an action does to the world, as a group name and in a sentence. */
export const EFFECT_WORDS: Record<Effect, Term & { group: string }> = {
  read: { word: 'Look only', group: 'Looking', technical: 'read' },
  draft: { word: 'Draft (not published)', group: 'Drafts', technical: 'draft' },
  commit: { word: 'Goes out', group: 'Posts to check', technical: 'commit' },
  pay: { word: 'Costs money', group: 'Costs money', technical: 'pay' },
  irreversible: { word: 'Cannot be undone', group: "Can't be undone", technical: 'irreversible' },
  secret: { word: 'Uses a saved login', group: 'Saved logins', technical: 'secret' },
  exec: { word: 'Runs a command', group: 'For a technical person', technical: 'exec' },
  install_overlay: { word: 'Installs on an account', group: 'For a technical person', technical: 'install_overlay' },
  install_base: { word: 'Installs on every account', group: 'For a technical person', technical: 'install_base' },
  service: { word: 'Runs a service', group: 'For a technical person', technical: 'service' },
  brain: { word: 'AI usage', group: 'AI usage', technical: 'brain' },
  agent: { word: 'Talks to an agent', group: 'For a technical person', technical: 'agent' },
  vm: { word: 'Rents a cloud machine', group: 'Costs money', technical: 'vm' },
};

export interface ActionVerbs {
  /** The yes button: the action itself, never "Approve". */
  yes: string;
  /** The no button. */
  no: string;
  /** What "no" means, said after it. */
  noMeans: string;
}

/**
 * The verbs on a decision. `amount` is a ready-made money string ("$40"), `count` and `noun` how
 * many of what it touches ("Delete 3 pages"); all are optional and only change the words when given.
 */
export function actionVerbs(effect: Effect, details: { amount?: string; count?: number; noun?: string } = {}): ActionVerbs {
  const many = details.count ? ` ${details.count}${details.noun ? ` ${details.noun}` : ''}` : '';
  switch (effect) {
    case 'commit':
      return { yes: 'Post it', no: "Don't post", noMeans: 'Nothing is posted.' };
    case 'pay':
      return { yes: details.amount ? `Pay ${details.amount}` : 'Pay', no: "Don't pay", noMeans: 'Nothing is paid.' };
    case 'irreversible':
      return { yes: details.count ? `Delete${many}` : 'Do it', no: details.count ? 'Keep them' : "Don't do it", noMeans: 'Nothing changes.' };
    case 'secret':
      return { yes: 'Use the login', no: "Don't use it", noMeans: 'The login stays unused.' };
    case 'draft':
      return { yes: 'Save the draft', no: "Don't save", noMeans: 'Nothing is saved.' };
    case 'read':
      return { yes: 'Let it look', no: "Don't look", noMeans: 'Nothing is opened.' };
    case 'exec':
      return { yes: 'Run it', no: "Don't run", noMeans: 'Nothing runs.' };
    case 'install_overlay':
    case 'install_base':
      return { yes: 'Install it', no: "Don't install", noMeans: 'Nothing is installed.' };
    case 'service':
      return { yes: 'Start it', no: "Don't start", noMeans: 'Nothing starts.' };
    case 'brain':
      return { yes: 'Allow it', no: "Don't allow", noMeans: 'Nothing is spent.' };
    case 'agent':
      return { yes: 'Allow it', no: "Don't allow", noMeans: 'Nothing is sent to the agent.' };
    case 'vm':
      return { yes: 'Rent it', no: "Don't rent", noMeans: 'No machine is rented.' };
  }
}

/**
 * The whole table from structure.md section 4, for the record and for a glossary screen later:
 * the word people read today in the old panel, the plain word, and where the old word may stay.
 */
export const GLOSSARY: { today: string; plain: string; techOk?: string }[] = [
  { today: 'Lane', plain: 'Account (a website login), Tool (shell, service)', techOk: 'Mono id aster/instagram, Inspect drawer' },
  { today: 'Lane name in headers aster/instagram', plain: "Aster Coffee · Instagram; in sentences Aster Coffee's Instagram", techOk: 'Technical details row' },
  { today: 'Workspace', plain: 'Workspace', techOk: 'everywhere' },
  { today: 'Org', plain: 'Organization (sentences), Org (tight labels only)' },
  { today: 'Node', plain: 'Machine; display names Mac mini (home), Cloud server with id in mono', techOk: 'Machine page header, map' },
  { today: 'Flow, aster.instagram.post', plain: 'Routine, Weekly Instagram post', techOk: 'Routine page, Technical details' },
  { today: 'Schedule, cron', plain: 'Every Friday at 09:00, When a new comment arrives', techOk: 'Advanced field' },
  { today: 'Run', plain: 'The sentence itself: Posted a photo on...', techOk: 'Run id under Technical details' },
  { today: 'Event, Events, last 24 hours', plain: 'things done', techOk: 'Machine logs' },
  { today: 'Principal, Started by claude-code', plain: 'Who did it: Claude Code, Claude, Codex, ChatGPT, or the routine', techOk: 'id in Technical details' },
  { today: 'Brain, claude-max', plain: 'AI model; Claude (Max plan), Local AI on the Mac mini', techOk: 'Settings, AI models' },
  { today: 'Provider', plain: 'AI service: TypeSafe, OpenRouter, Claude (Max plan), Local AI on the Mac mini', techOk: 'Technical details, the agentos brains command' },
  { today: 'Plugin', plain: 'Worker: Browser worker, Android worker, Desktop worker', techOk: 'Technical details' },
  { today: 'Action, pilot step', plain: 'Step', techOk: 'Technical details' },
  { today: 'Decide, write and rescue roles', plain: 'Decides each step, Writes text, Helps when it is unsure', techOk: 'Technical details' },
  { today: 'Escalation', plain: 'It asks for help, then it stops and asks you', techOk: 'Technical details' },
  { today: 'Model spend, Cost', plain: 'AI usage, $1.87 today (owner and admin only)', techOk: 'per-run cost with Technical details on' },
  { today: 'Tokens', plain: '(hidden)', techOk: 'Technical details' },
  { today: 'Grant', plain: 'Agent access: Which AI apps can use agentos', techOk: 'token screens' },
  { today: 'Vault', plain: 'Saved logins' },
  { today: 'Webhooks', plain: 'Webhooks (System only)' },
  { today: 'Audit log', plain: 'Who changed what' },
  { today: 'Retention', plain: 'How long history is kept' },
  { today: 'Danger zone', plain: 'Delete this organization' },
  { today: 'Devices', plain: 'Phones and browsers' },
  { today: 'Standing approvals', plain: 'Always allowed (rules)' },
  { today: 'Content hash', plain: 'Content ID, Technical details only' },
  { today: 'Running', plain: 'Working now (pink)' },
  { today: 'Waiting for you', plain: 'Waiting for you' },
  { today: 'Verified', plain: 'Checked; Live, checked when it is public' },
  { today: 'Unverified', plain: 'Not checked, plus It ran, but we could not see it on Facebook afterwards.' },
  { today: 'Partial', plain: 'Partly done' },
  { today: 'Failed', plain: 'Did not work, plus what did not happen: Nothing was posted.' },
  { today: 'Cancelled', plain: 'Stopped' },
  { today: 'Cold', plain: 'Sleeping', techOk: 'Inspect' },
  { today: 'Warming', plain: 'Waking up' },
  { today: 'Warm', plain: 'Ready' },
  { today: 'Active', plain: 'Working now (pink)' },
  { today: 'Quarantined', plain: 'Locked for safety' },
  { today: 'OK', plain: 'Fine' },
  { today: 'Captcha', plain: 'Needs a quick human check' },
  { today: 'Checkpoint', plain: 'Wants to confirm it is you' },
  { today: 'Rate limited', plain: 'Slowed down by the site (resumes by itself)' },
  { today: 'Blocked', plain: 'Blocked by the site' },
  { today: 'Logged out', plain: 'Signed out' },
  { today: 'Wrong account', plain: 'Signed in to the wrong account' },
  { today: 'Person in control', plain: 'You have control / Dana has control' },
  { today: 'Agent stopped', plain: 'Agent paused' },
  { today: 'Workspace health', plain: 'A sentence, not a word: All 6 accounts fine', techOk: 'dots behind Technical details' },
  { today: 'Read', plain: 'Look only' },
  { today: 'Draft', plain: 'Draft (not published)' },
  { today: 'Publish (commit)', plain: 'Goes out' },
  { today: 'Payment', plain: 'Costs money' },
  { today: 'Irreversible', plain: 'Cannot be undone' },
  { today: 'Login (secret)', plain: 'Uses a saved login' },
  { today: 'Run command, Install in lane, Install on base image, Run service', plain: 'For a technical person group, assigned to admins', techOk: 'full labels in System' },
  { today: 'Machine state Up, Degraded, Down', plain: 'Online, Struggling, Offline' },
  { today: 'Deny / Approve', plain: "The verb of the action: Post it / Don't post, Pay $40 / Don't pay, Delete 3 pages / Keep them", techOk: 'keyboard hints' },
  { today: 'New lane', plain: 'Add an account' },
];
