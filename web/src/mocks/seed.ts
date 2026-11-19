/**
 * The demo world: Northwind Group, four companies plus personal, 29
 * lanes on two nodes. Everything is fictional. IPs come from the documentation ranges
 * (RFC 5737), so nothing here can point at a real host.
 */
import { seedAccess } from './access';
import type { BrainTask, BrowserDevice, EnvSave, FormTrip, LaneLogin, Proxy, ProofShare, StudioRun, Vm } from '@/api/types';
import type { MockBrand } from './brand';
import type { MockEnv } from './environments';
import type { MockJob } from './jobs';
import { workerQuestion } from './workers';
import type {
  Approval,
  PreviewData,
  PreviewKind,
  AuditEvent,
  Brain,
  CodeRequest,
  Device,
  Effect,
  FleetNode,
  FreezeRecord,
  Flow,
  Grant,
  Invite,
  JoinToken,
  Lane,
  LaneKind,
  LaneState,
  Member,
  NodeLoad,
  NotificationSettings,
  Org,
  PushDevice,
  Retention,
  Webhook,
  WebhookDelivery,
  Passkey,
  PlanLimits,
  Principal,
  Question,
  Role,
  Run,
  RunStatus,
  RunStep,
  Schedule,
  SessionUser,
  Signal,
  StandingApproval,
  TraceEvent,
  VaultEntry,
} from '@/api/types';

import type { AskFirstSetting } from './ask-first';
import { emptyAccount, makeAccount } from './account';
import type { AccountRecords } from './account';
import { projectFires } from '@/lib/cron';
import { laneName } from '@/lib/labels';
import { idleControl } from '@/lib/lanes';

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

export function rng(seed: number) {
  let state = seed;
  return () => {
    state = (state + 0x6d2b79f5) | 0;
    let t = Math.imul(state ^ (state >>> 15), 1 | state);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const ULID_ALPHABET = '0123456789ABCDEFGHJKMNPQRSTVWXYZ';

export function makeIds(seed: number) {
  const random = rng(seed);
  return (prefix: string) => {
    let text = '';
    for (let index = 0; index < 16; index += 1) {
      text += ULID_ALPHABET[Math.floor(random() * 32)];
    }
    return `${prefix}_${text}`;
  };
}

export interface WorkspaceRecord {
  id: string;
  name: string;
  color: string;
}

export interface NodeRecord extends FleetNode {
  os: string;
  arch: string;
  load: NodeLoad;
}

/** What a computer keeps for a person beyond the lists above: see mocks/access.ts. */
export interface AccessData {
  vaultState: 'sealed' | 'unsealed';
  devices: BrowserDevice[];
  proxies: Proxy[];
  /** A sign-in per lane id: the owner's login form and what the computer reports. */
  logins: Record<string, LaneLogin>;
  /** Every form a secret is typed into, by trip id. `fields` are the plain values the app sent. */
  trips: Record<string, FormTrip & { fields: Record<string, string>; opened: number }>;
}

export interface OrgData extends AccessData {
  org: Org;
  role: Role;
  limits: PlanLimits;
  workspaces: WorkspaceRecord[];
  lanes: Lane[];
  approvals: Approval[];
  questions: Question[];
  /** Open one-time login codes a site asked for. Only the owner is asked for them. */
  codes: CodeRequest[];
  /** Freezes in force on this computer (break glass). */
  freeze: FreezeRecord[];
  activity: TraceEvent[];
  runs: Run[];
  flows: Flow[];
  /** The Ask me first settings the computers hold. */
  askFirst: AskFirstSetting[];
  /** An account's page: the owner's pace, limits and sites, and what each account is doing. */
  account: AccountRecords;
  schedules: Schedule[];
  nodes: NodeRecord[];
  joinTokens: JoinToken[];
  brains: Brain[];
  vault: VaultEntry[];
  grants: Grant[];
  standing: StandingApproval[];
  /** Decided approvals, newest first, for Review links and delivery. */
  history: Approval[];
  pushDevices: (PushDevice & { endpoint: string })[];
  webhooks: Webhook[];
  deliveries: Record<string, WebhookDelivery[]>;
  retention: Retention;
  members: Member[];
  invites: Invite[];
  audit: AuditEvent[];
  /** Read-only links made in Proof, with their tokens (the mock keeps the token, the app never reads it back). */
  proofShares?: (ProofShare & { token: string })[];
  /** What agents handed to an AI model. Kept in the mock only, so a task can be stopped and carried on. */
  brainTasks?: BrainTask[];
  /** Runs started with `agentos run` (mocks/jobs.ts), seeded on first read. */
  jobs?: MockJob[];
  /** A test holds the running runs: they keep printing but never end (`holdRunningJobs`). Reset with the db. */
  jobsHeld?: boolean;
  /** Environments, the cloud computers agents rent for one chat (mocks/environments.ts). */
  environments?: MockEnv[];
  /** Saves of kept environments (mocks/environments.ts), newest first. */
  environmentSaves?: EnvSave[];
  /** Studio checks (mocks/studio.ts), and whether studio is missing on this computer. */
  studioRuns?: StudioRun[];
  studioMissing?: boolean;
  /** Product and business profiles (mocks/brand.ts), and disposable VMs. */
  brands?: MockBrand[];
  vms?: Vm[];
  notifications: NotificationSettings;
  costToday: { tokens: number; usd: number };
}

export interface MockDb {
  user: SessionUser;
  stepUpUntil: string | null;
  passkeys: Passkey[];
  devices: Device[];
  orgs: Map<string, OrgData>;
}

const SURFACES = ['instagram', 'facebook', 'reddit', 'medium', 'blog', 'linkedin'] as const;

const COMPANIES: (WorkspaceRecord & { node: string; subnet: string })[] = [
  { id: 'aster', name: 'Aster Coffee', color: '#a0763f', node: 'home-mini', subnet: '203.0.113' },
  { id: 'harbor', name: 'Harbor Supply', color: '#3f76a0', node: 'hetzner-1', subnet: '198.51.100' },
  { id: 'meridian', name: 'Meridian Travel', color: '#3f9a8c', node: 'hetzner-1', subnet: '192.0.2' },
  { id: 'solstice', name: 'Solstice Studio', color: '#8c5fa8', node: 'hetzner-1', subnet: '203.0.113' },
];

const WARM = new Set([
  'aster/instagram',
  'aster/linkedin',
  'aster/blog',
  'harbor/instagram',
  'harbor/reddit',
  'harbor/linkedin',
  'meridian/instagram',
  'meridian/facebook',
  'meridian/linkedin',
  'meridian/medium',
  'solstice/instagram',
  'solstice/facebook',
  'solstice/blog',
]);
const ACTIVE = new Set(['aster/instagram', 'meridian/instagram']);
const SIGNALS: Record<string, Signal> = {
  'harbor/reddit': 'captcha',
  'harbor/linkedin': 'checkpoint',
  'solstice/facebook': 'logged_out',
  'meridian/reddit': 'rate_limited',
};
const SIGNAL_DETAIL: Record<string, string> = {
  'harbor/reddit': 'reCAPTCHA frame on the post form',
  'harbor/linkedin': 'Verification code page',
  'solstice/facebook': 'Redirected to the login page',
  'meridian/reddit': 'HTTP 429 from the site',
};

function makeLane(
  now: number,
  random: () => number,
  id: string,
  kind: LaneKind,
  node: string,
  subnet: string,
  host: number,
): Lane {
  const [workspace = '', name = ''] = id.split('/');
  const state: LaneState = ACTIVE.has(id) ? 'active' : WARM.has(id) ? 'warm' : 'cold';
  const signal = SIGNALS[id] ?? (state === 'cold' && workspace === 'personal' ? 'unknown' : 'ok');
  const usesProxy = kind === 'browser' || kind === 'android';
  const ip = `${subnet}.${host}`;
  return {
    id,
    workspace,
    name,
    kind,
    engine: kind === 'browser' ? 'camoufox' : null,
    preset: kind === 'terminal' ? 'workbench' : kind === 'service' ? 'service' : 'account',
    state,
    signal,
    proxy: usesProxy ? { label: `${workspace}-${name}`, host: ip, port: 12323 } : null,
    exit_ip: usesProxy ? ip : null,
    last_used: new Date(now - (state === 'cold' ? 6 * HOUR + random() * 3 * DAY : random() * 50 * MINUTE)).toISOString(),
    node,
    quarantine_reason: null,
    logged_in_hint: signal === 'logged_out' ? 'needs_login' : signal === 'unknown' ? 'unknown' : 'ok',
    viewer: state === 'cold' ? null : { url: `/api/orgs/northwind-group/lanes/${id}/vnc` },
    control: { ...idleControl },
    hold: null,
    hold_until: null,
    signal_detail: SIGNAL_DETAIL[id] ?? null,
    account: kind === 'browser' && signal !== 'logged_out' ? { site: `${name}.com`, user_id: `${workspace}-${name}`, source: 'profile', status: 'match' } : null,
    readiness: signal === 'logged_out' ? 'needs_login' : signal === 'unknown' ? 'needs_login' : 'ready',
    readiness_note: null,
    login_url: kind === 'browser' ? `https://${name}.example/login` : null,
  };
}

/** The states the lane window has to show, placed on lanes people would actually visit. */
function setScene(lanes: Lane[], now: number) {
  const find = (id: string) => lanes.find((lane) => lane.id === id) as Lane;
  const ago = (ms: number) => new Date(now - ms).toISOString();

  // A person is mid-way through a login, and the agent has asked to carry on.
  Object.assign(find('meridian/linkedin'), {
    control: {
      state: 'human',
      by: 'Mara Lindqvist',
      by_id: 'usr_MARA',
      since: ago(4 * MINUTE),
      takeback_requested: true,
      takeback_reason: 'The scheduled post is due in 10 minutes',
      takeback_by: 'claude-code',
      last_human_input: ago(40_000),
    },
  });
  // Someone stopped an agent that was going the wrong way.
  Object.assign(find('solstice/blog'), {
    control: { ...idleControl, state: 'paused', by: 'Demo Owner', by_id: 'usr_DEMO', since: ago(22 * MINUTE) },
    hold: 'Stopped by Demo Owner',
  });
  // A self-clearing hold, with its end time.
  Object.assign(find('meridian/reddit'), {
    hold: 'The site asked this lane to slow down',
    hold_until: new Date(now + 37 * MINUTE).toISOString(),
  });
  // The wrong account, which quarantines the lane until a person looks.
  Object.assign(find('aster/reddit'), {
    state: 'quarantined',
    quarantine_reason: 'Signed in as a different account than this lane is pinned to',
    hold: 'wrong_account',
    account: { site: 'reddit.com', user_id: 'someone-else', source: 'profile', status: 'wrong_account' },
    readiness: 'needs_attention',
    readiness_note: 'Signed in as the wrong account',
  });
  Object.assign(find('harbor/linkedin'), {
    hold: 'needs_human:checkpoint',
    readiness: 'needs_attention',
    readiness_note: 'Phone code asked',
  });
  Object.assign(find('harbor/reddit'), { hold: 'needs_human:captcha' });
}

function makeLanes(now: number): Lane[] {
  const random = rng(7);
  const lanes: Lane[] = [];
  let host = 10;
  for (const company of COMPANIES) {
    for (const surface of SURFACES) {
      host += 3;
      lanes.push(makeLane(now, random, `${company.id}/${surface}`, 'browser', company.node, company.subnet, host));
    }
  }
  const personal: [string, LaneKind][] = [
    ['mail', 'browser'],
    ['linkedin', 'browser'],
    ['shell', 'terminal'],
    ['notes-sync', 'service'],
    ['pixel', 'android'],
  ];
  for (const [name, kind] of personal) {
    host += 3;
    lanes.push(makeLane(now, random, `personal/${name}`, kind, 'home-mini', '192.0.2', host));
  }
  setScene(lanes, now);
  return lanes;
}

const AGENTS: Principal[] = [
  { kind: 'agent', id: 'claude-code', thread: 'T-91' },
  { kind: 'agent', id: 'claude-app', thread: 'T-104' },
  { kind: 'flow', id: 'weekly-posts' },
  { kind: 'agent', id: 'codex', thread: 'T-77' },
];

function hash(seed: number): string {
  const random = rng(seed);
  let text = '';
  for (let index = 0; index < 64; index += 1) text += Math.floor(random() * 16).toString(16);
  return `sha256:${text}`;
}

interface ApprovalSeed {
  /** What the approval shows beyond a post, as the cloud sends it (`preview.kind`, `preview.data`). */
  kind?: PreviewKind;
  data?: PreviewData;
  effect: Effect;
  lane: string;
  summary: string;
  text?: string;
  image?: boolean;
  agent?: number;
  ageMinutes: number;
  ttlHours?: number;
}

const APPROVALS: ApprovalSeed[] = [
  {
    effect: 'commit',
    lane: 'aster/instagram',
    summary: 'Post 1 photo with caption to instagram.com as @astercoffee',
    text: 'Our autumn blend is back. Washed Ethiopian with a little dried plum on the finish. Roasted Tuesday, on shelves Thursday.',
    image: true,
    ageMinutes: 4,
  },
  {
    effect: 'commit',
    lane: 'aster/linkedin',
    summary: 'Publish 1 post to linkedin.com as @astercoffee',
    text: 'We are hiring a head roaster for the Riverside site. Five years of production roasting, and a patient palate.',
    agent: 1,
    ageMinutes: 9,
  },
  {
    effect: 'commit',
    lane: 'harbor/facebook',
    summary: 'Post 1 photo with caption to facebook.com as @harborsupply',
    text: 'New marine rope in stock: 12 mm, 16 mm and 20 mm, cut to length in store.',
    image: true,
    ageMinutes: 22,
  },
  {
    effect: 'commit',
    lane: 'meridian/instagram',
    summary: 'Post 4 photos with caption to instagram.com as @meridiantravel',
    text: 'Four quiet days on the Dalmatian coast. Our autumn itinerary is open for booking.',
    image: true,
    agent: 2,
    ageMinutes: 31,
  },
  {
    effect: 'commit',
    lane: 'solstice/medium',
    summary: 'Publish 1 story to medium.com as @solsticestudio',
    // A long story the node had to shorten, with the core's own marker at the end.
    text: 'What we learned from a year of designing for small museums. A short essay on labels, light and patience.\n[preview truncated: 1840 more characters are not shown]',
    agent: 1,
    ageMinutes: 47,
  },
  {
    effect: 'pay',
    lane: 'meridian/facebook',
    summary: 'Boost 1 post on facebook.com for $40.00 over 5 days',
    text: 'Daily budget $8.00, audience saved as "Coastal travellers". Billed to the card on the Meridian ad account.',
    ageMinutes: 58,
  },
  {
    effect: 'irreversible',
    lane: 'harbor/blog',
    summary: 'Delete 3 draft pages from the harbor blog',
    text: '/drafts/spring-sale-old\n/drafts/test-page\n/drafts/copy-of-about',
    agent: 3,
    ageMinutes: 73,
  },
  {
    effect: 'secret',
    lane: 'solstice/facebook',
    summary: 'Release the saved login for facebook.com to this lane',
    text: 'The lane is logged out. The node will fill the login from its vault. The agent never sees it.',
    ageMinutes: 12,
  },
  // Everyday work beyond posting: email, money, forms, shop, messages, files and the computer.
  {
    kind: 'email',
    effect: 'commit',
    lane: 'aster/gmail',
    summary: 'Email to accounts@beanco.example: Invoice 1182 from Aster Coffee',
    data: {
      from: 'hello@astercoffee.example',
      to: ['accounts@beanco.example'],
      cc: [],
      subject: 'Invoice 1182 from Aster Coffee',
      body: 'Hello,\n\nPlease find invoice 1182 attached, for the October cupping sessions. Payment is due in 14 days.\n\nThank you,\nAster Coffee',
      attachments: [{ name: 'invoice-1182.pdf', size: 84_213 }],
    },
    ageMinutes: 6,
  },
  {
    kind: 'payment',
    effect: 'pay',
    lane: 'harbor/quickbooks',
    summary: 'Pay €412.50 to Nordic Ropeworks, reference INV-4471',
    data: {
      amount: 412.5,
      currency: 'EUR',
      payee: 'Nordic Ropeworks',
      reference: 'INV-4471',
      for: 'Supplier invoice: 16 mm marine rope, 200 m',
      from_account: 'Business account ending 4471',
      due: 'in 3 days',
    },
    ageMinutes: 66,
  },
  {
    kind: 'form',
    effect: 'commit',
    lane: 'meridian/booking',
    summary: 'Update the listing: price per person €610 to €690',
    data: {
      form: 'Listing: Dalmatian coast, 4 days',
      fields: [
        { label: 'Price per person', value: '€690', before: '€610' },
        { label: 'Dates', value: '12 to 15 May' },
        { label: 'Rooms left', value: '6', before: '8' },
      ],
      changed: ['Price per person', 'Rooms left'],
    },
    agent: 1,
    ageMinutes: 34,
  },
  {
    kind: 'order',
    effect: 'pay',
    lane: 'harbor/shopify',
    summary: 'Refund €24 to Lina Berg for order #1432',
    data: {
      order: '#1432',
      customer: 'Lina Berg',
      items: [{ name: 'Boat cover clips, pack of 10', quantity: 1, price: 24 }],
      amount: 24,
      currency: 'EUR',
      reason: 'Arrived damaged',
    },
    ageMinutes: 71,
  },
  {
    kind: 'message',
    effect: 'commit',
    lane: 'meridian/tripadvisor',
    summary: "Reply to Jonas Petraitis's 3-star review",
    data: {
      thread: [{ from: 'Jonas Petraitis', text: 'The booking page was confusing, otherwise a lovely trip. Our guide in Split was wonderful.', stars: 3 }],
      reply: 'Thank you, Jonas. We are glad Split was a highlight, and you are right about the booking page: we are making it simpler this month. Come back soon. Meridian Travel',
    },
    agent: 1,
    ageMinutes: 15,
  },
  {
    kind: 'delete',
    effect: 'irreversible',
    lane: 'personal/shell',
    summary: 'Delete 3 old files from Downloads',
    data: {
      computer: 'home-mini',
      command: 'rm ~/Downloads/old-installer.dmg ~/Downloads/screen-recording-0412.mov ~/Downloads/tmp-export.zip',
      items: [
        { name: '/home/me/Downloads/old-installer.dmg', size: 84_200_000 },
        { name: '/home/me/Downloads/screen-recording-0412.mov', size: 61_500_000 },
        { name: '/home/me/Downloads/tmp-export.zip', size: 2_300_000 },
      ],
    },
    agent: 3,
    ageMinutes: 64,
  },
  {
    kind: 'command',
    effect: 'exec',
    lane: 'personal/shell',
    summary: 'Run the backup of Documents now',
    data: { computer: 'home-mini', cwd: '~', command: 'restic backup ~/Documents --tag nightly' },
    agent: 3,
    ageMinutes: 82,
  },
  {
    kind: 'form',
    effect: 'irreversible',
    lane: 'personal/gov',
    summary: 'Submit the October VAT return',
    data: {
      form: 'VAT return, October',
      fields: [
        { label: 'Period', value: '1 to 31 October' },
        { label: 'Sales', value: '€6,480.00' },
        { label: 'VAT due', value: '€612.40' },
      ],
      changed: [],
    },
    ageMinutes: 90,
  },
  {
    effect: 'install_base',
    lane: 'personal/shell',
    summary: 'Rebuild base image dev-box@1 with ffmpeg 7.1 (touches 3 lanes)',
    text: 'apt-get install -y ffmpeg=7:7.1-1',
    agent: 3,
    ageMinutes: 96,
  },
  {
    kind: 'push',
    effect: 'irreversible',
    lane: 'harbor/run-shop',
    summary: 'Push main to github.com/harbor-supply/shop (claude-code): it force-pushes main (commits on the remote would be lost); main is a protected branch of github.com/harbor-supply/shop',
    data: {
      remote: 'github.com/harbor-supply/shop',
      push_refs: [
        {
          ref: 'refs/heads/main',
          name: 'main',
          kind: 'update',
          force: true,
          protected: true,
          commits: 3,
          log: ['7c1e9a4 Round the cart total to cents before charging', '2d8f3e6 Show the card error next to the field', '0b5c9d1 Test the checkout in dark mode'],
        },
      ],
    },
    ageMinutes: 7,
  },
  {
    kind: 'vm',
    effect: 'vm',
    lane: 'harbor/ci-box',
    summary: 'Rent a cpx21 VM (3 vCPU, 4 GB, 80 GB disk, x86) in hel1 as lane harbor/ci-box for up to 90 min, EUR 0.0119 an hour (at most EUR 0.04 for 90 min). Asked for: the cheapest that fits (small profile), image ubuntu-24.04',
    data: { computer: 'harbor/ci-box', server_type: 'cpx21', vm_profile: 'small', ttl_minutes: 90, price_hourly_eur: 0.0119, max_cost_eur: 0.04 },
    ageMinutes: 9,
  },
  {
    kind: 'lane',
    effect: 'exec',
    lane: 'harbor/run-shop',
    summary: 'Sync 1284 files (12 changed, 1 deleted) into src/harbor-shop and run in the background in sandbox lane harbor/run-shop (egress via forwarder to registry.npmjs.org, filesystem overlay), in src/harbor-shop with PYTHONUNBUFFERED set for up to 3600s: pnpm vitest run',
    data: {
      computer: 'harbor/run-shop',
      command: 'pnpm vitest run',
      cwd: 'src/harbor-shop',
      timeout_s: 3600,
      sync_dest: 'src/harbor-shop',
      synced_files: null,
      lane_preset: 'sandbox',
      fields: [
        { label: 'Network', value: 'isolated' },
        { label: 'Can reach', value: 'registry.npmjs.org' },
      ],
    },
    ageMinutes: 11,
  },
];

const TOOLS: Partial<Record<Effect, string>> = {
  exec: 'terminal_job_start',
  vm: 'vm_create',
  pay: 'browser_act',
  irreversible: 'browser_act',
  secret: 'vault_fill',
  install_base: 'image_rebuild',
};

/** Why the node asks, in the node's words. */
const REASONS: Partial<Record<Effect, string>> = {
  pay: 'Spending money always asks a person.',
  irreversible: 'This cannot be undone, so a person decides.',
  secret: 'A saved login is released only when a person says so.',
  install_base: 'A shared image change touches several lanes.',
};

const routineOf = (seed: ApprovalSeed) =>
  `${seed.lane.replace('/', '.')}.${seed.kind === 'email' ? 'send' : seed.kind === 'message' ? 'reply' : seed.kind === 'form' ? 'update' : 'post'}`;

/** A commit asks because a switch is on, or because a safety check stopped it running on its own. */
function reasonOf(seed: ApprovalSeed): string | null {
  if (seed.effect !== 'commit') return REASONS[seed.effect] ?? null;
  if (seed.kind === 'email') return 'it follows untrusted text from an email at nordicbeans-orders.example';
  return `Ask me first is on for routine ${routineOf(seed)}`;
}

function makeApprovals(now: number, ids: (prefix: string) => string, org: string): Approval[] {
  return APPROVALS.map((seed, index) => {
    const id = ids('ap');
    const agent = AGENTS[seed.agent ?? 0] as Principal;
    return {
      id,
      state: 'pending',
      effect: seed.effect,
      lane: seed.lane,
      summary: seed.summary,
      content_hash: hash(100 + index),
      content_digest: hash(200 + index),
      kind: 'action',
      // Publishing runs as a versioned flow; everything else is one tool call.
      flow_target: seed.effect === 'commit' ? `flow:${routineOf(seed)}@3` : null,
      tool: seed.effect === 'commit' ? null : (TOOLS[seed.effect] ?? 'browser_act'),
      reason: reasonOf(seed),
      delivery: null,
      delivery_error: null,
      preview: {
        text: seed.text ?? seed.data?.body ?? seed.data?.reply ?? seed.data?.command ?? null,
        image: seed.image ? `/api/orgs/${org}/approvals/${id}/preview.png` : null,
        ...(seed.kind ? { kind: seed.kind, data: seed.data ?? null } : {}),
      },
      requested_by: agent,
      created_at: new Date(now - seed.ageMinutes * MINUTE).toISOString(),
      expires_at: new Date(now + (seed.ttlHours ?? 20) * HOUR).toISOString(),
    };
  });
}

const surface = (lane: Lane) => laneName(lane.name);

function makeQuestions(now: number, ids: (prefix: string) => string): Question[] {
  const base = { state: 'open' as const, answer: null, answered_at: null, answered_by: null };
  return [
    {
      ...base,
      id: ids('ap'),
      lane: 'aster/instagram',
      question: 'Which photo should lead Thursday\'s post?',
      options: ['The kraft bag on the counter', 'The roaster at sunrise', 'Skip Thursday'],
      asked_by: AGENTS[0] as Principal,
      created_at: new Date(now - 6 * MINUTE).toISOString(),
      expires_at: new Date(now + 20 * HOUR).toISOString(),
    },
    {
      ...base,
      id: ids('ap'),
      lane: null,
      question: 'What discount code should the autumn newsletter use?',
      options: null,
      asked_by: AGENTS[1] as Principal,
      created_at: new Date(now - 18 * MINUTE).toISOString(),
      expires_at: new Date(now + 44 * HOUR).toISOString(),
    },
    workerQuestion(now, ids('ap')),
  ];
}

/** A login at its code step on Aster's Instagram, asked by Claude Code: the owner is the one who types it. */
function makeCodes(now: number, ids: (prefix: string) => string): CodeRequest[] {
  return [
    {
      id: ids('cr'),
      state: 'open',
      lane: 'aster/instagram',
      site: 'instagram.com',
      requested_by: { kind: 'agent', id: 'claude-code' },
      created_at: new Date(now - 4 * MINUTE).toISOString(),
      expires_at: new Date(now + 9 * MINUTE).toISOString(),
    },
  ];
}

interface WorkTemplate {
  tool: string;
  effect: Effect;
  summary: (lane: Lane) => string;
  usd: number;
  tokens: number;
}

const WORK: WorkTemplate[] = [
  { tool: 'browser.post', effect: 'commit', summary: (lane) => `Posted 1 photo to ${surface(lane)} as ${lane.workspace}`, usd: 0.08, tokens: 8200 },
  { tool: 'browser.post', effect: 'commit', summary: (lane) => `Published 1 post to ${surface(lane)} as ${lane.workspace}`, usd: 0.06, tokens: 6100 },
  { tool: 'browser.read', effect: 'read', summary: (lane) => `Read 24 new comments on ${surface(lane)}`, usd: 0.02, tokens: 2400 },
  { tool: 'browser.draft', effect: 'draft', summary: (lane) => `Saved a draft on ${surface(lane)}`, usd: 0.03, tokens: 3100 },
  { tool: 'browser.read', effect: 'read', summary: (lane) => `Checked notifications on ${surface(lane)}`, usd: 0.01, tokens: 900 },
  { tool: 'browser.reply', effect: 'commit', summary: (lane) => `Replied to 3 comments on ${surface(lane)}`, usd: 0.05, tokens: 5200 },
];

const STATUS_POOL: RunStatus[] = [
  'succeeded_verified',
  'succeeded_verified',
  'succeeded_verified',
  'succeeded_verified',
  'succeeded_verified',
  'succeeded_unverified',
  'succeeded_unverified',
  'partial',
  'failed',
  'cancelled',
];

function makeSteps(ids: (prefix: string) => string, run: Run, start: number): RunStep[] {
  const failed = run.status === 'failed';
  const steps: [string, string][] = [
    ['lane.open', 'Lane was already warm'],
    ['browser.navigate', 'Opened the compose page'],
    ['browser.fill', 'Filled the post body'],
    ['browser.submit', failed ? 'Submit returned an error page' : 'Submitted the post'],
  ];
  if (!failed) {
    steps.push([
      'proof.verify',
      run.status === 'succeeded_verified' ? 'Found the post on the public profile' : 'Could not confirm the post from outside',
    ]);
  }
  return steps.map(([tool, summary], index) => ({
    id: ids('stp'),
    at: new Date(start + index * 7_000).toISOString(),
    tool,
    summary,
    status: index === steps.length - 1 ? run.status : 'succeeded_verified',
  }));
}

function makeWork(now: number, ids: (prefix: string) => string, lanes: Lane[], slug: string) {
  const random = rng(42);
  const pick = <T,>(items: readonly T[]): T => items[Math.floor(random() * items.length)] as T;
  // Posting work belongs to the company lanes. Personal lanes have a quieter history.
  const browserLanes = lanes.filter((lane) => lane.kind === 'browser' && lane.workspace !== 'personal');
  const activity: TraceEvent[] = [];
  const runs: Run[] = [];
  let at = now - 3 * MINUTE;
  // Enough to fill the 30 day charts, so a bar and the list under it always agree.
  for (let index = 0; index < 1450; index += 1) {
    at -= (8 + random() * 44) * MINUTE;
    const lane = pick(browserLanes);
    const work = pick(WORK);
    const principal = pick(AGENTS);
    const status: RunStatus = work.effect === 'read' ? 'succeeded_verified' : pick(STATUS_POOL);
    const scale = 0.6 + random() * 0.9;
    const cost = {
      tokens: Math.round(work.tokens * scale),
      usd: Math.round(work.usd * scale * 100) / 100,
    };
    const summary = status === 'failed' ? `Failed: ${work.summary(lane)}` : work.summary(lane);
    const runId = work.effect === 'read' ? null : ids('run');
    if (runId) {
      const verified = status === 'succeeded_verified';
      const run: Run = {
        id: runId,
        flow: `${lane.workspace}.${lane.name}.post`,
        lane: lane.id,
        status,
        summary,
        principal,
        started_at: new Date(at).toISOString(),
        finished_at: new Date(at + 34_000).toISOString(),
        cost,
        proof: verified
          ? {
              url: `https://example.com/${lane.workspace}/${lane.name}/${index}`,
              id: `post-${1000 + index}`,
              screenshot: `/api/orgs/${slug}/runs/${runId}/proof.png`,
            }
          : null,
      };
      run.steps = makeSteps(ids, run, at);
      runs.push(run);
    }
    activity.push({
      id: ids('trc'),
      at: new Date(at).toISOString(),
      kind: 'tool_call',
      tool: work.tool,
      lane: lane.id,
      // The full principal, as the cloud sends it on a trace.
      principal,
      effect: work.effect,
      status,
      summary,
      cost,
      run_id: runId,
    });
  }
  // One run still going, so a live bar and a stop are always there to see.
  const lane = browserLanes[0] as Lane;
  const live: Run = {
    id: ids('run'),
    flow: `${lane.workspace}.${lane.name}.post`,
    lane: lane.id,
    status: 'running',
    summary: `Publishing the weekly post on ${lane.id}`,
    principal: AGENTS[0] as Principal,
    started_at: new Date(now - 41_000).toISOString(),
    finished_at: null,
    cost: { tokens: 2_140, usd: 0.02 },
    proof: null,
  };
  live.steps = [
    { id: ids('stp'), at: new Date(now - 38_000).toISOString(), tool: 'lane.open', summary: 'Lane was already warm', status: 'succeeded_verified' },
    { id: ids('stp'), at: new Date(now - 27_000).toISOString(), tool: 'browser.navigate', summary: 'Opened the compose page', status: 'succeeded_verified' },
    { id: ids('stp'), at: new Date(now - 27_000).toISOString(), tool: 'browser.fill', summary: 'Filling the post body', status: 'running' },
  ];
  runs.unshift(live);
  activity.unshift({
    id: ids('trc'),
    at: live.started_at,
    kind: 'tool_call',
    tool: 'flow.run',
    lane: lane.id,
    principal: AGENTS[0] as Principal,
    effect: 'commit',
    status: 'running',
    summary: live.summary,
    cost: live.cost,
    run_id: live.id,
  });
  return { activity, runs };
}

/** A routine on an account with no run of its own yet gets one, so its page has something real to open. */
function madeRun(now: number, ids: (prefix: string) => string, runs: Run[], flow: string, lane: string, effect: Effect, index: number): Run {
  const at = new Date(now - (index + 1) * 26 * HOUR).toISOString();
  const run: Run = {
    id: ids('run'),
    flow,
    lane,
    status: 'succeeded_verified',
    summary: `Ran ${flow}`,
    principal: { kind: 'flow', id: flow, thread: null },
    started_at: at,
    finished_at: at,
    cost: { tokens: 6400, usd: 0.07 },
    proof: null,
    steps: [],
    effect,
  };
  runs.push(run);
  return run;
}

function makeFlows(now: number, ids: (prefix: string) => string, runs: Run[]): Flow[] {
  const seeds: [string, string, Effect][] = [
    ['harbor.xero.invoice-reminders', 'harbor/xero', 'commit'],
    ['aster.portal.bean-order', 'aster/portal', 'commit'],
    ['solstice.outlook.outreach', 'solstice/outlook', 'commit'],
    ['meridian.booking.supplier-form', 'meridian/booking', 'commit'],
    ['aster.gmail.newsletter', 'aster/gmail', 'commit'],
    ['aster.gmail.welcome-series', 'aster/gmail', 'commit'],
    ['harbor.xero.pay-suppliers', 'harbor/xero', 'pay'],
    ['harbor.shopify.delete-old-orders', 'harbor/shopify', 'irreversible'],
    ['aster.instagram.post', 'aster/instagram', 'commit'],
    ['aster.linkedin.post', 'aster/linkedin', 'commit'],
    ['harbor.facebook.post', 'harbor/facebook', 'commit'],
    ['harbor.blog.publish', 'harbor/blog', 'commit'],
    ['meridian.instagram.post', 'meridian/instagram', 'commit'],
    ['meridian.facebook.boost', 'meridian/facebook', 'pay'],
    ['solstice.medium.publish', 'solstice/medium', 'commit'],
    ['solstice.reddit.read', 'solstice/reddit', 'read'],
    ['personal.mail.digest', 'personal/mail', 'read'],
    ['harbor.shopify.new-order', 'harbor/shopify', 'commit'],
  ];
  return seeds.map(([id, lane, effect], index) => {
    const run = runs.find((item) => item.lane === lane) ?? (index % 4 === 3 ? undefined : madeRun(now, ids, runs, id, lane, effect, index));
    return {
      id,
      lane,
      version: 1 + (index % 3),
      effect,
      params:
        effect === 'read'
          ? {}
          : {
              caption: { type: 'string', label: 'Text', required: true },
              ...(effect === 'pay' ? { budget_usd: { type: 'number', label: 'Budget in USD', required: true } } : {}),
            },
      last_run: run ? { id: run.id, status: run.status, started_at: run.started_at, cost: run.cost } : null,
      recent_runs: runs
        .filter((item) => item.lane === lane)
        .slice(0, 20)
        .map((item) => ({ id: item.id, status: item.status, at: item.started_at })),
    };
  });
}

/**
 * Most routines run on their own. An agent turned one on for itself, a person turned on the
 * newsletter and the routines waiting in Needs you, and one account asks about everything there.
 */
function makeAskFirst(now: number, approvals: Approval[]): AskFirstSetting[] {
  const ago = (hours: number) => new Date(now - hours * HOUR).toISOString();
  const me = { kind: 'user', id: 'usr_DEMO' } as const;
  const named: AskFirstSetting[] = [
    { scope: 'flow', id: 'aster.gmail.newsletter', ask_first: true, set_by: me, set_at: ago(50) },
    { scope: 'flow', id: 'harbor.blog.publish', ask_first: true, set_by: { kind: 'agent', id: 'claude-code' }, set_at: ago(3) },
    {
      scope: 'flow',
      id: 'aster.gmail.welcome-series',
      ask_first: true,
      set_by: { kind: 'agent', id: 'claude-code' },
      set_at: ago(5),
      why: 'a person set routine aster.gmail.newsletter on aster/gmail to ask first, so a new routine an agent saves there starts the same way',
    },
    { scope: 'lane', id: 'harbor/quickbooks', ask_first: true, set_by: { kind: 'computer', id: 'owner:cli' }, set_at: ago(30) },
  ];
  // What waits in Needs you waits because a switch says so, unless a safety check made it ask.
  const waiting = approvals.filter((item) => item.effect === 'commit' && item.reason?.startsWith('Ask me first'));
  const settings = waiting.map((item): AskFirstSetting => {
    const flow = item.flow_target?.replace(/^flow:/, '').replace(/@.*$/, '');
    return { scope: flow ? 'flow' : 'lane', id: flow ?? item.lane, ask_first: true, set_by: me, set_at: ago(26) };
  });
  return [...named, ...settings.filter((item) => !named.some((other) => other.scope === item.scope && other.id === item.id))];
}

/** Times come from the cron line on the lane's own node, as a real node reports them. */
function makeSchedules(now: number, ids: (prefix: string) => string, flows: Flow[], zoneOfLane: (lane: string) => string): Schedule[] {
  const made = (flow: string, lane: string) => {
    const last = flows.find((item) => item.id === flow)?.last_run;
    return { id: ids('sch'), flow, lane, params: {}, enabled: true, last_run: last ? { id: last.id, status: last.status } : null };
  };
  const cron = (flow: string, lane: string, line: string, deadman: number | null): Schedule => {
    const next = projectFires(line, now, now + 14 * DAY, zoneOfLane(lane), 1)?.[0];
    const at = next ? new Date(next).toISOString() : null;
    return { ...made(flow, lane), cron: line, deadman_minutes: deadman, next_run: at, next_fire_at: at };
  };
  return [
    cron('aster.instagram.post', 'aster/instagram', '0 9 * * 1,3,5', 30),
    cron('aster.linkedin.post', 'aster/linkedin', '0 11 * * 2', null),
    cron('harbor.blog.publish', 'harbor/blog', '30 8 * * 1', 60),
    cron('meridian.instagram.post', 'meridian/instagram', '0 18 * * *', 30),
    { ...made('solstice.reddit.read', 'solstice/reddit'), watch: 'new posts in r/typography', deadman_minutes: 120, next_run: null, next_fire_at: null },
    { ...cron('personal.mail.digest', 'personal/mail', '0 7 * * *', null), enabled: false, next_fire_at: null, next_run: null },
  ];
}

function makeNodes(now: number): NodeRecord[] {
  return [
    {
      id: 'home-mini',
      state: 'up',
      capabilities: ['browser', 'terminal', 'service', 'android', 'gpu'],
      cpu: 22,
      mem: { used_mb: 9_216, total_mb: 16_384 },
      warm_lanes: 3,
      max_warm_lanes: 6,
      timezone: 'Europe/Vilnius',
      last_seen: new Date(now - 6_000).toISOString(),
      version: '0.1.0',
      degraded: false,
      os: 'macOS 15.1',
      arch: 'arm64',
      load: { cpu_pct: 22, mem_used_mb: 9_216, mem_total_mb: 16_384, disk_free_mb: 182_000 },
    },
    {
      id: 'hetzner-1',
      state: 'up',
      capabilities: ['browser', 'terminal', 'service'],
      cpu: 41,
      mem: { used_mb: 21_504, total_mb: 32_768 },
      warm_lanes: 10,
      max_warm_lanes: 12,
      timezone: 'Europe/Berlin',
      last_seen: new Date(now - 11_000).toISOString(),
      version: '0.1.0',
      degraded: false,
      os: 'Debian 12',
      arch: 'x86_64',
      load: { cpu_pct: 41, mem_used_mb: 21_504, mem_total_mb: 32_768, disk_free_mb: 96_000 },
    },
  ];
}

function makePeople(now: number, ids: (prefix: string) => string) {
  const ago = (ms: number) => new Date(now - ms).toISOString();
  const people: [string, string, Role, string[] | null, number][] = [
    ['Demo Owner', 'demo@agentos.local', 'owner', null, 0],
    ['Mara Lindqvist', 'mara@northwind.example', 'admin', null, 2 * HOUR],
    ['Tomas Reyes', 'tomas@northwind.example', 'member', ['aster', 'harbor'], 5 * HOUR],
    ['Ines Okafor', 'ines@northwind.example', 'approver', ['meridian'], DAY],
    ['Jun Park', 'jun@northwind.example', 'viewer', ['solstice'], 6 * DAY],
  ];
  const members: Member[] = people.map(([name, email, role, workspaces, idle], index) => ({
    id: ids('mem'),
    user: { id: index === 0 ? 'usr_DEMO' : ids('usr'), email, name },
    role,
    workspaces,
    joined_at: ago((90 - index * 12) * DAY),
    last_active_at: ago(idle),
  }));
  const invites: Invite[] = [
    {
      id: ids('inv'),
      email: 'priya@northwind.example',
      role: 'approver',
      workspaces: ['aster'],
      invited_by: 'Demo Owner',
      created_at: ago(DAY),
      expires_at: new Date(now + 6 * DAY).toISOString(),
      state: 'pending',
    },
    {
      id: ids('inv'),
      email: 'leo@harbor.example',
      role: 'viewer',
      workspaces: ['harbor'],
      invited_by: 'Mara Lindqvist',
      created_at: ago(9 * DAY),
      expires_at: ago(2 * DAY),
      state: 'expired',
    },
  ];
  return { members, invites };
}

function makeAudit(now: number, ids: (prefix: string) => string): AuditEvent[] {
  const random = rng(11);
  const seeds: [string, string, string][] = [
    ['Demo Owner', 'grant.issued', 'claude-code-aster'],
    ['Mara Lindqvist', 'member.role_changed', 'Tomas Reyes to member'],
    ['Demo Owner', 'node.enrolled', 'hetzner-1'],
    ['Mara Lindqvist', 'invite.sent', 'priya@northwind.example'],
    ['Demo Owner', 'session.step_up', 'passkey'],
    ['Mara Lindqvist', 'grant.revoked', 'old-zapier-bridge'],
    ['Demo Owner', 'workspace.created', 'solstice'],
    ['Demo Owner', 'standing_approval.created', '20 posts to aster'],
    ['Mara Lindqvist', 'member.removed', 'sam@northwind.example'],
    ['Demo Owner', 'plan.changed', 'free to team'],
  ];
  const events: AuditEvent[] = [];
  let at = now - 40 * MINUTE;
  for (let index = 0; index < 72; index += 1) {
    at -= (2 + random() * 22) * HOUR;
    const [actor, action, target] = seeds[index % seeds.length] as [string, string, string];
    events.push({
      id: ids('aud'),
      at: new Date(at).toISOString(),
      actor,
      action,
      target,
      ip: `203.0.113.${40 + (index % 6)}`,
    });
  }
  return events;
}

function makeVault(now: number, ids: (prefix: string) => string): VaultEntry[] {
  const seeds: [string, VaultEntry['kind'], string | null, string[]][] = [
    ['aster-instagram', 'password', 'instagram.com', ['aster/instagram']],
    ['aster-instagram-2fa', 'totp', 'instagram.com', ['aster/instagram']],
    ['aster-linkedin', 'password', 'linkedin.com', ['aster/linkedin']],
    ['harbor-facebook', 'password', 'facebook.com', ['harbor/facebook']],
    ['harbor-blog-cms', 'api_key', 'harbor.example', ['harbor/blog']],
    ['meridian-instagram', 'password', 'instagram.com', ['meridian/instagram']],
    ['meridian-ads', 'cookie', 'facebook.com', ['meridian/facebook']],
    ['solstice-facebook', 'password', 'facebook.com', ['solstice/facebook']],
    ['solstice-medium', 'password', 'medium.com', ['solstice/medium']],
    ['personal-mail', 'password', 'mail.example', ['personal/mail']],
  ];
  return seeds.map(([name, kind, domain, lanes], index) => ({
    id: ids('vlt'),
    name,
    kind,
    domain,
    lanes,
    created_at: new Date(now - (40 + index * 3) * DAY).toISOString(),
    last_used: index % 4 === 3 ? null : new Date(now - (index + 1) * 5 * HOUR).toISOString(),
  }));
}

function makeGrants(now: number, ids: (prefix: string) => string): Grant[] {
  const ago = (ms: number) => new Date(now - ms).toISOString();
  return [
    { id: ids('grt'), subject: 'claude-code-aster', workspaces: ['aster'], lanes: null, ceiling: 'commit', specials: [], budget: '20 posts per week', expires_at: new Date(now + 23 * DAY).toISOString(), revoked: false, created_at: ago(7 * DAY) },
    { id: ids('grt'), subject: 'claude-app-all', workspaces: ['aster', 'harbor', 'meridian', 'solstice'], lanes: null, ceiling: 'draft', specials: [], budget: null, expires_at: new Date(now + 60 * DAY).toISOString(), revoked: false, created_at: ago(30 * DAY) },
    { id: ids('grt'), subject: 'codex-personal', workspaces: ['personal'], lanes: ['personal/shell'], ceiling: 'commit', specials: ['exec'], budget: null, expires_at: null, revoked: false, created_at: ago(12 * DAY) },
    { id: ids('grt'), subject: 'old-zapier-bridge', workspaces: ['harbor'], lanes: null, ceiling: 'read', specials: [], budget: null, expires_at: null, revoked: true, created_at: ago(80 * DAY) },
  ];
}

function makeStanding(now: number, ids: (prefix: string) => string): StandingApproval[] {
  const base = { per_day: null, note: null, revoked_at: null, contents: 0 };
  return [
    {
      ...base,
      id: ids('sa'),
      workspace: 'aster',
      effect: 'commit',
      lanes: ['aster/instagram', 'aster/linkedin'],
      count: 20,
      used: 7,
      remaining: 13,
      per_day: 3,
      until: new Date(now + 12 * DAY).toISOString(),
      targets: ['flow:aster.weekly-post@4'],
      covers_publishing: true,
      note: 'The weekly roastery post, same template each time.',
      state: 'active',
      created_at: new Date(now - 18 * DAY).toISOString(),
      enforcement: notApplied(['home-mini']),
    },
    {
      ...base,
      id: ids('sa'),
      workspace: 'personal',
      effect: 'exec',
      lanes: null,
      count: 50,
      used: 50,
      remaining: 0,
      until: new Date(now + 40 * DAY).toISOString(),
      targets: ['tool:shell_run'],
      contents: 4,
      covers_publishing: false,
      state: 'used_up',
      created_at: new Date(now - 30 * DAY).toISOString(),
      enforcement: notApplied(['home-mini']),
    },
    {
      ...base,
      id: ids('sa'),
      workspace: 'harbor',
      effect: 'secret',
      lanes: ['harbor/facebook'],
      count: 10,
      used: 2,
      remaining: 8,
      until: new Date(now - 4 * DAY).toISOString(),
      targets: [],
      covers_publishing: false,
      state: 'expired',
      created_at: new Date(now - 34 * DAY).toISOString(),
      enforcement: notApplied(['hetzner-1']),
    },
  ];
}

function makeHistory(now: number, ids: (prefix: string) => string): Approval[] {
  const at = (minutes: number) => new Date(now - minutes * MINUTE).toISOString();
  return [
    {
      id: ids('ap'),
      kind: 'action',
      state: 'approved',
      effect: 'commit',
      lane: 'solstice/linkedin',
      summary: 'Publish 1 post to linkedin.com as @solsticestudio',
      content_hash: hash(301),
      content_digest: hash(302),
      flow_target: 'flow:solstice.case-study@2',
      tool: null,
      reason: 'Ask me first is on for routine solstice.case-study',
      preview: { text: 'A short note on our museum label work, with three photos from the opening.', image: null },
      requested_by: AGENTS[1] as Principal,
      created_at: at(140),
      expires_at: new Date(now + 18 * HOUR).toISOString(),
      decided_by: 'Demo Owner',
      decided_at: at(120),
      // The node did not take the decision. It still holds the approval open there.
      delivery: 'refused',
      delivery_error: 'invalid',
    },
    {
      id: ids('ap'),
      kind: 'action',
      state: 'denied',
      effect: 'commit',
      lane: 'harbor/reddit',
      summary: 'Post 1 comment to reddit.com as @harborsupply',
      content_hash: hash(303),
      content_digest: hash(304),
      flow_target: null,
      tool: 'browser_act',
      reason: 'Ask me first is on for account harbor/reddit',
      preview: { text: 'We stock that rope in 16 mm too.', image: null },
      requested_by: AGENTS[0] as Principal,
      created_at: at(400),
      expires_at: new Date(now + 10 * HOUR).toISOString(),
      decided_by: 'Mara Lindqvist',
      decided_at: at(380),
      delivery: 'delivered',
      delivery_error: null,
    },
  ].map((item) => ({ ...item, preview: { ...item.preview, image: item.preview.image ?? null } })) as Approval[];
}

function makeWebhooks(now: number, ids: (prefix: string) => string): Pick<OrgData, 'webhooks' | 'deliveries'> {
  const at = (minutes: number) => new Date(now - minutes * MINUTE).toISOString();
  const id = ids('wh');
  const deliveries: WebhookDelivery[] = [
    { id: ids('whd'), kind: 'approval.requested', state: 'delivered', attempts: 1, status: 200, error: null, next_attempt_at: null, created_at: at(9), delivered_at: at(9) },
    { id: ids('whd'), kind: 'run.failed', state: 'delivered', attempts: 2, status: 200, error: null, next_attempt_at: null, created_at: at(64), delivered_at: at(62) },
    { id: ids('whd'), kind: 'approval.decided', state: 'failed', attempts: 6, status: 502, error: 'The receiver answered 502.', next_attempt_at: null, created_at: at(26 * 60), delivered_at: null },
    { id: ids('whd'), kind: 'ping', state: 'delivered', attempts: 1, status: 204, error: null, next_attempt_at: null, created_at: at(3 * 24 * 60), delivered_at: at(3 * 24 * 60) },
  ];
  return {
    webhooks: [
      {
        id,
        origin: 'https://hooks.northwind.example',
        events: ['approval.requested', 'approval.decided', 'run.failed'],
        enabled: true,
        created_at: at(5 * 24 * 60),
        last_delivery: deliveries[0] ?? null,
      },
    ],
    deliveries: { [id]: deliveries },
  };
}

function emptyNotifications(): NotificationSettings {
  return {
    push: true,
    email_digest: 'daily',
    email_urgent: true,
    quiet_hours: { enabled: true, from: '22:00', to: '07:00', timezone: 'Europe/Vilnius', urgent_breaks_through: true },
    batch_minutes: 5,
  };
}

/** Says what the cloud says today: no node applies standing approvals. */
export function notApplied(nodes: string[]): StandingApproval['enforcement'] {
  return nodes.length
    ? { state: 'not_applied', applied_by: [], not_applied_by: nodes, message: 'No computer applies standing approvals yet. Whatever asks first will still ask each time.' }
    : { state: 'no_lanes', applied_by: [], not_applied_by: [], message: 'No node runs a lane this covers yet, so it changes nothing today.' };
}

export function emptyOrg(now: number, slug: string, name: string, role: Role, plan: string): OrgData {
  return {
    org: { id: `org_${slug.toUpperCase().replaceAll('-', '')}`, slug, name, plan, created_at: new Date(now - 3 * DAY).toISOString() },
    role,
    limits: { nodes: 1, active_lanes: 5, workspaces: 1, members: 3, retention_days: 7 },
    workspaces: [],
    lanes: [],
    approvals: [],
    questions: [],
    codes: [],
    freeze: [],
    activity: [],
    runs: [],
    flows: [],
    askFirst: [],
    account: emptyAccount(),
    schedules: [],
    nodes: [],
    joinTokens: [],
    brains: [],
    vault: [],
    ...seedAccess(true),
    grants: [],
    standing: [],
    history: [],
    pushDevices: [],
    webhooks: [],
    deliveries: {},
    retention: {
      plan,
      retention_days: 7,
      kept_days: 30,
      // A downgrade keeps what was stored under the longer window until that window ends.
      hold_until: new Date(now + 23 * DAY).toISOString(),
      oldest_at: new Date(now - 3 * DAY).toISOString(),
      can_export: role === 'owner',
    },
    members: [],
    invites: [],
    audit: [],
    notifications: emptyNotifications(),
    costToday: { tokens: 0, usd: 0 },
  };
}

export function createDb(now = Date.now()): MockDb {
  const ids = makeIds(2026);
  const slug = 'northwind-group';
  const lanes = makeLanes(now);
  const { activity, runs } = makeWork(now, ids, lanes, slug);
  const { members, invites } = makePeople(now, ids);

  const nodes = makeNodes(now);
  const flows = makeFlows(now, ids, runs);
  const approvals = makeApprovals(now, ids, slug);
  const zoneOfLane = (id: string) => nodes.find((node) => node.id === lanes.find((lane) => lane.id === id)?.node)?.timezone ?? 'UTC';
  const grants = makeGrants(now, ids);
  const northwind: OrgData = {
    ...emptyOrg(now, slug, 'Northwind Group', 'owner', 'Team'),
    limits: { nodes: 5, active_lanes: 40, workspaces: 10, members: 10, retention_days: 30 },
    workspaces: [
      ...COMPANIES.map(({ id, name, color }) => ({ id, name, color })),
      { id: 'personal', name: 'Personal', color: '#6b7280' },
    ],
    lanes,
    approvals,
    questions: makeQuestions(now, ids),
    codes: makeCodes(now, ids),
    freeze: [],
    activity,
    runs,
    flows,
    askFirst: makeAskFirst(now, approvals),
    schedules: makeSchedules(now, ids, flows, zoneOfLane),
    nodes,
    brains: [
      { name: 'claude-max', kind: 'subscription', provider: 'anthropic', state: 'ready', seats: { in_use: 1, total: 2 }, window_used_pct: 40 },
      { name: 'ollama-local', kind: 'local', provider: 'ollama', state: 'ready', seats: null, window_used_pct: null },
      { name: 'anthropic-api', kind: 'metered', provider: 'anthropic', state: 'cooling_down', seats: null, window_used_pct: 82 },
    ],
    vault: makeVault(now, ids),
    ...seedAccess(false),
    grants,
    account: makeAccount(now, grants),
    standing: makeStanding(now, ids),
    history: makeHistory(now, ids),
    pushDevices: [
      { id: ids('dev'), label: 'Safari on iPhone', endpoint: 'https://web.push.apple.com/mock-iphone', created_at: new Date(now - 9 * DAY).toISOString(), last_sent_at: new Date(now - 2 * HOUR).toISOString() },
    ],
    ...makeWebhooks(now, ids),
    retention: {
      plan: 'Team',
      retention_days: 30,
      kept_days: 30,
      hold_until: null,
      oldest_at: new Date(now - 29 * DAY - 3 * HOUR).toISOString(),
      can_export: true,
    },
    members,
    invites,
    audit: makeAudit(now, ids),
    costToday: { tokens: 184_200, usd: 1.87 },
  };

  const juniper = emptyOrg(now, 'juniper-labs', 'Juniper Labs', 'approver', 'Free');

  return {
    user: {
      id: 'usr_DEMO',
      email: 'demo@agentos.local',
      name: 'Demo Owner',
      email_verified: true,
      has_passkey: true,
      avatar_color: null,
    },
    stepUpUntil: null,
    passkeys: [
      { id: ids('pk'), name: 'MacBook Touch ID', created_at: new Date(now - 60 * DAY).toISOString(), last_used_at: new Date(now - 2 * DAY).toISOString() },
    ],
    devices: [
      { id: ids('ses'), label: 'Chrome on macOS', ip: '203.0.113.42', location: 'Vilnius', last_active_at: new Date(now).toISOString(), current: true },
      { id: ids('ses'), label: 'Safari on iPhone', ip: '198.51.100.17', location: 'Vilnius', last_active_at: new Date(now - 3 * HOUR).toISOString(), current: false },
      { id: ids('ses'), label: 'Firefox on Linux', ip: '192.0.2.88', location: null, last_active_at: new Date(now - 9 * DAY).toISOString(), current: false },
    ],
    orgs: new Map([
      [slug, northwind],
      ['juniper-labs', juniper],
    ]),
  };
}
