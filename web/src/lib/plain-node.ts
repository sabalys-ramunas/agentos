/**
 * What a computer says in its own words, put into a person's. The node's sentences carry raw
 * reasons (`needs_human:captcha`), UTC times, commands and ids. These rules turn them into what
 * happened and what to do. The raw text stays one switch away, under Technical details, so an
 * expert still has the command, the id or the code.
 *
 * This is the one place for those words. A screen that shows node text reads it from here, and a
 * new raw sentence gets a rule here with a test, never a sentence of its own on the screen.
 */
import { date, dayAndTime } from './format';
import { laneName } from './labels';

/** The raw hold reason after `needs_human:`, in words a person reads. */
const HOLD_WORDS: Record<string, string> = {
  captcha: 'a robot check',
  checkpoint: 'a check that it is you',
  wrong_account: 'the wrong account signed in',
  logged_out: 'it signed out',
  blocked: 'the site blocked it',
  rate_limited: 'the site slowed it down',
};

/** An account's id to its name in a person's words, such as "Biz A's Social". Built from the workspaces. */
export type PlaceNames = ReadonlyMap<string, string>;

/** "today 16:21" in the person's own time, as the rest of the app says it, lower case mid-sentence. */
export function localWhen(iso: string): string {
  return dayAndTime(iso).replace(/^(Today|Tomorrow)\b/, (word) => word.toLowerCase());
}

/** "Held for a human since 2026-11-07 15:21 UTC: needs_human:captcha." */
const HELD = /Held for a human(?: since (\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}) UTC)?: needs_human:([a-z_]+)\.?/g;
/** A raw hold reason on its own, as the node writes it. */
const HOLD_TOKEN = /needs_human:([a-z_]+)/g;
/** The node's own stamp, `2026-11-07 15:21 UTC`, in any sentence that carries one. */
const UTC_STAMP = /(\d{4}-\d{2}-\d{2}) (\d{2}:\d{2}) UTC/g;
/** An account's id as the node writes it: `biz-a/shop`. */
const LANE_ID = /\b([a-z0-9][a-z0-9-]*\/[a-z0-9][a-z0-9-]*)\b/g;

function holdWord(reason: string): string {
  return HOLD_WORDS[reason] ?? 'it needs a person';
}

/** Command fragments a node sentence carries, and the words that say the same thing without them. */
const COMMAND_WORDS: [RegExp, string][] = [
  [/\(agentos migrate run stamps them\)/, '(a migration updates them)'],
  [/not stamped yet/, 'need updating'],
  [/; check each one with: agentos lane check --all/, ', and check each one on the computer'],
  [/`agentos lane keepalive --node on` turns it on/, 'Turn it on from the computer'],
  [/nodes run on Linux/, 'agentos runs on Linux'],
  [/no node\.toml in \S+/, 'The settings file is missing'],
  [/bubblewrap is missing \(terminal and service lanes isolate programs with it\)/, 'bubblewrap is missing. It keeps each environment\'s programs apart.'],
  [/no lane is visited unattended, so a lane left idle/, 'no account is visited unattended, so an account left idle'],
  [/(\d+) lane\(s\)/, '$1 accounts'],
  [/no backup in \S+; /, 'No backup is kept yet; '],
  [/ under (?:home|\/\S+)$/, ' on this computer'],
  [/MCP answers on (\S+) with the node\.toml token; requests without it get 401/, 'Agents reach this computer at $1, and only with its key'],
  [/lanes exit from the node's IP/, 'accounts connect from this computer'],
  [/^unsealed/, 'Unlocked'],
  [/so the lane has no logins/, 'so the account has no logins'],
  [/\[address\]:\d+/, "this computer's address"],
];

/**
 * A node sentence about accounts, computers and holds, in words. Raw reasons, UTC times and command
 * fragments become what they mean, and account ids become names when `names` knows them. Anything
 * else is left as the node wrote it.
 */
export function plainDetail(text: string, names?: PlaceNames): string {
  let out = text.replace(HELD, (_match, day: string | undefined, time: string | undefined, reason: string) => {
    const when = day && time ? ` since ${localWhen(`${day}T${time}:00Z`)}` : '';
    return `Held for a person${when}, for ${holdWord(reason)}.`;
  });
  out = out.replace(HOLD_TOKEN, (_match, reason: string) => holdWord(reason));
  // "7 problem(s): home is 755 (want 700); ..." is folders that others can read, listed by path.
  out = out.replace(/^(\d+) problem\(s\): .*\(want 700\).*$/, (_match, count: string) => `${count} folders can be read by other users, so they should be private.`);
  out = out.replace(UTC_STAMP, (_match, day: string, time: string) => localWhen(`${day}T${time}:00Z`));
  for (const [pattern, words] of COMMAND_WORDS) out = out.replace(pattern, words);
  if (names) {
    // "Lane biz-a/shop" reads as the account's own name, without the word lane.
    out = out.replace(/\bLane ([a-z0-9][a-z0-9-]*\/[a-z0-9][a-z0-9-]*)/g, (match, id: string) => names.get(id) ?? match);
    out = out.replace(LANE_ID, (id) => names.get(id) ?? id);
  }
  return out;
}

/** Each account's id in the node's sentences reads as its name: "biz-a/social" is "Biz A's Social". */
export function placeNames(
  workspaces: { name: string; lanes: { id: string; name: string }[] }[] | undefined,
  environments?: { title: string; lanes: { terminal: string; browser: string } }[],
): PlaceNames {
  const accounts = (workspaces ?? []).flatMap((workspace) => workspace.lanes.map((lane) => [lane.id, `${workspace.name}'s ${laneName(lane.name)}`] as const));
  // An environment's lanes read as the environment's title, the one name a person knows it by.
  const envs = (environments ?? []).flatMap((env) => [[env.lanes.terminal, env.title] as const, [env.lanes.browser, `the browser of ${env.title}`] as const]);
  return new Map([...accounts, ...envs]);
}

/** An account's name from its id: the workspace's name for it when known, else the account's own. */
export function placeName(id: string, names?: PlaceNames): string {
  const known = names?.get(id);
  if (known) return known;
  // An environment's lanes carry its machine id, which is no name: say what it is instead.
  if (id.startsWith('env/')) return id.endsWith('-browser') ? "an environment's browser" : 'an environment';
  return laneName(id.split('/')[1] ?? id);
}

/**
 * A raw reason an error or a result carries, in words: a refusal, a push that did not arrive, a
 * passphrase that did not open a file. Text no rule knows goes through plainDetail as it is.
 */
const REASONS: [RegExp, string][] = [
  [/^ntfy unreachable/i, 'The ntfy service could not be reached from this computer. Try again in a moment.'],
  [/^webhook unreachable/i, 'The webhook address could not be reached from this computer.'],
  [/^ntfy is not configured/i, 'Phone alerts are not set up yet.'],
  [/^webhook is not configured/i, 'The webhook is not set up yet.'],
  [/^ntfy answered (\d{3})/i, 'The ntfy service answered with an error ($1).'],
  [/wrong passphrase/i, 'That passphrase did not open the file. Check it, or the file may be damaged.'],
  [/is (?:warm|warming|active); close it first/i, 'This account is open right now. Close it first, then try again.'],
  // Signed out: the node says how to sign in with its command. A person is told what to do, never the command.
  [/^Sign in (?:to the panel )?first\. Run .*agentos panel login/, 'Sign in again with a new code from this computer.'],
  [/only the owner \(or an admin grant\) may write owner-only entries/, 'Only the owner can change this.'],
  [/domain must be a lowercase registrable domain/, 'Type the site by its plain address, such as instagram.com.'],
  [/^the Android engine does not exist yet, so this worker cannot run on any node/, 'Android accounts are not supported yet, so this worker cannot run on any computer'],
  [/^the desktop worker is planned for later: no desktop engine exists yet$/, 'Desktop apps are not supported yet, so this worker is not available here'],
  [/^Browser accounts are built by agentos itself\. Other browser engines and fonts are not here yet\.$/, 'Browser accounts are built by agentos itself. Other browsers and fonts are not here yet.'],
  [/ConnectError|ConnectTimeout/, 'The service could not be reached from this computer.'],
];

export function plainReason(text: string, names?: PlaceNames): string {
  for (const [pattern, sentence] of REASONS) {
    const hit = pattern.exec(text);
    if (hit) return sentence.replace('$1', hit[1] ?? '');
  }
  return plainDetail(text, names);
}

/** Pattern, and the plain sentence for what to do. The raw fix text stays under Technical details. */
const FIXES: [RegExp, string][] = [
  [/^chmod /, 'Make the data folders private, so only this account can read them.'],
  [/agentos node init/, 'Set this computer up first. Its settings file is missing.'],
  [/apt-get install/, 'Install the system programs this computer is missing, as its administrator.'],
  [/agentos backup create/, 'Keep a backup on another disk, so one dead disk cannot take every login.'],
  [/agentos lane check/, 'Check each account, and fix what each one reports.'],
  [/agentos migrate run/, 'Update the account folders to the current format.'],
  [/Ubuntu 24\.04/, 'Run agentos on a computer with Ubuntu 24.04. This one runs another system.'],
  [/agentos identity init/, 'Give the account its identity on this computer. The key is made there and never leaves it.'],
  [/^Restore its profile folder from a backup, or set the account up again\.?$/, 'Restore its profile from a backup kept on this computer, or set the account up again.'],
  [/^restore \S+ from a backup/, 'Restore it from a backup kept on this computer.'],
];

const FALLBACK_FIX = 'Fix it on the computer. Technical details has the exact steps.';

/** A plain sentence the node wrote before its command: "Free some disk, or keep the backups elsewhere: agentos ..." */
const LEAD = /^(.+?): (?=agentos |sudo |chmod |AGENTOS_HOME=|run |apt-get )/;

/** What to do about a check, in a sentence, and the raw text it came from. */
export function fixWords(fix: string): { plain: string; command: string } {
  const lead = LEAD.exec(fix)?.[1];
  if (lead) return { plain: `${lead.charAt(0).toUpperCase()}${lead.slice(1)}.`, command: fix };
  const hit = FIXES.find(([pattern]) => pattern.test(fix));
  return { plain: hit ? hit[1] : FALLBACK_FIX, command: fix };
}

/** The agent's name in an access line: `claude-code@host` reads as Claude Code. */
const AGENTS: Record<string, string> = { 'claude-code': 'Claude Code', claude: 'Claude', codex: 'Codex', chatgpt: 'ChatGPT' };

/**
 * A health line about an agent's access: "agents use grant gr_... (claude-code@host, up to
 * irreversible), until 2027-02-04". The grant id is for Technical details only.
 */
export function accessDetail(text: string, names?: PlaceNames): string {
  const grant = /^agents use grant (\S+) \(([^,)]+), up to ([a-z_]+)\), until (\d{4}-\d{2}-\d{2})$/.exec(text);
  if (!grant) return plainReason(text, names);
  const [, , subject = '', ceiling = '', until = ''] = grant;
  const name = subject.split('@')[0] ?? '';
  const who = AGENTS[name] ?? (name || 'An agent');
  const reach = ceiling === 'irreversible' ? 'anything that cannot be undone' : 'what it is allowed to do';
  // Noon, so the day reads the same in every time zone.
  return `${who} can act here, up to ${reach}, until ${date(`${until}T12:00:00`)}.`;
}

/** What an audit line's action did, as a person reads it. The raw action id stays under Technical details. */
const AUDIT_ACTIONS: Record<string, string> = {
  'panel.lane.check': 'Checked the account',
  'panel.lane.keepalive': 'Planned the keep-alive visits for',
  'panel.lane.sites': 'Read the site list of',
  'panel.environment.read': 'Opened the page of',
  'panel.environment.close': 'Closed',
  'panel.environment.extend': 'Kept open longer:',
  'panel.job.read': 'Read the output of a command in',
  'panel.job.cancel': 'Stopped a command in',
};

/** The workspace a grant or a routine covers, in words: `*` is every one. */
export function workspaceWords(id: string, name?: string | null): string {
  if (id === '*' || name === '*') return 'every workspace';
  return name || id;
}

/** An audit line in a sentence: "Checked the account Biz B's Forum", whatever action id the node wrote. */
export function auditWords(action: string, target: string | null, names?: PlaceNames): string {
  // The node writes `<kind> <tool>`, such as `panel panel.lane.check`: the tool says what happened.
  const tool = action.split(' ').pop() ?? action;
  const known = AUDIT_ACTIONS[tool];
  // An agent's tool (`browser_close`, `browser_act`) reads as what it does: "Close an account: Test's LinkedIn".
  const agentTool = !known && !tool.startsWith('panel.') && /^[a-z][a-z0-9_]*$/.test(tool);
  const fallback = tool.replace(/^panel\./, '').replaceAll('.', ' ').replaceAll('_', ' ');
  const sentence = known ?? (agentTool ? toolPlainTitle(tool) : fallback.charAt(0).toUpperCase() + fallback.slice(1));
  if (!target) return sentence;
  const named = /^[a-z0-9][a-z0-9-]*\/[a-z0-9][a-z0-9-]*$/.test(target) ? placeName(target, names) : target;
  return agentTool ? `${sentence}: ${named}` : `${sentence} ${named}`;
}

/** A step's summary on the proof page, which the node writes with the account's id: "Check lane biz-a/social". */
const PROOF_STEPS: [RegExp, string][] = [
  [/^Site list of lane (\S+)$/, 'Read the site list of $1'],
  [/^Keepalive plan of lane (\S+)$/, 'Planned the keep-alive visits for $1'],
  [/^Check lane (\S+)$/, 'Checked $1'],
];

export function proofSummary(text: string, names?: PlaceNames): string {
  for (const [pattern, sentence] of PROOF_STEPS) {
    const hit = pattern.exec(text);
    if (hit) return sentence.replace('$1', placeName(hit[1] ?? '', names));
  }
  return plainDetail(text, names);
}

/** Check names the node writes that a person would not say. */
const CHECK_NAMES: Record<string, string> = {
  'Package bubblewrap': 'Sandbox programs',
  'Disk (AGENTOS_HOME and /tmp)': 'Disk space',
  'MCP endpoint': 'Agent address',
  Lanes: 'Accounts',
  'Lane directory formats': 'Account folders',
  Vault: 'Saved logins',
  'Session keepalive': 'Keeping accounts signed in',
  'node.toml': 'Settings file',
};

export function checkName(name: string): string {
  // `agentos doctor` names one check per `agentos connect` connection: "Connection <its name>".
  const connection = /^Connection (\S+)$/.exec(name);
  if (connection) return `Agent connection ${connection[1]}`;
  return CHECK_NAMES[name] ?? name;
}

/**
 * What an AI app can call on a computer, in a person's words. The node names each tool for the agent
 * (`browser_open`, `terminal_create`); the name stays under Technical details. A tool the list does not know reads
 * its own name with the words made plain (`lane` is an account).
 */
export const TOOL_TITLES: Record<string, string> = {
  whoami: 'Check this computer',
  browsers_list: 'List the accounts',
  browser_open: 'Open an account',
  browser_close: 'Close an account',
  browser_view: 'Show the live view',
  browser_claim: 'Hold an account for a job',
  browser_release: 'Let go of an account',
  browser_queue: 'See who waits for an account',
  browser_navigate: 'Open a page',
  browser_snapshot: 'Read a page',
  browser_act: 'Click or type on a page',
  browser_scroll: 'Scroll a page',
  browser_screenshot: 'Take a picture of a page',
  browser_wait: 'Wait for something on a page',
  browser_upload: 'Attach a file to a page',
  browser_tabs: 'List the open tabs',
  browser_tab: 'Switch to a tab',
  browser_dialog: 'See the pop-up box that waits',
  browser_dialog_answer: 'Answer a pop-up box',
  browser_console: "Read a page's error messages",
  browser_network: "See a page's network requests",
  howto: 'Recipes for common jobs',
  browser_file_put: 'Store a file for an account',
  browser_files: "List an account's files",
  browser_file_get: "Read one of an account's files",
  browser_file_delete: "Delete one of an account's files",
  browser_control: 'See who is in control of an account',
  browser_request_control: 'Ask for control back',
  browser_wait_handback: 'Wait for control to come back',
  browser_login_request: 'Ask for a login',
  browser_freeze: 'Freeze an account',
  login: 'Log in with a saved login',
  vault_fill: 'Fill in a saved login',
  vault_list: 'List the saved logins',
  workspace_list: 'List the workspaces',
  workspace_queue: 'See the login queue',
  workspace_plan: 'Preview a template',
  workspace_apply: 'Create accounts from a template',
  browser_set_readiness: "Record an account's login state",
  approval_status: 'Check a yes or no',
  approval_wait: 'Wait for a yes or no',
  approval_confirm: 'Ask for a yes or no',
  human_ask: 'Ask the owner a question',
  traces_query: 'Read the action log',
  grant_info: 'See what an agent may do',
  flows_list: 'List the routines',
  flow_run: 'Run a routine',
  run_status: 'Check a run',
  run_cancel: 'Stop a run',
  flow_save: 'Save a routine',
  ask_first_on: 'Turn on Ask me first',
  flow_record_start: 'Start recording a routine',
  flow_record_stop: 'Stop recording a routine',
  flow_biz_a_social_post: 'Post a photo from a saved routine',
  connectors_list: 'List the site connectors',
  connector_status: 'Check a site connector',
  workflow_save: 'Save a workflow',
  workflow_list: 'List the workflows',
  workflow_get: 'Read a workflow',
  workflow_catalog: 'See what a workflow can use',
  workflow_run: 'Run a workflow now',
  workflow_runs: "Read a workflow's runs",
  workflow_retry: 'Retry a failed step',
  workflow_propose: 'Ask a person to turn on a workflow',
  send_suppress: 'Stop messages to an address',
  leads_propose: 'Propose people to write to',
  site_api_discover: "Find a page's repeated items",
  site_api_try: 'Try a site API',
  site_api_propose: 'Ask a person to turn on a site API',
  site_apis_list: 'List the site APIs',
  mail_connect: 'Ask a person to connect a mailbox',
  mail_list: 'List the mail',
  mail_read: 'Read a message',
  mail_mark: 'Mark a message read or unread',
  mail_send: 'Send an email',
  brain_run: 'Start an AI task',
  brain_status: 'Check an AI task',
  brain_tail: "Read an AI task's steps",
  brain_cancel: 'Stop an AI task',
  brain_resume: 'Resume an AI task',
  brain_tasks: 'List the AI tasks',
  brains_list: 'List the AI services',
  brain_use: 'Choose the AI service for a task',
  terminals_list: 'List the terminals',
  terminal_create: 'Make a terminal',
  terminal_remove: 'Remove a terminal',
  terminal_exec: 'Run a command',
  terminal_job_start: 'Start a program',
  terminal_job_read: "Read a program's output",
  terminal_job_cancel: 'Stop a program',
  terminal_jobs: 'List the programs',
  terminal_upload: 'Send files for a run',
  terminal_upload_check: 'Check which files a run still needs',
  file_read: 'Read a file',
  file_write: 'Write a file',
  file_edit: 'Edit a file',
  environment_open: 'Open its environment',
  environment_close: 'Close its environment',
  environment: 'Read its environment',
  environment_save: 'Save its environment',
  environment_pull_request: 'Open a pull request',
  environment_checkpoint: 'Make an undo point',
  environment_rollback: 'Go back to an undo point',
  environment_port: 'Share a dev server',
  browser_record: 'Record a video of the browser',
  ui_check: "Check a page's design",
  ui_diff: 'Compare a page with before',
  system_check: 'Check the design system',
  profile_use: 'Work to a brand',
  profile_learn_brand: 'Learn a brand from its site',
  terminal_install: 'Install a program',
  browser_check: 'Check an account',
  shell_open: 'Open a terminal',
  shell_write: 'Type in a terminal',
  shell_read: 'Read a terminal',
  shell_close: 'Close a terminal',
  service_start: 'Start a service',
  service_stop: 'Stop a service',
  service_status: 'Check a service',
  service_logs: "Read a service's log",
  images_list: 'List the base images',
  image_build: 'Build a base image',
  events_list: 'List the events',
  triggers_list: 'List the triggers',
  trigger_propose: 'Propose a trigger',
  memory_context: 'Read the memory for a workspace',
  memory_search: 'Search the memory',
  memory_get: 'Read a memory',
  memory_write: 'Save a memory',
  memory_forget: 'Forget a memory',
  plugins_list: 'List the workers',
  browser_task: 'Hand a browser job to the worker',
};

/**
 * The names tools had before 2026-11-19 (agentos.core.toolnames): the action log and an environment's calls
 * from before then still carry them, so they read as the tool does now.
 */
export const OLD_TOOL_NAMES: Record<string, string> = {
  lane_exec: 'terminal_exec',
  lane_job_start: 'terminal_job_start',
  lane_job_read: 'terminal_job_read',
  lane_job_cancel: 'terminal_job_cancel',
  lane_jobs_list: 'terminal_jobs',
  lane_file_read: 'file_read',
  lane_file_write: 'file_write',
  lane_file_edit: 'file_edit',
  lane_wait_control: 'browser_wait_handback',
  lanes_list: 'browsers_list',
  lane_open: 'browser_open',
  lane_close: 'browser_close',
  lane_claim: 'browser_claim',
  lane_release: 'browser_release',
  lane_view: 'browser_view',
  lane_control: 'browser_control',
  lane_request_control: 'browser_request_control',
  lane_queue: 'browser_queue',
  lane_check: 'browser_check',
  lane_freeze: 'browser_freeze',
  lane_login_request: 'browser_login_request',
  lane_set_readiness: 'browser_set_readiness',
  lane_do: 'browser_task',
  files_put: 'browser_file_put',
  files_list: 'browser_files',
  files_get: 'browser_file_get',
  files_delete: 'browser_file_delete',
  env_create: 'terminal_create',
  env_remove: 'terminal_remove',
  env_lanes_list: 'terminals_list',
  lane_install: 'terminal_install',
  lane_upload: 'terminal_upload',
  lane_upload_check: 'terminal_upload_check',
};

/** A tool's name as it is called today: an old name's new one, any other as it is. */
export function currentToolName(name: string): string {
  return OLD_TOOL_NAMES[name] ?? name;
}

export function toolPlainTitle(name: string): string {
  const known = TOOL_TITLES[currentToolName(name)];
  if (known) return known;
  const words = name.replaceAll('_', ' ').replace(/\blanes?\b/g, 'account');
  return words.charAt(0).toUpperCase() + words.slice(1);
}
