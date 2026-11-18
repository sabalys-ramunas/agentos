/**
 * Typed API client. Paths and bodies follow docs/API.md under the org prefix
 * (/api/orgs/{org}/..., docs/WEB-NODE.md). Nothing here, or above it, knows whether the backend is real.
 */
import { ApiError, download, http, served } from './http';
import { TURNING_OFF } from '@/lib/ask-first';
import { LOWERING_A_YES } from '@/lib/confirm';
import { CLEARING_TAINT, GIVING_A_TOKEN, LIFTING_A_FREEZE, TURNING_ON_ACCOUNT_WORKER, TURNING_ON_WORKER } from '@/lib/vocabulary';
import type {
  AccountFile,
  BrandComponents,
  BrandDecision,
  BrandProfile,
  BrandProfiles,
  BrandProposal,
  EnvCheckpointDiff,
  EnvResumeAnswer,
  EnvDetail,
  EnvList,
  EnvReplayTimeline,
  EnvSummary,
  PreparedList,
  Job,
  JobDetail,
  JobList,
  StudioBaseline,
  StudioDiff,
  StudioMissing,
  StudioRun,
  StudioRuns,
  VmList,
  AccountFiles,
  BrainHandover,
  HealthRound,
  HealthRun,
  IdentityAccount,
  NodeBackups,
  NodeEnvironments,
  NodeIdentity,
  NodeUpgrade,
  PassphraseTrip,
  ServiceLog,
  ServiceView,
  SettingRow,
  CodeRequest,
  RepoConnected,
  RepoDeployKey,
  RepoTokenForm,
  FreezeReport,
  FreezeScope,
  FreezeStatus,
  UnfreezeResult,
  WorkerAccount,
  WorkerJob,
  WorkerLimits,
  WorkerOnComputer,
  WorkerOverview,
  WorkerThing,
  ConfirmationOverlay,
  ConfirmationSetting,
  ConfirmationView,
  ConfirmLevel,
  ConfirmMethodView,
  BatchResult,
  ActivityMetrics,
  HistoryPage,
  HistoryShow,
  ActivityRange,
  Approval,
  AuditEvent,
  AuthProviders,
  Brain,
  ControlRequest,
  Device,
  FleetNode,
  Flow,
  RoutineCatalog,
  RoutineDay,
  RoutineDefinition,
  RoutineRun,
  SiteAction,
  LeadBatch,
  Mailbox,
  MailDiscovery,
  MailFormLink,
  MailFormLinkBody,
  MailFormState,
  MailList,
  MailMessage,
  MailRoutine,
  SiteApi,
  SiteApiDetail,
  SiteDiscovery,
  SiteKey,
  SiteKeyCreated,
  SiteKeyInput,
  SiteTryResult,
  SiteUsageHour,
  LaneAskFirst,
  LaneChecks,
  LaneCheck,
  LaneKeepalive,
  LanePace,
  LaneQueue,
  LaneSites,
  LaneSitesChange,
  LaneTaint,
  Limits,
  Meter,
  Pace,
  ViewLink,
  WorkspaceZone,
  Grant,
  Invite,
  GettingStarted,
  GoalKey,
  InviteJoined,
  InvitePreview,
  JoinToken,
  Lane,
  LoginQueue,
  Member,
  BrainTask,
  BrainTaskDetail,
  MemberDetail,
  Override,
  ProofDayIndex,
  ProofReplay,
  ProofShare,
  ProofShareCreated,
  ProofShareInput,
  ProofShared,
  ProofVerify,
  NewGrant,
  HealthCheck,
  NodeConnections,
  NodeDetail,
  NodeMetrics,
  NodeProgram,
  NodeRange,
  NodeSchedule,
  NodeService,
  NodeSystem,
  NewStandingApproval,
  NotificationSettings,
  PushConfig,
  PushDevice,
  Retention,
  Webhook,
  WebhookDelivery,
  WebhookEvent,
  Enforcement,
  MemoryConfirmed,
  MemoryDetail,
  MemoryForgotten,
  MemoryList,
  MemoryReviewed,
  MemoryWorkspaces,
  Org,
  Page,
  Passkey,
  Question,
  Readiness,
  Role,
  Run,
  Schedule,
  Session,
  StandingApproval,
  Status,
  ToolDef,
  Topology,
  TopologyWindow,
  TraceEvent,
  VaultEntry,
  VaultStatus,
  StartFormKind,
  FormTrip,
  LaneLogin,
  Proxy,
  BrowserDevice,
  DeviceMovePlan,
  Workspace,
} from './types';

/** A URL reads these as "here" and "up", even percent-encoded, so no id may be one. */
const DOT_SEGMENT = /^(\.|%2e){1,2}$/i;

function segment(value: string | number): string {
  const text = String(value);
  if (text === '' || DOT_SEGMENT.test(text)) throw new ApiError(400, 'invalid', 'That address is not valid.');
  return encodeURIComponent(text);
}

/**
 * Builds a path with every interpolated value encoded as one segment, so an id taken from the
 * address bar can never climb out of its place (`../..`) or add a query (security review 13).
 */
export function path(strings: TemplateStringsArray, ...values: (string | number)[]): string {
  return strings.reduce((out, text, index) => out + text + (index < values.length ? segment(values[index]!) : ''), '');
}

function query(params: Record<string, string | number | null | undefined>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== '') search.set(key, String(value));
  }
  const text = search.toString();
  return text ? `?${text}` : '';
}

export interface ActivityFilters {
  workspace?: string;
  lane?: string;
  principal?: string;
  effect?: string;
  status?: string;
  /** A window on the time axis, from dragging the histogram. */
  from?: string;
  to?: string;
}

export const auth = {
  providers: () => http<AuthProviders>('/api/auth/providers', { allowUnauthenticated: true }),
  /** The computer's own sign-in. It sets the cookie, and the session is read next as usual. */
  signInWithCode: (code: string) =>
    http<{ csrf: string; expires_at: string }>('/api/session', { method: 'POST', body: { code }, allowUnauthenticated: true }),
  sendMagicLink: (body: { email: string; aup_accepted?: boolean; next?: string }) =>
    http<void>('/api/auth/magic-link', { method: 'POST', body, allowUnauthenticated: true }),
  verifyMagicLink: (token: string) =>
    http<Session & { next: string | null }>('/api/auth/magic-link/verify', {
      method: 'POST',
      body: { token },
      allowUnauthenticated: true,
    }),
  passkeyLoginOptions: () =>
    http<PublicKeyCredentialRequestOptionsJSON>('/api/auth/passkey/login/options', {
      method: 'POST',
      allowUnauthenticated: true,
    }),
  passkeyLoginVerify: (credential: unknown) =>
    http<Session>('/api/auth/passkey/login/verify', {
      method: 'POST',
      body: credential,
      allowUnauthenticated: true,
    }),
  passkeyRegisterOptions: () =>
    http<PublicKeyCredentialCreationOptionsJSON>('/api/auth/passkey/register/options', { method: 'POST' }),
  passkeyRegisterVerify: (credential: unknown) =>
    http<Passkey>('/api/auth/passkey/register/verify', { method: 'POST', body: credential }),
  passkeys: () => http<Passkey[]>('/api/auth/passkeys'),
  deletePasskey: (id: string) => http<void>(path`/api/auth/passkeys/${id}`, { method: 'DELETE' }),
  stepUpPasskeyOptions: () =>
    http<PublicKeyCredentialRequestOptionsJSON>('/api/auth/step-up/passkey/options', { method: 'POST' }),
  stepUpPasskeyVerify: (credential: unknown) =>
    http<void>('/api/auth/step-up/passkey/verify', { method: 'POST', body: credential }),
  stepUpMagicLink: () => http<void>('/api/auth/step-up/magic-link', { method: 'POST' }),
  // The link opens in a new tab. A 401 there means another browser, which the page explains.
  stepUpVerify: (token: string) =>
    http<Session>('/api/auth/step-up/verify', { method: 'POST', body: { token }, allowUnauthenticated: true }),
  googleStartUrl: (options: { aupAccepted?: boolean; next?: string; link?: boolean }) =>
    '/api/auth/google/start' + query({ aup_accepted: options.aupAccepted ? 'true' : undefined, link: options.link ? 'true' : undefined, next: options.next }),
  session: () => http<Session>('/api/session', { allowUnauthenticated: true }),
  signOut: () => http<void>('/api/session', { method: 'DELETE' }),
  devices: () => http<Device[]>('/api/sessions'),
  revokeDevice: (id: string) => http<void>(path`/api/sessions/${id}`, { method: 'DELETE' }),
  updateMe: (body: { name?: string; avatar_color?: string }) =>
    http<Session['user']>('/api/me', { method: 'PATCH', body }),
};

export const invites = {
  preview: (token: string) =>
    http<InvitePreview>(path`/api/invites/${token}`, { allowUnauthenticated: true }),
  accept: (token: string) => http<{ org: string }>(path`/api/invites/${token}/accept`, { method: 'POST' }),
  /** One step for a new person: the link went to their address, so it signs them in and accepts. */
  join: (token: string, body: { aup_accepted: true; name?: string }) =>
    http<InviteJoined>(path`/api/invites/${token}/join`, { method: 'POST', body, allowUnauthenticated: true }),
  /** A computer's invite is accepted with a passkey: the options first, then the credential made from them. */
  passkeyOptions: (token: string) =>
    http<PublicKeyCredentialCreationOptionsJSON>(path`/api/invites/${token}/passkey/options`, { method: 'POST', allowUnauthenticated: true }),
  passkeyJoin: (token: string, credential: unknown, timezone?: string) =>
    http<Session>(path`/api/invites/${token}/passkey`, { method: 'POST', body: { credential, timezone }, allowUnauthenticated: true }),
};

export const setup = {
  gettingStarted: (org: string) => http<GettingStarted>(path`/api/orgs/${org}/getting-started`),
};

export const orgs = {
  create: (body: { name: string; slug?: string; timezone?: string; workspace?: string; goal?: GoalKey; goal_note?: string | null }) =>
    http<Org>('/api/orgs', { method: 'POST', body }),
  get: (org: string) => http<Org>(path`/api/orgs/${org}`),
  update: (org: string, body: { name?: string; slug?: string; goal?: GoalKey; goal_note?: string | null }) =>
    http<Org>(path`/api/orgs/${org}`, { method: 'PATCH', body }),
  remove: (org: string, confirm: string) =>
    http<void>(path`/api/orgs/${org}`, { method: 'DELETE', body: { confirm } }),
  status: (org: string) => http<Status>(path`/api/orgs/${org}/status`),
  members: (org: string) => http<Member[]>(path`/api/orgs/${org}/members`),
  /** One member, with the devices they are signed in on. An admin's to read. */
  member: (org: string, id: string) => served(http<MemberDetail>(path`/api/orgs/${org}/members/${id}`)),
  /** Ends one device: that sign-in stops at once. The node answers how many sessions it ended. */
  endMemberDevice: (org: string, id: string, device: string) =>
    http<{ ok: boolean; sessions_ended: number }>(path`/api/orgs/${org}/members/${id}/devices/${device}`, { method: 'DELETE' }),
  updateMember: (org: string, id: string, body: { role?: Role; workspaces?: string[] | null; workspace?: string }) =>
    http<Member>(path`/api/orgs/${org}/members/${id}`, { method: 'PATCH', body }),
  removeMember: (org: string, id: string) =>
    http<void>(path`/api/orgs/${org}/members/${id}`, { method: 'DELETE' }),
  invites: (org: string) => http<Invite[]>(path`/api/orgs/${org}/invites`),
  invite: (org: string, body: { email: string; role: Role; workspaces?: string[] | null }) =>
    http<Invite>(path`/api/orgs/${org}/invites`, { method: 'POST', body }),
  revokeInvite: (org: string, id: string) =>
    http<void>(path`/api/orgs/${org}/invites/${id}`, { method: 'DELETE' }),
  audit: (org: string, before?: string, limit = 50) =>
    http<Page<AuditEvent>>(path`/api/orgs/${org}/audit` + query({ before, limit })),
};

/** The wire allows a null lane and a null preview. Every reader gets a plain string and object. */
export type WireApproval = Omit<Approval, 'lane' | 'preview'> & { lane: string | null; preview: Approval['preview'] | null };

/** Safety checks a person turned off for an account, newest first. The owner alone (docs/API.md, Workspaces). */
export const overrides = {
  list: (org: string, limit = 50) => served(http<Override[]>(path`/api/orgs/${org}/overrides` + query({ limit }))),
};

/** What agents handed to an AI model. The owner alone; the node refuses everyone else (docs/API.md, "Brains"). */
export const brainTasks = {
  list: (org: string, limit = 50) => served(http<BrainTask[]>(path`/api/orgs/${org}/brains/tasks` + query({ limit }))),
  get: (org: string, id: string) => served(http<BrainTaskDetail>(path`/api/orgs/${org}/brains/tasks/${id}`)),
  cancel: (org: string, id: string) => http<BrainTask>(path`/api/orgs/${org}/brains/tasks/${id}/cancel`, { method: 'POST' }),
  resume: (org: string, id: string) => http<BrainTask>(path`/api/orgs/${org}/brains/tasks/${id}/resume`, { method: 'POST', body: {} }),
};

/** Proof: the action log, replayed by account and day, checked, and shared as a read-only link (docs/API.md, "Audit"). */
export const proof = {
  /** The accounts that acted on a day, with how many things each did. */
  dayIndex: (org: string, day?: string, tz?: string) =>
    served(http<ProofDayIndex>(path`/api/orgs/${org}/audit/days` + query({ day, tz }))),
  /** One account's day, oldest first. `lane` is `workspace/name`. */
  replay: (org: string, lane: string, day: string, tz?: string) => {
    const [ws = '', name = ''] = lane.split('/');
    return served(http<ProofReplay>(path`/api/orgs/${org}/audit/lanes/${ws}/${name}` + query({ day, tz })));
  },
  /** Whether the log is intact. The owner alone. */
  verify: (org: string, params: { day?: string; lane?: string; tz?: string } = {}) =>
    served(http<ProofVerify>(path`/api/orgs/${org}/audit/verify` + query(params))),
  shares: (org: string) => served(http<ProofShare[]>(path`/api/orgs/${org}/audit/shares`)),
  createShare: (org: string, body: ProofShareInput) =>
    http<ProofShareCreated>(path`/api/orgs/${org}/audit/shares`, { method: 'POST', body }),
  revokeShare: (org: string, id: string) => http<ProofShare>(path`/api/orgs/${org}/audit/shares/${id}`, { method: 'DELETE' }),
  /** Read with no sign-in: the token is the credential, and a bad one answers 404. The node is always the org `local`. */
  shared: (token: string) =>
    http<ProofShared>(path`/api/orgs/local/audit/shared/${token}`, { allowUnauthenticated: true }),
  runScreenshot: (org: string, run: string) => path`/api/orgs/${org}/audit/runs/${run}/screenshot`,
  sharedScreenshot: (token: string, run: string) =>
    path`/api/orgs/local/audit/shared/${token}/runs/${run}/screenshot`,
};

export const evenApproval = (item: WireApproval): Approval => ({ ...item, lane: item.lane ?? '', preview: item.preview ?? {} });

/**
 * What a save sends. The zone read back is the org's when the person never chose one, and sending
 * it would make it theirs, so it goes only when it already is (api-for-rebuild.md, section 4).
 */
export function notificationBody(value: NotificationSettings): NotificationSettings {
  const { timezone, timezone_source, digest: _digest, ...rest } = value;
  return timezone_source === 'person' && timezone ? { ...rest, timezone } : rest;
}

export const fleet = {
  workspaces: (org: string) => http<Workspace[]>(path`/api/orgs/${org}/workspaces`),
  /** One business, with its accounts. Anyone who sees the business. */
  workspace: (org: string, id: string) => http<Workspace>(path`/api/orgs/${org}/workspaces/${id}`),
  /** What is left to log in in one business. The owner alone. */
  workspaceQueue: (org: string, id: string) => served(http<LoginQueue>(path`/api/orgs/${org}/workspaces/${id}/queue`)),
  createWorkspace: (org: string, body: { id?: string; name: string; color?: string; goal?: GoalKey; goal_note?: string | null }) =>
    http<Workspace>(path`/api/orgs/${org}/workspaces`, { method: 'POST', body }),
  updateWorkspace: (org: string, id: string, body: { name?: string }) =>
    http<Workspace>(path`/api/orgs/${org}/workspaces/${id}`, { method: 'PATCH', body }),
  deleteWorkspace: (org: string, id: string) =>
    http<void>(path`/api/orgs/${org}/workspaces/${id}`, { method: 'DELETE' }),
  releaseLane: (org: string, ws: string, name: string) =>
    http<void>(path`/api/orgs/${org}/lanes/${ws}/${name}/release`, { method: 'POST' }),
  lane: (org: string, ws: string, name: string) =>
    http<Lane>(path`/api/orgs/${org}/lanes/${ws}/${name}`),
  createLane: (org: string, body: { id: string; kind: string; preset?: string }) =>
    http<Lane>(path`/api/orgs/${org}/lanes`, { method: 'POST', body }),
  openLane: (org: string, ws: string, name: string) =>
    http<Lane>(path`/api/orgs/${org}/lanes/${ws}/${name}/open`, { method: 'POST' }),
  closeLane: (org: string, ws: string, name: string) =>
    http<Lane>(path`/api/orgs/${org}/lanes/${ws}/${name}/close`, { method: 'POST' }),
  laneControl: (org: string, ws: string, name: string, request: ControlRequest) =>
    http<Lane>(path`/api/orgs/${org}/lanes/${ws}/${name}/control`, { method: 'POST', body: request }),
  setReadiness: (org: string, ws: string, name: string, body: { state: Readiness; note?: string }) =>
    http<void>(path`/api/orgs/${org}/lanes/${ws}/${name}/readiness`, { method: 'POST', body }),
  loginQueue: (org: string) => served(http<LoginQueue>(path`/api/orgs/${org}/queue`)),
  activityMetrics: (org: string, range: ActivityRange, scope: { workspace?: string; lane?: string }) =>
    http<ActivityMetrics>(path`/api/orgs/${org}/metrics/activity` + query({ range, ...scope })),
  nodeMetrics: (org: string, id: string, range: NodeRange) =>
    http<NodeMetrics>(path`/api/orgs/${org}/nodes/${id}/metrics` + query({ range })),
  questions: (org: string) => http<Question[]>(path`/api/orgs/${org}/questions` + query({ state: 'open' })),
  answerQuestion: (org: string, id: string, answer: string) =>
    http<Question>(path`/api/orgs/${org}/questions/${id}/answer`, { method: 'POST', body: { answer } }),
  cancelRun: (org: string, id: string) => http<Run>(path`/api/orgs/${org}/runs/${id}/cancel`, { method: 'POST' }),
  /** A run that stopped short: retry it, skip the rest, or fail it. The node says when it cannot. */
  decideRun: (org: string, id: string, decision: 'retry' | 'skip' | 'fail') =>
    http<Run>(path`/api/orgs/${org}/runs/${id}/decide`, { method: 'POST', body: { decision } }),
  setScheduleEnabled: (org: string, id: string, enabled: boolean) =>
    http<Schedule>(path`/api/orgs/${org}/schedules/${id}`, { method: 'PATCH', body: { enabled } }),

  approvals: (org: string, state = 'pending', limit = 50) =>
    http<WireApproval[]>(path`/api/orgs/${org}/approvals` + query({ state, limit })).then((items) => items.map(evenApproval)),
  /** One approval, pending or decided, with its outcome. */
  approval: (org: string, id: string) => http<WireApproval>(path`/api/orgs/${org}/approvals/${id}`).then(evenApproval),
  /** Decided approvals whose outcome failed or partly happened and nobody has cleared. */
  failedApprovals: (org: string) =>
    http<WireApproval[]>(path`/api/orgs/${org}/approvals` + query({ outcome: 'needs_you' })).then((items) => items.map(evenApproval)),
  /** Takes a decision back inside its undo window: the approval is pending again. */
  undo: (org: string, id: string) => http<WireApproval>(path`/api/orgs/${org}/approvals/${id}/undo`, { method: 'POST' }).then(evenApproval),
  /** "Got it" on a failed outcome: it leaves Needs you. */
  clearOutcome: (org: string, id: string) =>
    http<WireApproval>(path`/api/orgs/${org}/approvals/${id}/clear`, { method: 'POST' }).then(evenApproval),
  // Every decision names the content it was made on. The API refuses it if that has changed.
  approve: (org: string, id: string, content_hash: string, note?: string) =>
    http<Approval>(path`/api/orgs/${org}/approvals/${id}/approve`, { method: 'POST', body: { content_hash, note } }),
  deny: (org: string, id: string, content_hash: string, note?: string) =>
    http<Approval>(path`/api/orgs/${org}/approvals/${id}/deny`, { method: 'POST', body: { content_hash, note } }),
  decideBatch: (org: string, items: { id: string; content_hash: string }[], decision: 'approve' | 'deny') =>
    http<BatchResult[]>(path`/api/orgs/${org}/approvals/batch`, {
      method: 'POST',
      body: { ids: items.map((item) => item.id), content_hashes: Object.fromEntries(items.map((item) => [item.id, item.content_hash])), decision },
    }),

  activity: (org: string, filters: ActivityFilters, before?: string, limit = 50) =>
    http<Page<TraceEvent>>(path`/api/orgs/${org}/activity` + query({ ...filters, before, limit })),
  runs: (org: string, status?: string, limit = 50) =>
    http<Run[]>(path`/api/orgs/${org}/runs` + query({ status, limit })),
  run: (org: string, id: string) => http<Run>(path`/api/orgs/${org}/runs/${id}`),
  /** History as stories, a page of days, newest first. `before` is a day: `2026-11-03`. */
  history: (org: string, options: { workspace?: string; show?: HistoryShow; before?: string; days?: number }) =>
    http<HistoryPage>(path`/api/orgs/${org}/history` + query({ ...options, show: options.show === 'everything' ? undefined : options.show, days: options.days ? String(options.days) : undefined })),

  flows: (org: string) => http<Flow[]>(path`/api/orgs/${org}/flows`),
  routineDay: (org: string) => served(http<RoutineDay>(path`/api/orgs/${org}/routines/day`)),
  routineCatalog: (org: string) => served(http<RoutineCatalog>(path`/api/orgs/${org}/routines/catalog`)),
  mailboxes: (org: string) => served(http<Mailbox[]>(path`/api/orgs/${org}/email/mailboxes`)),
  mailDiscover: (org: string, address: string) => http<MailDiscovery>(path`/api/orgs/${org}/email/discover`, { method: 'POST', body: { address } }),
  mailMessages: (org: string) => served(http<MailMessage[]>(path`/api/orgs/${org}/email/messages`)),
  /** One message, read in full. The list carries only its clip. */
  mailMessage: (org: string, id: string) => served(http<MailMessage>(path`/api/orgs/${org}/email/messages/${id}`)),
  /** A link to the node's own form, for a mailbox typed in the panel. The password is typed there, never here. */
  mailFormLink: (org: string, body: MailFormLinkBody) => http<MailFormLink>(path`/api/orgs/${org}/email/form-links`, { method: 'POST', body }),
  /** Whether the node's form for this link is still open. Only the person who opened it can ask. */
  mailFormState: (org: string, token: string) => http<MailFormState>(path`/api/orgs/${org}/email/form-links/${token}/state`),
  mailFormEnd: (org: string, token: string) => http<{ state: 'ended' }>(path`/api/orgs/${org}/email/form-links/${token}`, { method: 'DELETE' }),
  mailMark: (org: string, id: string, read: boolean) => http<void>(path`/api/orgs/${org}/email/messages/${id}/mark`, { method: 'POST', body: { read } }),
  mailSend: (org: string, body: { mailbox: string; to: string; subject: string; body: string; reply_to?: string }) => http<void>(path`/api/orgs/${org}/email/send`, { method: 'POST', body }),
  leadBatches: (org: string) => served(http<LeadBatch[]>(path`/api/orgs/${org}/email/batches`)),
  leadBatch: (org: string, id: string) => served(http<LeadBatch>(path`/api/orgs/${org}/email/batches/${id}`)),
  decideLeadBatch: (org: string, id: string, keep: string[]) => http<{ list: string | null }>(path`/api/orgs/${org}/email/batches/${id}/decide`, { method: 'POST', body: { keep } }),
  mailLists: (org: string) => served(http<MailList[]>(path`/api/orgs/${org}/email/lists`)),
  mailList: (org: string, id: string) => served(http<MailList>(path`/api/orgs/${org}/email/lists/${id}`)),
  updateMailList: (org: string, id: string, body: { state?: MailList['state']; pace?: number }) => http<MailList>(path`/api/orgs/${org}/email/lists/${id}`, { method: 'PATCH', body }),
  mailRoutine: (org: string, id: string) => served(http<MailRoutine>(path`/api/orgs/${org}/email/routines/${id}`)),
  siteApis: (org: string) => served(http<SiteApi[]>(path`/api/orgs/${org}/site-apis`)),
  siteApi: (org: string, api: string) => served(http<SiteApiDetail>(path`/api/orgs/${org}/site-apis/${api}`)),
  siteUsage: (org: string, api: string) => served(http<SiteUsageHour[]>(path`/api/orgs/${org}/site-apis/${api}/usage`)),
  siteAction: (org: string, api: string, action: SiteAction) => http<SiteApiDetail>(path`/api/orgs/${org}/site-apis/${api}/${action.kind}`, { method: 'POST', body: action }),
  siteTry: (org: string, api: string, body: { endpoint: string; params: Record<string, string>; fresh: boolean }) => http<SiteTryResult>(path`/api/orgs/${org}/site-apis/${api}/try`, { method: 'POST', body }),
  siteKeys: (org: string) => served(http<SiteKey[]>(path`/api/orgs/${org}/site-keys`)),
  createSiteKey: (org: string, body: SiteKeyInput) => http<SiteKeyCreated>(path`/api/orgs/${org}/site-keys`, { method: 'POST', body }),
  revokeSiteKey: (org: string, id: string) => http<void>(path`/api/orgs/${org}/site-keys/${id}`, { method: 'DELETE' }),
  siteDiscovery: (org: string, lane: string) => served(http<SiteDiscovery | null>(path`/api/orgs/${org}/site-apis/discover` + query({ lane }))),
  routineRuns: (org: string, id: string) => served(http<RoutineRun[]>(path`/api/orgs/${org}/flows/${id}/runs`)),
  retryRoutineRun: (org: string, id: string, run: string) => http<RoutineRun>(path`/api/orgs/${org}/flows/${id}/runs/${run}/retry`, { method: 'POST' }),
  saveRoutine: (org: string, id: string, body: RoutineDefinition) => http<Flow>(path`/api/orgs/${org}/flows/${id}/routine`, { method: 'PUT', body }),
  /** Turns a saved routine on. The node decides whether this person may: a token never can. */
  enableRoutine: (org: string, id: string) => http<Flow>(path`/api/orgs/${org}/flows/${id}/enable`, { method: 'POST', body: {} }),
  createRoutine: (org: string, body: RoutineDefinition) => http<Flow>(path`/api/orgs/${org}/flows`, { method: 'POST', body }),
  runFlow: (org: string, id: string, params: Record<string, unknown>) =>
    http<Run>(path`/api/orgs/${org}/flows/${id}/run`, { method: 'POST', body: { params } }),
  setFlowAskFirst: (org: string, id: string, on: boolean) =>
    http<Flow>(path`/api/orgs/${org}/flows/${id}/ask-first`, { method: 'PUT', body: { ask_first: on }, stepUpReason: TURNING_OFF }),
  laneAskFirst: (org: string) => http<LaneAskFirst[]>(path`/api/orgs/${org}/ask-first`),
  setLaneAskFirst: (org: string, lane: string, on: boolean) => {
    const [workspace = '', name = ''] = lane.split('/');
    return http<LaneAskFirst>(path`/api/orgs/${org}/lanes/${workspace}/${name}/ask-first`, { method: 'PUT', body: { ask_first: on }, stepUpReason: TURNING_OFF });
  },
  // An account's page (docs/API.md). Reads go to anyone who can see the account. Every change is the
  // owner's, and a refusal comes back as the node says it. Taint is cleared on a grant's family.
  lanePace: (org: string, ws: string, name: string) => served(http<LanePace>(path`/api/orgs/${org}/lanes/${ws}/${name}/pace`)),
  setLanePace: (org: string, ws: string, name: string, body: { pace: Pace | null } | { preset: true }) =>
    http<LanePace>(path`/api/orgs/${org}/lanes/${ws}/${name}/pace`, { method: 'PUT', body }),
  workspaceZone: (org: string, ws: string) => served(http<WorkspaceZone>(path`/api/orgs/${org}/pace/workspaces/${ws}`)),
  setWorkspaceZone: (org: string, ws: string, timezone: string | null) =>
    http<WorkspaceZone>(path`/api/orgs/${org}/pace/workspaces/${ws}`, { method: 'PUT', body: { timezone } }),
  laneMeter: (org: string, ws: string, name: string) => served(http<Meter>(path`/api/orgs/${org}/lanes/${ws}/${name}/meter`)),
  setLaneLimits: (org: string, ws: string, name: string, limits: Limits | null) =>
    http<Meter>(path`/api/orgs/${org}/lanes/${ws}/${name}/meter`, { method: 'PUT', body: { limits } }),
  laneQueue: (org: string, ws: string, name: string) => served(http<LaneQueue>(path`/api/orgs/${org}/lanes/${ws}/${name}/queue`)),
  laneTaint: (org: string, ws: string, name: string) => served(http<LaneTaint>(path`/api/orgs/${org}/lanes/${ws}/${name}/taint`)),
  clearTaint: (org: string, grant: string) =>
    http<{ grant_id: string; family: string; origins_cleared: number }>(path`/api/orgs/${org}/grants/${grant}/taint/clear`, {
      method: 'POST',
      body: { confirm: 'clear' },
      stepUpReason: CLEARING_TAINT,
    }),
  laneCheck: (org: string, ws: string, name: string) => served(http<LaneCheck>(path`/api/orgs/${org}/lanes/${ws}/${name}/check`)),
  laneChecks: (org: string) => served(http<LaneChecks>(path`/api/orgs/${org}/lanes/checks`)),
  laneKeepalive: (org: string, ws: string, name: string) => served(http<LaneKeepalive>(path`/api/orgs/${org}/lanes/${ws}/${name}/keepalive`)),
  laneSites: (org: string, ws: string, name: string) => served(http<LaneSites>(path`/api/orgs/${org}/lanes/${ws}/${name}/sites`)),
  setLaneSites: (org: string, ws: string, name: string, body: { op: 'add' | 'remove' | 'set'; sites: string[]; strict?: boolean }) =>
    http<LaneSitesChange>(path`/api/orgs/${org}/lanes/${ws}/${name}/sites`, { method: 'POST', body }),
  /** A signed link to watch this account. Shown once; the node says whether it was made as the owner. */
  laneViewLink: (org: string, ws: string, name: string) =>
    http<ViewLink>(path`/api/orgs/${org}/lanes/${ws}/${name}/view-link`, { method: 'POST', body: { mode: 'view' } }),

  // Concept stage: only the mock answers these. Nothing in the cloud serves them yet.
  confirmation: (org: string) => http<ConfirmationView>(path`/api/orgs/${org}/confirmation`),
  confirmMethods: (org: string) => http<ConfirmMethodView[]>(path`/api/orgs/${org}/confirmation/methods`),
  setConfirmation: (org: string, body: { setting: ConfirmationSetting } | { business: string; overlay: ConfirmationOverlay }) =>
    http<ConfirmationView>(path`/api/orgs/${org}/confirmation`, { method: 'PUT', body, stepUpReason: LOWERING_A_YES }),
  /** The passkey challenge for one heavy yes. The browser signs it, and `proveYes` sends the answer back. */
  proveYesOptions: (org: string, id: string) =>
    http<PublicKeyCredentialRequestOptionsJSON>(path`/api/orgs/${org}/approvals/${id}/proof/options`, { method: 'POST' }),
  /** The fresh check a heavy yes spends: the passkey's answer to `proveYesOptions`, for this approval only. */
  proveYes: (org: string, id: string, credential: unknown) =>
    http<{ ok: true; level: ConfirmLevel }>(path`/api/orgs/${org}/approvals/${id}/proof`, { method: 'POST', body: credential }),
  schedules: (org: string) => http<Schedule[]>(path`/api/orgs/${org}/schedules`),
  createSchedule: (org: string, body: Omit<Schedule, 'id' | 'next_run'>) =>
    http<Schedule>(path`/api/orgs/${org}/schedules`, { method: 'POST', body }),
  deleteSchedule: (org: string, id: string) =>
    http<void>(path`/api/orgs/${org}/schedules/${id}`, { method: 'DELETE' }),

  topology: (org: string, window: TopologyWindow) => http<Topology>(path`/api/orgs/${org}/topology` + query({ window })),
  nodes: (org: string) => http<FleetNode[]>(path`/api/orgs/${org}/nodes`),
  nodeSystem: (org: string, id: string) => http<NodeSystem | null>(path`/api/orgs/${org}/nodes/${id}/system`),
  nodePrograms: (org: string, id: string) => http<NodeProgram[] | null>(path`/api/orgs/${org}/nodes/${id}/programs`),
  nodeServices: (org: string, id: string) => http<NodeService[] | null>(path`/api/orgs/${org}/nodes/${id}/services`),
  nodeTools: (org: string, id: string) => http<ToolDef[] | null>(path`/api/orgs/${org}/nodes/${id}/tools`),
  nodeHealth: (org: string, id: string) => http<HealthCheck[] | null>(path`/api/orgs/${org}/nodes/${id}/health`),
  /** Every check, probed on request. The owner's, and read-only. */
  nodeHealthRun: (org: string, id: string) => http<HealthRun>(path`/api/orgs/${org}/nodes/${id}/health/run`, { method: 'POST' }),
  nodeHealthRounds: (org: string, id: string) => served(http<HealthRound | null>(path`/api/orgs/${org}/nodes/${id}/health/rounds`)),
  nodeBackups: (org: string, id: string) => served(http<NodeBackups>(path`/api/orgs/${org}/nodes/${id}/backups`)),
  /** Starts a backup: the answer is the node's own passphrase form, which the owner fills in there. */
  startBackup: (org: string, id: string) => http<PassphraseTrip>(path`/api/orgs/${org}/nodes/${id}/backups`, { method: 'POST' }),
  checkBackup: (org: string, id: string, name: string) =>
    http<PassphraseTrip>(path`/api/orgs/${org}/nodes/${id}/backups/check`, { method: 'POST', body: { name } }),
  nodeUpgrade: (org: string, id: string) => served(http<NodeUpgrade>(path`/api/orgs/${org}/nodes/${id}/upgrade`)),
  nodeEnvironments: (org: string, id: string) => served(http<NodeEnvironments | null>(path`/api/orgs/${org}/nodes/${id}/environments`)),
  /** A service lane is `workspace/name`, sent as two segments so each is encoded on its own. */
  serviceAction: (org: string, id: string, lane: string, action: 'start' | 'stop') => {
    const [workspace = '', name = ''] = lane.split('/');
    return action === 'start'
      ? http<ServiceView>(path`/api/orgs/${org}/nodes/${id}/services/${workspace}/${name}/start`, { method: 'POST' })
      : http<ServiceView>(path`/api/orgs/${org}/nodes/${id}/services/${workspace}/${name}/stop`, { method: 'POST' });
  },
  serviceLogs: (org: string, id: string, lane: string, lines = 100) => {
    const [workspace = '', name = ''] = lane.split('/');
    return served(http<ServiceLog>(path`/api/orgs/${org}/nodes/${id}/services/${workspace}/${name}/logs` + query({ lines })));
  },
  nodeIdentity: (org: string, id: string) => served(http<NodeIdentity>(path`/api/orgs/${org}/nodes/${id}/identity`)),
  addSite: (org: string, id: string, lane: string, body: { origin: string; reason: string }) => {
    const [workspace = '', name = ''] = lane.split('/');
    return http<IdentityAccount>(path`/api/orgs/${org}/nodes/${id}/identity/${workspace}/${name}/sites`, { method: 'POST', body });
  },
  removeSite: (org: string, id: string, lane: string, origin: string) => {
    const [workspace = '', name = ''] = lane.split('/');
    return http<IdentityAccount>(path`/api/orgs/${org}/nodes/${id}/identity/${workspace}/${name}/sites` + query({ origin }), { method: 'DELETE' });
  },
  nodeSettings: (org: string, id: string) => served(http<SettingRow[]>(path`/api/orgs/${org}/nodes/${id}/settings`)),
  nodeSchedules: (org: string, id: string) => http<NodeSchedule[] | null>(path`/api/orgs/${org}/nodes/${id}/schedules`),
  nodeConnections: (org: string, id: string, range = '30d') =>
    http<NodeConnections | null>(path`/api/orgs/${org}/nodes/${id}/connections` + query({ range })),
  nodeLogsUrl: (org: string, id: string, filters: { source?: string; level?: string; q?: string; since?: string }) =>
    path`/api/orgs/${org}/nodes/${id}/logs/stream` + query(filters),
  node: (org: string, id: string) => http<NodeDetail>(path`/api/orgs/${org}/nodes/${id}`),
  removeNode: (org: string, id: string) =>
    http<void>(path`/api/orgs/${org}/nodes/${id}`, { method: 'DELETE' }),
  createJoinToken: (org: string, body: { name?: string; capabilities?: string[] }) =>
    http<JoinToken>(path`/api/orgs/${org}/nodes/join-tokens`, { method: 'POST', body }),

  brains: (org: string) => http<Brain[]>(path`/api/orgs/${org}/brains`),
  workers: (org: string) => served(http<WorkerOverview>(path`/api/orgs/${org}/workers`)),
  workersOnComputer: (org: string, node: string) => served(http<WorkerOnComputer>(path`/api/orgs/${org}/workers/computers/${node}`)),
  workerOnAccount: (org: string, lane: string) => served(http<WorkerThing | null>(path`/api/orgs/${org}/workers/things` + query({ lane }))),
  setWorkerPlace: (org: string, node: string, on: boolean) =>
    http<WorkerOverview>(path`/api/orgs/${org}/workers/computers/${node}`, { method: 'POST', body: { on }, stepUpReason: on ? TURNING_ON_WORKER : undefined }),
  startWorkerThing: (org: string, body: { lane: string; told: string }) => http<WorkerThing>(path`/api/orgs/${org}/workers/things`, { method: 'POST', body }),
  workerAccount: (org: string, lane: string) => served(http<WorkerAccount | null>(path`/api/orgs/${org}/workers/accounts` + query({ lane }))),
  setWorkerAccount: (org: string, lane: string, on: boolean) =>
    http<WorkerAccount>(path`/api/orgs/${org}/workers/accounts`, { method: 'POST', body: { lane, on }, stepUpReason: on ? TURNING_ON_ACCOUNT_WORKER : undefined }),
  setWorkerJob: (org: string, job: WorkerJob, brain: string) => http<WorkerOverview>(path`/api/orgs/${org}/workers/jobs/${job}`, { method: 'POST', body: { brain } }),
  setWorkerLimits: (org: string, limits: Partial<WorkerLimits>) => http<WorkerOverview>(path`/api/orgs/${org}/workers/limits`, { method: 'POST', body: limits }),
  /** Says which service and which computer. The key itself goes from the person to that computer. */
  startHandover: (org: string, body: { service: string; computer: string }) => http<BrainHandover>(path`/api/orgs/${org}/brains/handovers`, { method: 'POST', body }),
  handover: (org: string, id: string) => served(http<BrainHandover>(path`/api/orgs/${org}/brains/handovers/${id}`)),
  /** Concept stage: the drawn form stands in for the computer saying it was used. No key is in it. */
  handoverFilled: (org: string, id: string) => http<BrainHandover>(path`/api/orgs/${org}/brains/handovers/${id}/filled`, { method: 'POST' }),
  setBrainOrder: (org: string, order: string[]) =>
    http<void>(path`/api/orgs/${org}/brains/order`, { method: 'POST', body: { order } }),
  vault: (org: string) => http<VaultEntry[]>(path`/api/orgs/${org}/vault`),
  vaultStatus: (org: string) => served(http<VaultStatus>(path`/api/orgs/${org}/vault/status`)),
  lockVault: (org: string) => http<VaultStatus>(path`/api/orgs/${org}/vault/seal`, { method: 'POST' }),
  removeVaultEntry: (org: string, id: string) => http<{ ok: boolean }>(path`/api/orgs/${org}/vault/${id}`, { method: 'DELETE' }),
  // A form the computer serves for a secret: the app starts the trip and opens its form_url, and never sees the value.
  startForm: (org: string, body: { kind: StartFormKind; fields?: Record<string, string> }) =>
    http<FormTrip>(path`/api/orgs/${org}/forms`, { method: 'POST', body }),
  formTrip: (org: string, id: string) => served(http<FormTrip>(path`/api/orgs/${org}/forms/trips/${id}`)),
  browserDevices: (org: string) => served(http<BrowserDevice[]>(path`/api/orgs/${org}/browser-devices`)),
  createBrowserDevice: (org: string, body: { name: string; proxy_label?: string | null; from_lane?: string | null; note?: string | null; confirm?: boolean }) =>
    http<BrowserDevice>(path`/api/orgs/${org}/browser-devices`, { method: 'POST', body }),
  removeBrowserDevice: (org: string, name: string) =>
    http<{ removed: string; trash: string | null }>(path`/api/orgs/${org}/browser-devices/${name}`, { method: 'DELETE' }),
  deviceMovePlan: (org: string, ws: string, name: string, to: string | null) =>
    served(http<DeviceMovePlan>(path`/api/orgs/${org}/lanes/${ws}/${name}/device-move` + query({ to: to ?? 'none' }))),
  moveLaneDevice: (org: string, ws: string, name: string, body: { device: string | null; confirm?: boolean }) =>
    http<{ plan: DeviceMovePlan }>(path`/api/orgs/${org}/lanes/${ws}/${name}/device`, { method: 'POST', body }),
  proxies: (org: string) => http<Proxy[]>(path`/api/orgs/${org}/proxies`),
  createProxy: (org: string, body: { label: string; host: string; port: number; username?: string | null; confirm?: boolean }) =>
    http<Proxy>(path`/api/orgs/${org}/proxies`, { method: 'POST', body }),
  laneLogin: (org: string, ws: string, name: string) => served(http<LaneLogin>(path`/api/orgs/${org}/lanes/${ws}/${name}/login`)),
  startLaneLogin: (org: string, ws: string, name: string) =>
    http<FormTrip & { login: LaneLogin }>(path`/api/orgs/${org}/lanes/${ws}/${name}/login/form`, { method: 'POST', body: {} }),
  // The saved login signs the lane in, with no form. The node checks the entry's binding itself.
  laneLoginVault: (org: string, ws: string, name: string) =>
    http<LaneLogin>(path`/api/orgs/${org}/lanes/${ws}/${name}/login/vault`, { method: 'POST', body: {} }),
  laneLoginAction: (org: string, ws: string, name: string, action: 'refresh' | 'take' | 'resume' | 'cancel') =>
    http<LaneLogin>(path`/api/orgs/${org}/lanes/${ws}/${name}/login/${action}`, { method: 'POST', body: action === 'cancel' ? { done: false } : {} }),

  grants: (org: string) => http<Grant[]>(path`/api/orgs/${org}/grants`),
  createGrant: (org: string, body: NewGrant) =>
    http<{ id: string; token: string }>(path`/api/orgs/${org}/grants`, { method: 'POST', body }),
  revokeGrant: (org: string, id: string) =>
    http<void>(path`/api/orgs/${org}/grants/${id}/revoke`, { method: 'POST' }),

  standingApprovals: (org: string, all = false) =>
    http<StandingApproval[]>(path`/api/orgs/${org}/standing-approvals` + query({ all: all ? 'true' : undefined })),
  standingSupport: (org: string, workspace: string) =>
    http<Enforcement>(path`/api/orgs/${org}/standing-approvals/support` + query({ workspace })),
  createStandingApproval: (org: string, body: NewStandingApproval) =>
    http<StandingApproval>(path`/api/orgs/${org}/standing-approvals`, { method: 'POST', body }),
  deleteStandingApproval: (org: string, id: string) =>
    http<void>(path`/api/orgs/${org}/standing-approvals/${id}`, { method: 'DELETE' }),

  notifications: (org: string) =>
    http<NotificationSettings>(path`/api/orgs/${org}/settings/notifications`),
  saveNotifications: (org: string, body: NotificationSettings) =>
    http<NotificationSettings>(path`/api/orgs/${org}/settings/notifications`, { method: 'PUT', body: notificationBody(body) }),
  testNotification: (org: string) =>
    http<void>(path`/api/orgs/${org}/settings/notifications/test`, { method: 'POST' }),

  pushConfig: (org: string) => http<PushConfig>(path`/api/orgs/${org}/push/config`),
  pushDevices: (org: string) => http<PushDevice[]>(path`/api/orgs/${org}/push/devices`),
  addPushDevice: (org: string, body: { endpoint: string; keys: { p256dh: string; auth: string } }) =>
    http<PushDevice>(path`/api/orgs/${org}/push/devices`, { method: 'POST', body }),
  removePushDevice: (org: string, id: string) =>
    http<void>(path`/api/orgs/${org}/push/devices/${id}`, { method: 'DELETE' }),
  testPush: (org: string) => http<{ devices: number }>(path`/api/orgs/${org}/push/test`, { method: 'POST' }),

  /** Where a Review link lands. It says which approval the link meant; it decides nothing. */
  review: (org: string, id: string, e: string, s: string) =>
    http<WireApproval>(path`/api/orgs/${org}/approvals/${id}/review` + query({ e, s })).then(evenApproval),

  webhooks: (org: string) => http<Webhook[]>(path`/api/orgs/${org}/webhooks`),
  createWebhook: (org: string, body: { url: string; events: WebhookEvent[] }) =>
    http<Webhook & { secret: string }>(path`/api/orgs/${org}/webhooks`, { method: 'POST', body }),
  updateWebhook: (org: string, id: string, body: { enabled?: boolean; events?: WebhookEvent[] }) =>
    http<Webhook>(path`/api/orgs/${org}/webhooks/${id}`, { method: 'PATCH', body }),
  deleteWebhook: (org: string, id: string) => http<void>(path`/api/orgs/${org}/webhooks/${id}`, { method: 'DELETE' }),
  rotateWebhook: (org: string, id: string) =>
    http<{ secret: string }>(path`/api/orgs/${org}/webhooks/${id}/rotate`, { method: 'POST' }),
  testWebhook: (org: string, id: string) =>
    http<{ id: string }>(path`/api/orgs/${org}/webhooks/${id}/test`, { method: 'POST' }),
  webhookDeliveries: (org: string, id: string, limit = 50) =>
    http<WebhookDelivery[]>(path`/api/orgs/${org}/webhooks/${id}/deliveries` + query({ limit })),

  retention: (org: string) => http<Retention>(path`/api/orgs/${org}/retention`),
  exportActivity: (org: string, options: { format: 'csv' | 'ndjson'; start?: string; end?: string; workspace?: string }) =>
    download(path`/api/orgs/${org}/activity/export` + query(options)),

  billingPortal: (org: string) =>
    http<{ url: string }>(path`/api/orgs/${org}/billing/portal`, { method: 'POST' }),

  /** What is frozen on this computer (docs/API.md, "Freeze (break glass)"). Any signed-in caller. */
  freeze: (org: string) => served(http<FreezeStatus>(path`/api/orgs/${org}/freeze`)),
  /** Freezes a lane, a workspace or this computer. The reason is required by the node. */
  freezeNow: (org: string, body: { scope: Exclude<FreezeScope, 'mesh'>; target?: string; reason: string }) =>
    http<FreezeReport>(path`/api/orgs/${org}/freeze`, { method: 'POST', body }),
  /** The owner, signed in. Nothing is restored, so the step-up says so before the press. */
  unfreeze: (org: string, body: { scope: FreezeScope; target?: string }) =>
    http<UnfreezeResult>(path`/api/orgs/${org}/unfreeze`, { method: 'POST', body, stepUpReason: LIFTING_A_FREEZE }),

  /** The open code cards. Owner only: a member or a grant is refused. */
  codes: (org: string) => served(http<CodeRequest[]>(path`/api/orgs/${org}/codes`)),
  /** The one code, once. Its request is answered with the card, which never holds the code. */
  enterCode: (org: string, id: string, code: string) =>
    http<CodeRequest>(path`/api/orgs/${org}/codes/${id}`, { method: 'POST', body: { code } }),
  declineCode: (org: string, id: string) =>
    http<CodeRequest>(path`/api/orgs/${org}/codes/${id}/decline`, { method: 'POST' }),

  /**
   * A repository card: the link to the computer's own page for the token. The owner in person, with the usual step-up.
   * The token is typed on that page, never here, and the app waits on the trip (`formTrip`).
   */
  repoTokenForm: (org: string, id: string) =>
    http<RepoTokenForm>(path`/api/orgs/${org}/repo-connect/${id}/form`, { method: 'POST', stepUpReason: GIVING_A_TOKEN }),
  /** The node's deploy key for the card's repository (made the first time), for an admin of it to add. */
  repoDeployKey: (org: string, id: string) => http<RepoDeployKey>(path`/api/orgs/${org}/repo-connect/${id}/deploy-key`, { method: 'POST' }),
  /** "I added it, check": can the node read the repository now? */
  checkRepo: (org: string, id: string) => http<RepoConnected>(path`/api/orgs/${org}/repo-connect/${id}/check`, { method: 'POST' }),
  declineRepo: (org: string, id: string) => http<{ repo: string; declined: true }>(path`/api/orgs/${org}/repo-connect/${id}/decline`, { method: 'POST' }),

  eventsUrl: (org: string) => path`/api/orgs/${org}/events`,
};

/**
 * An account's files on this computer: its inbox (what agents and people put in) and its outbox (what its
 * browser downloaded). Reads need `see` in the account's workspace, changes need `operate`, and a person in
 * the panel does them. A file is only stored, and a download is an attachment, never shown inline.
 */
export const accountFiles = {
  list: (org: string, ws: string, name: string) => served(http<AccountFiles>(path`/api/orgs/${org}/lanes/${ws}/${name}/files`)),
  /** The bytes as they are, with their own type. The node sniffs them, and refuses what the inbox does not take. */
  upload: (org: string, ws: string, name: string, file: Blob, fileName: string) =>
    http<{ lane: string; file: AccountFile }>(path`/api/orgs/${org}/lanes/${ws}/${name}/files` + query({ name: fileName }), {
      method: 'POST',
      body: file,
    }),
  download: (org: string, ws: string, name: string, id: string) =>
    download(path`/api/orgs/${org}/lanes/${ws}/${name}/files/${id}/download`),
  remove: (org: string, ws: string, name: string, id: string) =>
    http<{ lane: string; deleted: AccountFile }>(path`/api/orgs/${org}/lanes/${ws}/${name}/files/${id}`, { method: 'DELETE' }),
};

/**
 * What agents remember about each workspace's work. Only the owner in person reads and changes it: a token
 * and a member are refused by the node, and the screens never offer what they could not do.
 */
export const memory = {
  workspaces: (org: string) => served(http<MemoryWorkspaces>(path`/api/orgs/${org}/memory/workspaces`)),
  /** Every workspace with memory, when `workspace` is left out. */
  list: (org: string, options: { workspace?: string; q?: string; type?: string; trust?: string; unreviewed?: boolean; limit?: number }) =>
    served(http<MemoryList>(path`/api/orgs/${org}/memory` + query({ ...options, unreviewed: options.unreviewed ? '1' : undefined }))),
  entry: (org: string, id: string, workspace: string, version?: number) =>
    served(http<MemoryDetail>(path`/api/orgs/${org}/memory/${id}` + query({ workspace, version }))),
  confirm: (org: string, id: string, body: { workspace: string; version: number; title?: string; body?: string }) =>
    http<MemoryConfirmed>(path`/api/orgs/${org}/memory/${id}/confirm`, { method: 'POST', body }),
  review: (org: string, id: string, workspace: string) =>
    http<MemoryReviewed>(path`/api/orgs/${org}/memory/${id}/review`, { method: 'POST', body: { workspace } }),
  /** Erases the entry's content in every version. The reason is kept; the content is not. */
  forget: (org: string, id: string, workspace: string, reason: string) =>
    http<MemoryForgotten>(path`/api/orgs/${org}/memory/${id}/forget`, { method: 'POST', body: { workspace, reason } }),
  exportFile: (org: string, workspace: string | undefined, format: 'jsonl' | 'md') =>
    download(path`/api/orgs/${org}/memory/export` + query({ workspace, format })),
};

/**
 * Runs: commands started with `agentos run`, across the lanes the caller sees. Reading needs `see`, a
 * cancel needs `operate`, and the node checks the lane against the caller's grant.
 */
export const jobs = {
  list: (org: string, options: { lane?: string; state?: string; limit?: number } = {}) =>
    served(http<JobList>(path`/api/orgs/${org}/jobs` + query(options))),
  /** One run with its output after these offsets. `wait_ms` waits up to that long for more while it runs. */
  get: (org: string, id: string, options: { stdout_offset?: number; stderr_offset?: number; wait_ms?: number } = {}) =>
    served(http<JobDetail>(path`/api/orgs/${org}/jobs/${id}` + query(options))),
  cancel: (org: string, id: string) => http<Job>(path`/api/orgs/${org}/jobs/${id}/cancel`, { method: 'POST', body: {} }),
};

/**
 * Checks: what studio found on a page. A list answers `available: false` when studio is not installed; one
 * run, an image or a diff then answers the `unavailable` error with the command that adds it.
 */
export const studio = {
  runs: (org: string, options: { url?: string; profile?: string; limit?: number } = {}) =>
    served(http<StudioRuns | StudioMissing>(path`/api/orgs/${org}/studio/runs` + query(options))),
  run: (org: string, id: string) => served(http<StudioRun>(path`/api/orgs/${org}/studio/runs/${id}`)),
  baseline: (org: string, url: string, profile?: string) =>
    served(http<StudioBaseline>(path`/api/orgs/${org}/studio/baseline` + query({ url, profile }))),
  /** Two stored captures compared, with no browser: the causes, one sentence each. */
  diff: (org: string, before: string, after: string) =>
    served(http<StudioDiff>(path`/api/orgs/${org}/studio/diff` + query({ before, after }))),
  /** Component use and look-alikes of a product profile, from its newest stored system check. */
  components: (org: string, ws: string, id: string) =>
    served(http<BrandComponents | StudioMissing>(path`/api/orgs/${org}/studio/profiles/${ws}/${id}/components`)),
};

/** The address of a screen's screenshot, for an `<img>`: sized, and cached by its content. */
export function studioShotUrl(org: string, capture: string, screen: string, maxWidth?: number): string {
  return path`/api/orgs/${org}/studio/captures/${capture}/screens/${screen}` + query({ max_width: maxWidth });
}

/** The address of a finding's crop, outlined with the page around it, for an `<img>`. */
export function studioCropUrl(org: string, run: string, finding: string, screen?: string): string {
  return path`/api/orgs/${org}/studio/runs/${run}/findings/${finding}/crop` + query({ screen });
}

/**
 * Brand: product and business profiles. The owner in person reads and decides them; anyone else is refused
 * by the node, `proposals` included (useBrandProposals reads that refusal as none waiting).
 */
export const brand = {
  list: (org: string, workspace?: string) => served(http<BrandProfiles>(path`/api/orgs/${org}/brand` + query({ workspace }))),
  get: (org: string, ws: string, id: string) => served(http<BrandProfile>(path`/api/orgs/${org}/brand/${ws}/${id}`)),
  /** Waiting brand drafts, in a shape Needs you lists beside approvals. */
  proposals: (org: string) => served(http<BrandProposal[]>(path`/api/orgs/${org}/brand/proposals`)),
  /** Always the version the owner read: never "the latest". */
  approve: (org: string, ws: string, id: string, version: number) =>
    http<BrandDecision>(path`/api/orgs/${org}/brand/${ws}/${id}/approve`, { method: 'POST', body: { version } }),
  reject: (org: string, ws: string, id: string, version: number, note?: string) =>
    http<BrandDecision>(path`/api/orgs/${org}/brand/${ws}/${id}/reject`, { method: 'POST', body: { version, note } }),
};

/** Disposable cloud machines this computer rented, read only. */
export const vms = {
  list: (org: string) => served(http<VmList>(path`/api/orgs/${org}/vms`)),
};

/**
 * Environments: the cloud computers agents rent for one chat (docs/design/environments.md). Reading needs
 * `see` in workspace `env`, close and extend `operate`. A close with unsaved work answers 409 `unsaved_work`
 * unless it says `discard`.
 */
export const environments = {
  list: (org: string, state: 'open' | 'all' = 'all') => served(http<EnvList>(path`/api/orgs/${org}/environments` + query({ state }))),
  get: (org: string, id: string) => served(http<EnvDetail>(path`/api/orgs/${org}/environments/${id}`)),
  close: (org: string, id: string, discard = false) =>
    http<EnvSummary>(path`/api/orgs/${org}/environments/${id}/close`, { method: 'POST', body: { discard } }),
  extend: (org: string, id: string, minutes: number) =>
    http<EnvSummary>(path`/api/orgs/${org}/environments/${id}/extend`, { method: 'POST', body: { minutes } }),
  /** Forget a kept environment's save: its snapshot and files are deleted. The owner in person. */
  forget: (org: string, saveId: string) => http<{ forgot?: boolean; snapshots_deleted?: number }>(path`/api/orgs/${org}/environments/saves/${saveId}`, { method: 'DELETE' }),
  /** The replay of its browser: frames and calls by time (agentos.environments.replay). The owner only. */
  replay: (org: string, id: string) => served(http<EnvReplayTimeline>(path`/api/orgs/${org}/environments/${id}/replay` + query({ limit: '2000' }))),
  /** One checkpoint against the work tree now: per repository its files and patch (capped). The owner only. */
  checkpointDiff: (org: string, id: string, checkpoint: number) =>
    served(http<EnvCheckpointDiff>(path`/api/orgs/${org}/environments/${id}/checkpoints/${String(checkpoint)}`)),
  /** The prepared machines, the warm pool and what both cost. The owner only. */
  prepared: (org: string) => served(http<PreparedList>(path`/api/orgs/${org}/environments/prepared`)),
  /**
   * Resume a save from the app, held by no chat: the next agent that opens its title takes it over. The owner
   * in person. 409 `environment_limit` or `environment_full` with the fix in the message.
   */
  resume: (org: string, saveId: string) => http<EnvResumeAnswer>(path`/api/orgs/${org}/environments/saves/${saveId}/resume`, { method: 'POST' }),
};
