/** The mock backend. Same routes, shapes, errors and step-up rules as the real API. */
import { delay, http, HttpResponse } from 'msw';
import type {
  LaneKind,
  ActivityRange,
  Approval,
  CodeRequest,
  ControlAction,
  Effect,
  FleetNode,
  FormTrip,
  FreezeRecord,
  FreezeReport,
  FreezeScope,
  FreezeStatus,
  Health,
  Lane,
  LaneControl,
  Limits,
  LoginQueue,
  Member,
  NewGrant,
  Pace,
  NodeRange,
  NotificationSettings,
  Org,
  Readiness,
  Role,
  RoutineDefinition,
  Schedule,
  Session,
  Status,
  Workspace,
} from '@/api/types';
import { idleControl, needsHand } from '@/lib/lanes';
import { laneScreenshot, postPreview } from './art';
import { emit } from './bus';
import {
  laneShare,
  nodeConnections,
  nodeHealth,
  nodePrograms,
  nodeSchedules,
  nodeServices,
  nodeSystem,
  nodeTools,
  reportsDepth,
  topology,
} from './fleet';
import { confirmationHandlers, spendProof } from './confirmations';
import { ALWAYS_MESSAGE, applyAsk, CEILING_MESSAGE, isOnline, isSupported, laneAsk, mayTurn, NOT_SUPPORTED_MESSAGE, OFFLINE_MESSAGE, refusesTurningOff, withAsk } from './ask-first';
import { previewNeedYou } from './fleet-preview';
import { historyPage } from './history';
import {
  checkBriefs,
  clearTaint,
  laneCheck,
  laneKeepalive,
  laneMeter,
  laneQueue,
  laneSites,
  laneTaint,
  lanePace,
  setLaneLimits,
  setLanePace,
  setLaneSites,
  viewLink,
  withClaim,
  workspaceZone,
} from './account';
import { activityMetrics, nodeMetrics, spark } from './metrics';
import { retryRun, routineCatalog, routineDay, routineRuns, saveRoutine, withRoutine } from './routines';
import { mockAuth, scenario } from './scenario';
import { s3Handlers } from './s3';
import { setupHandlers } from './setup';
import { emailHandlers } from './email';
import { filesHandlers } from './files';
import { memoryHandlers } from './memory';
import { siteApiHandlers } from './site-apis';
import { accessHandlers } from './access';
import { workerBrains, workerHandlers } from './workers';
import { opsHandlers } from './ops';
import { proofHandlers } from './proof';
import { brainTaskHandlers } from './brain-tasks';
import { brandHandlers } from './brand';
import { environmentHandlers, withEnvironments } from './environments';
import { jobHandlers, mockJobs } from './jobs';
import { studioHandlers } from './studio';
import { overrideHandlers } from './overrides';
import { memberDeviceHandlers } from './member-devices';
import { createDb, emptyOrg, makeIds } from './seed';
import type { MockDb, NodeRecord, OrgData } from './seed';
import { needsProof } from '@/lib/confirm';
import { coveringFreeze, NO_REASON } from '@/lib/freeze';
import { principalId } from '@/lib/principal';
import { withWorkLanes } from './work-lanes';
import { withMachines } from './machines';
import { doneOutcome, homeFields, homeSummary, outcomeOf, withHealth, withPictures, withPlace, withZone } from './rebuild';

/** `?mock=outage` opens the panel with hetzner-1 down, for the map's blast radius and the node page. */
function withScenario(next: MockDb): MockDb {
  withWorkLanes(next);
  withEnvironments(next);
  withMachines(next);
  if (scenario.has('all-good')) allGood(next);
  if (!scenario.has('outage')) return next;
  const node = next.orgs.get('northwind-group')?.nodes.find((item) => item.id === 'hetzner-1');
  if (node) Object.assign(node, { state: 'down', last_seen: new Date(Date.now() - 38 * 60_000).toISOString(), cpu: 0, warm_lanes: 0 });
  return next;
}

/** Who may change a switch, in the cloud's order: the role, then the step-up to turn it off. */
function askFirstRefusal(data: OrgData, on: boolean) {
  if (!mayTurn(data.role, on)) return failure(403, 'forbidden', 'You do not have access to do that.');
  return !on && !stepUpValid() ? stepUpNeeded() : null;
}

/** What the computer itself says: it has no switches, it is away, or it refuses the cloud. */
function computerRefusal(data: OrgData, lane: string, on: boolean) {
  const node = data.nodes.find((item) => item.id === data.lanes.find((candidate) => candidate.id === lane)?.node);
  if (!isSupported(node)) return failure(409, 'unsupported', NOT_SUPPORTED_MESSAGE);
  if (!isOnline(node)) return failure(503, 'unavailable', OFFLINE_MESSAGE);
  return !on && refusesTurningOff() ? failure(403, 'forbidden', CEILING_MESSAGE) : null;
}

/** `?mock=all-good` is a calm day: nothing waits, every account is fine, for Home's All good. */
function allGood(next: MockDb) {
  const data = next.orgs.get('northwind-group');
  if (!data) return;
  data.approvals = [];
  data.questions = [];
  for (const lane of data.lanes) {
    Object.assign(lane, { signal: 'ok', quarantine_reason: null, hold: null, hold_until: null, signal_detail: null });
    if (lane.state === 'quarantined') lane.state = 'warm';
    if (lane.account?.status === 'wrong_account') lane.account = { ...lane.account, status: 'match' };
    lane.control = { ...lane.control, state: 'agent', takeback_requested: false, takeback_reason: null, takeback_by: null };
  }
}

export let db: MockDb = withScenario(createDb());

export function resetDb() {
  mockTiming.undoMs = 6_000;
  mockTiming.runMs = 1_500;
  for (const timer of releases.values()) clearTimeout(timer);
  releases.clear();
  db = withScenario(createDb());
}

const newId = makeIds(Date.now() % 100000);
const iso = (offset = 0) => new Date(Date.now() + offset).toISOString();

function failure(status: number, code: string, message: string, extra: Record<string, unknown> = {}) {
  return HttpResponse.json({ error: { code, message }, ...extra }, { status });
}

const notFound = () => failure(404, 'not_found', 'Not found.');
const denied = () => failure(403, 'denied', 'Only the owner changes this. A grant or a member cannot.');

/** An account named in the path, or the node's not-found for one it does not hold. */
function onLane(data: OrgData, params: Record<string, unknown>, answer: (lane: Lane) => Response): Response {
  const lane = data.lanes.find((item) => item.id === `${params.ws}/${params.name}`);
  return lane ? answer(lane) : failure(404, 'lane_not_found', 'No such lane.');
}
const stepUpNeeded = () => failure(403, 'step_up_required', 'Confirm it is you to continue.');

function stepUpValid() {
  return db.stepUpUntil !== null && new Date(db.stepUpUntil).getTime() > Date.now();
}

function grantStepUp() {
  db.stepUpUntil = iso(10 * 60_000);
}

function session(): Session {
  return {
    user: db.user,
    csrf: 'mock-csrf-token',
    step_up_until: db.stepUpUntil,
    expires_at: iso(14 * 86_400_000),
    orgs: [...db.orgs.values()].map((data) => ({
      id: data.org.id,
      slug: data.org.slug,
      name: data.org.name,
      role: data.role,
      plan: data.org.plan,
      needs_you: needsYou(data),
    })),
  };
}

/** As the cloud counts it: what waits for an answer, what did not work after a yes, and accounts that need a hand. */
function needsYou(data: OrgData) {
  const failed = data.history.filter((item) => (item.outcome?.state === 'failed' || item.outcome?.state === 'partial') && !item.outcome.cleared_at);
  return data.approvals.length + data.questions.length + openCodes(data).length + failed.length + data.lanes.filter(needsHand).length;
}

/** Login codes are the owner's: nobody else is shown one, so nobody else counts one. */
function openCodes(data: OrgData): CodeRequest[] {
  return data.role === 'owner' ? data.codes.filter((card) => card.state === 'open') : [];
}

function workspaceHealth(lanes: Lane[]): Health {
  if (lanes.some((lane) => lane.signal === 'blocked' || lane.state === 'quarantined')) return 'blocked';
  if (lanes.some(needsHand)) return 'attention';
  if (lanes.length === 0 || lanes.every((lane) => lane.state === 'cold')) return 'idle';
  return 'ok';
}

function workspaceSummary(data: OrgData, id: string, lanes: Lane[]) {
  const count = `${lanes.length} ${lanes.length === 1 ? 'lane' : 'lanes'}`;
  if (lanes.length === 0) return 'No lanes yet';
  const hands = lanes.filter(needsHand);
  const scheduled = data.schedules.filter((item) => {
    if (!item.flow.startsWith(`${id}.`) || !item.next_run) return false;
    return new Date(item.next_run).getTime() - Date.now() < 86_400_000;
  }).length;
  const today = scheduled ? `, ${scheduled} scheduled today` : '';
  if (hands.length) {
    const counted = (test: (lane: Lane) => boolean, word: string) => {
      const number = hands.filter(test).length;
      return number ? `${number} ${word}` : '';
    };
    const parts = [
      counted((lane) => lane.signal === 'captcha', 'captcha'),
      counted((lane) => lane.signal === 'checkpoint', 'checkpoint'),
      counted((lane) => lane.signal === 'logged_out', 'logged out'),
      counted((lane) => lane.account?.status === 'wrong_account', 'wrong account'),
    ].filter(Boolean);
    return `${[count, ...parts].join(', ')}${today}`;
  }
  if (lanes.every((lane) => lane.state === 'cold')) return `${count}, sleeping`;
  return `${count}, all ok${today}`;
}

function workspaces(data: OrgData): Workspace[] {
  return data.workspaces.map((record) => {
    const lanes = data.lanes.filter((lane) => lane.workspace === record.id).map((lane) => withPictures(data, lane));
    return {
      ...record,
      ...homeFields(data, record.id, lanes),
      health: workspaceHealth(lanes),
      summary: workspaceSummary(data, record.id, lanes),
      lanes,
      spark: spark(data, record.id),
    };
  });
}

/** A depth route. A node on an older build refuses it, and the panel says so truthfully. */
function nodeReport(part: string, read: (data: OrgData, node: NodeRecord) => unknown) {
  return [
    http.get(
      `/api/orgs/:org/nodes/:id/${part}`,
      org((data, { params }) => {
        const node = data.nodes.find((item) => item.id === params.id);
        if (!node) return notFound();
        if (!reportsDepth(node)) return failure(409, 'unsupported', 'This node does not report this yet.');
        return HttpResponse.json(read(data, node) as never);
      }),
    ),
  ];
}

/** What a computer is doing now, as the node tells it: runs going, lanes by kind and busy, approvals waiting. */
function nodeBusy(data: OrgData, nodeId: string): Pick<FleetNode, 'jobs_running' | 'lanes_by_kind' | 'lanes_active' | 'approvals_waiting'> {
  const lanes = data.lanes.filter((lane) => lane.node === nodeId);
  const byKind: Partial<Record<LaneKind, number>> = {};
  for (const lane of lanes) byKind[lane.kind] = (byKind[lane.kind] ?? 0) + 1;
  return {
    jobs_running: mockJobs(data).filter((item) => item.job.node === nodeId && item.job.state === 'running').length,
    lanes_by_kind: byKind,
    lanes_active: lanes.filter((lane) => lane.state === 'active').length,
    approvals_waiting: data.approvals.filter((approval) => approval.state === 'pending').length,
  };
}

function nodeResource(node: NodeRecord): FleetNode {
  const { os: _os, arch: _arch, load: _load, ...rest } = node;
  return rest;
}

function status(data: OrgData): Status {
  const brain = data.brains[0];
  const preview = scenario.has('preview-3') ? 3 : scenario.has('preview-120') ? 120 : 0;
  return {
    node: data.nodes[0]?.id ?? '',
    version: '0.1.0',
    csrf: 'mock-csrf-token',
    needs_you: preview ? previewNeedYou(preview) : needsYou(data),
    needs_you_detail: {
      approvals: data.approvals.length,
      questions: data.questions.length,
      lanes: data.lanes.filter(needsHand).map((lane) => lane.id),
      codes: openCodes(data).length,
    },
    timezone: data.nodes[0]?.timezone ?? 'UTC',
    cost_today: data.costToday,
    brain: brain ? { name: brain.name, state: brain.state, offline_ready: true } : null,
    nodes: data.nodes.map((node) => ({ id: node.id, state: node.state, degraded: node.degraded })),
    plan: data.org.plan,
    limits: data.limits,
    usage: {
      nodes: data.nodes.length,
      active_lanes: data.lanes.filter((lane) => lane.state !== 'cold').length,
      workspaces: data.workspaces.length,
      members: data.members.length,
    },
  };
}

function freezeStatus(data: OrgData): FreezeStatus {
  const lanes = data.lanes.flatMap((lane) => {
    const covering = coveringFreeze({ available: true, frozen: data.freeze, lanes: [], node_frozen: false }, lane.id);
    return covering ? [{ lane: lane.id, state: lane.state, by: `${covering.scope} ${covering.target}` }] : [];
  });
  return {
    available: true,
    node: data.nodes[0]?.id ?? '',
    frozen: data.freeze,
    lanes,
    node_frozen: data.freeze.some((record) => record.scope === 'node'),
    problem: null,
    journal: [],
  };
}

/** The lanes a freeze of this scope reaches, as the node works it out. */
function reachedLanes(data: OrgData, scope: FreezeScope, target: string): string[] {
  if (scope === 'node' || scope === 'mesh') return data.lanes.map((lane) => lane.id);
  if (scope === 'workspace') return data.lanes.filter((lane) => lane.workspace === target).map((lane) => lane.id);
  return [target];
}

/** Stops every agent in the scope, revokes the grants that reach nothing outside it (every grant for a
 * computer), and pauses the schedules that run there. A wider grant is kept for the rest. */
function freezeNow(data: OrgData, body: { scope: 'lane' | 'workspace' | 'node'; target?: string; reason: string }) {
  const node = data.nodes[0]?.id ?? '';
  let target = body.target ?? '';
  if (body.scope === 'node') {
    if (body.target && body.target !== node) return failure(400, 'invalid', `This computer is ${node}. Freeze another computer on that computer or through the controller.`);
    target = node;
  } else if (body.scope === 'workspace') {
    if (!data.workspaces.some((workspace) => workspace.id === target)) return notFound();
  } else if (!data.lanes.some((lane) => lane.id === target)) {
    return notFound();
  }
  const existing = data.freeze.find((record) => record.scope === body.scope && record.target === target);
  const record: FreezeRecord = existing ?? {
    id: newId('fz'),
    scope: body.scope,
    target,
    reason: body.reason.trim() || NO_REASON,
    by: 'owner',
    at: iso(),
    via: 'panel',
    durable: true,
    applied_at: iso(),
    revoked: [],
  };
  if (!existing) data.freeze.push(record);
  const lanes = reachedLanes(data, body.scope, target);
  const inside = (grant: OrgData['grants'][number]) => {
    if (body.scope === 'node') return true;
    if (grant.lanes !== null && grant.lanes.length > 0) {
      return grant.lanes.every((lane) => (body.scope === 'workspace' ? lane.split('/')[0] === target : lane === target));
    }
    return body.scope === 'workspace' && grant.workspaces.length > 0 && grant.workspaces.every((ws) => ws === target);
  };
  const grants = data.grants.filter((grant) => !grant.revoked && inside(grant));
  for (const grant of grants) grant.revoked = true;
  record.revoked = [...(record.revoked ?? []), ...grants.map((grant) => grant.id)];
  const schedules = data.schedules.filter((item) => item.enabled !== false && (body.scope === 'node' || lanes.includes(item.lane ?? '')));
  for (const schedule of schedules) {
    schedule.enabled = false;
    schedule.next_fire_at = null;
    schedule.next_run = null;
    emit(data.org.slug, { type: 'schedule', payload: schedule });
  }
  emit(data.org.slug, { type: 'freeze', payload: { state: 'frozen', scope: body.scope, target } });
  const report: FreezeReport = {
    freeze_id: record.id,
    scope: body.scope,
    target,
    reason: record.reason,
    by: 'owner',
    already_frozen: Boolean(existing),
    frozen_at: record.at,
    durable: true,
    details_saved: true,
    lanes,
    lanes_warm: [],
    stopped: { calls: 0, ms: 0, still_running: 0 },
    revoked: {
      grants: grants.map((grant) => ({ id: grant.id, subject: grant.subject, note: 'revoked by the freeze' })),
      cascades_to: [],
      standing: [],
    },
    runs_cancelled: [],
    schedules_paused: schedules.map((item) => item.id),
    errors: [],
    took_ms: 12,
    next: 'Agents are stopped and must be granted again after an unfreeze. A human can still take control in the viewer and look; nothing was closed or deleted.',
  };
  return HttpResponse.json(report);
}

/** Lifts one freeze. Nothing is restored: revoked grants and paused schedules stay as they are. */
function unfreezeNow(data: OrgData, scope: FreezeScope, target: string) {
  const index = data.freeze.findIndex((record) => record.scope === scope && record.target === target);
  const wider = coverOf(data, scope, target);
  if (index < 0) return HttpResponse.json({ scope, target, unfrozen: false, was_frozen: false, still_frozen_by: wider });
  const [record] = data.freeze.splice(index, 1);
  emit(data.org.slug, { type: 'freeze', payload: { state: 'unfrozen', scope, target } });
  return HttpResponse.json({
    scope,
    target,
    unfrozen: true,
    was_frozen: true,
    freeze_id: record?.id ?? null,
    still_frozen_by: coverOf(data, scope, target),
    next: 'Nothing was restored: grants revoked by the freeze stay revoked, and paused schedules, triggers and cancelled runs stay that way. Grant agents again and resume what you want back.',
  });
}

/** The wider freeze that still covers a lane or a workspace, once this one is lifted. */
function coverOf(data: OrgData, scope: FreezeScope, target: string) {
  if (scope === 'node' || scope === 'mesh') return null;
  const probe = scope === 'lane' ? target : `${target}/x`;
  const record = coveringFreeze({ available: true, frozen: data.freeze.filter((item) => item.scope === 'node' || item.scope === 'mesh'), lanes: [], node_frozen: false }, probe);
  return record ? { scope: record.scope, target: record.target, reason: record.reason } : null;
}

/** An open repository card: a question with `repo_connect`. */
function repoCard(data: OrgData, id: string) {
  const card = data.questions.find((item) => item.id === id);
  return card?.repo_connect ? { ...card, repo_connect: card.repo_connect } : null;
}

const repoChecks = new Map<string, number>();

/** A code card answered or declined: it leaves the list, and the live stream says so. */
function closeCode(data: OrgData, card: CodeRequest, state: 'used' | 'declined') {
  data.codes = data.codes.filter((item) => item.id !== card.id);
  const closed: CodeRequest = { ...card, state };
  emit(data.org.slug, { type: 'code', payload: closed });
  return HttpResponse.json(closed);
}

function isAuthed() {
  return mockAuth.signedIn;
}

type Params = Record<string, string | readonly string[] | undefined>;

interface Info {
  request: Request;
  params: Params;
}

/** Wraps an org route: auth, tenancy, scenarios and a little realistic latency. */
function org(resolver: (data: OrgData, info: Info) => Response | Promise<Response>) {
  return async (info: Info) => {
    if (!isAuthed()) return failure(401, 'unauthenticated', 'Sign in to continue.');
    if (scenario.has('loading')) await delay('infinite');
    if (scenario.has('error')) return failure(500, 'unavailable', 'The service is unavailable.');
    await delay(scenario.has('still') ? 0 : 80 + Math.random() * 160);
    const data = db.orgs.get(String(info.params.org));
    if (!data) return notFound();
    return resolver(data, info);
  };
}

async function jsonBody<T>(request: Request): Promise<T> {
  return (await request.clone().json()) as T;
}

const changed = () => failure(409, 'conflict', 'This changed since you read it.');

/**
 * The cloud holds a decision `undoMs` before it goes to the node, so a slip can be undone, and
 * the node takes `runMs` to say how it went. Tests shorten both.
 */
export const mockTiming = { undoMs: 6_000, runMs: 1_500 };
const releases = new Map<string, ReturnType<typeof setTimeout>>();

/** Writes a new version of a decided approval and tells the panel. */
function rewrite(data: OrgData, id: string, patch: (item: Approval) => Approval) {
  const index = data.history.findIndex((item) => item.id === id);
  const item = data.history[index];
  if (!item) return;
  const next = withPlace(data, patch(item));
  data.history[index] = next;
  emit(data.org.slug, { type: 'approval', payload: next });
}

/** The undo window is over: the decision goes to the node, and a yes comes back with how it went. */
function release(data: OrgData, id: string) {
  releases.delete(id);
  const item = data.history.find((entry) => entry.id === id);
  if (!item) return;
  if (item.state !== 'approved') return rewrite(data, id, (entry) => ({ ...entry, undo_until: null }));
  const outcome = outcomeOf(withPlace(data, item), 'approve', db.user.name);
  rewrite(data, id, (entry) => ({ ...entry, undo_until: null, outcome }));
  if (outcome.state !== 'pending') return;
  setTimeout(() => rewrite(data, id, (entry) => (entry.outcome?.state === 'pending' ? { ...entry, outcome: doneOutcome(entry) } : entry)), mockTiming.runMs);
}

function decide(data: OrgData, id: string, decision: 'approve' | 'deny', proved = false): Approval | null {
  const found = data.approvals.find((item) => item.id === id);
  if (!found) return null;
  const approval = withPlace(data, found);
  data.approvals = data.approvals.filter((item) => item.id !== id);
  // A yes is "under way" until it is sent; a no has nothing to wait for.
  const decided: Approval = withPlace(data, {
    ...approval,
    state: decision === 'approve' ? 'approved' : 'denied',
    decided_by: db.user.name,
    decided_at: iso(),
    decided_method: proved ? 'passkey' : 'panel',
    decided_level: proved ? 'present' : 'signed_in',
    decided_where: proved ? "Demo's MacBook Pro" : null,
    delivery: 'delivered',
    delivery_error: null,
    undo_until: iso(mockTiming.undoMs),
    outcome: decision === 'deny' ? outcomeOf(approval, 'deny', db.user.name) : { ...outcomeOf({ ...approval, lane_state: null }, 'approve', db.user.name) },
  });
  data.history.unshift(decided);
  emit(data.org.slug, { type: 'approval', payload: decided });
  releases.set(id, setTimeout(() => release(data, id), mockTiming.undoMs));
  return decided;
}

/** Inside the window, a decision is taken back: pending again, as if nobody had answered. */
function undo(data: OrgData, id: string): Approval | Response {
  const item = data.history.find((entry) => entry.id === id);
  if (!item) return notFound();
  const timer = releases.get(id);
  if (!timer || !item.undo_until || new Date(item.undo_until).getTime() <= Date.now()) {
    return failure(409, 'undo_closed', 'It is too late to undo this: it has already been sent.');
  }
  clearTimeout(timer);
  releases.delete(id);
  data.history = data.history.filter((entry) => entry.id !== id);
  const back: Approval = { ...item, state: 'pending', decided_by: null, decided_at: null, delivery: null, undo_until: null, outcome: null };
  data.approvals.unshift(back);
  const shown = withPlace(data, back);
  emit(data.org.slug, { type: 'approval', payload: shown });
  return shown;
}

function setLane(data: OrgData, id: string, patch: Partial<Lane>): Lane | null {
  const index = data.lanes.findIndex((lane) => lane.id === id);
  const current = data.lanes[index];
  if (!current) return null;
  const next = { ...current, ...patch };
  data.lanes[index] = next;
  emit(data.org.slug, { type: 'lane', payload: withHealth(next) });
  return withHealth(next);
}

const enrollNames = ['office-mini', 'hetzner-2', 'studio-mac', 'edge-3'];

/** Plays the node side of enrollment: the install runs, then the node appears. */
function enrollAfterDelay(data: OrgData, tokenId: string, name: string | null | undefined) {
  setTimeout(() => {
    const token = data.joinTokens.find((item) => item.id === tokenId);
    if (!token) return;
    data.joinTokens = data.joinTokens.filter((item) => item.id !== tokenId);
    const id = name || enrollNames[data.nodes.length % enrollNames.length] || 'new-node';
    const node: NodeRecord = {
      id,
      state: 'up',
      capabilities: ['browser', 'terminal', 'service'],
      cpu: 4,
      mem: { used_mb: 1_400, total_mb: 8_192 },
      warm_lanes: 0,
      max_warm_lanes: 4,
      last_seen: iso(),
      version: '0.1.0',
      degraded: false,
      os: 'Debian 12',
      arch: 'x86_64',
      load: { cpu_pct: 4, mem_used_mb: 1_400, mem_total_mb: 8_192, disk_free_mb: 60_000 },
    };
    data.nodes.push(node);
    emit(data.org.slug, { type: 'node', payload: nodeResource(node) });
  }, 6_000);
}

const paged = <T extends { at: string }>(items: T[], before: string | null, limit: number) => {
  const start = before ? items.findIndex((item) => item.at < before) : 0;
  const slice = start < 0 ? [] : items.slice(start, start + limit);
  const last = slice[slice.length - 1];
  const more = start >= 0 && start + limit < items.length;
  return { items: slice, next_before: more && last ? last.at : null };
};

const svg = (body: string) => new HttpResponse(body, { headers: { 'Content-Type': 'image/svg+xml' } });

export const handlers = [
  // First, so its watchers see a request before the handler that answers it.
  ...setupHandlers({ org, find: (slug) => db.orgs.get(slug) }, (token) => {
    if (token === 'expired') return failure(410, 'expired', 'This invite has expired.');
    if (token === 'used') return failure(410, 'already_used', 'This invite was already used.');
    // Joined as the approver the invite was for: Northwind is now theirs to approve in.
    mockAuth.signedIn = true;
    const data = db.orgs.get('northwind-group');
    if (data) data.role = 'approver';
    return HttpResponse.json({ ...session(), org: 'northwind-group' });
  }),
  // Auth
  http.get('/api/auth/providers', () =>
    HttpResponse.json(scenario.has('computer') ? { magic_link: false, google: false, passkey: true, code: true } : { magic_link: true, google: true, passkey: true }),
  ),
  http.post('/api/auth/magic-link', async ({ request }) => {
    const body = await jsonBody<{ email: string }>(request);
    await delay(240);
    // The mock's "inbox". A real email goes through Mailpit.
    console.info(`[mock mail] Sign-in link for ${body.email}: ${location.origin}/auth/verify?token=mock-valid`);
    return new HttpResponse(null, { status: 202 });
  }),
  http.post('/api/auth/magic-link/verify', async ({ request }) => {
    const { token } = await jsonBody<{ token: string }>(request);
    await delay(320);
    // Unknown, expired and already used all answer the same way, as the API does.
    if (token === 'expired') return failure(410, 'expired', 'This link is no longer valid. Request a new one.');
    mockAuth.signedIn = true;
    return HttpResponse.json({ ...session(), next: null });
  }),
  http.post('/api/auth/passkey/login/options', () => HttpResponse.json(passkeyOptions())),
  http.post('/api/auth/passkey/login/verify', async () => {
    await delay(200);
    mockAuth.signedIn = true;
    return HttpResponse.json(session());
  }),
  http.post('/api/auth/passkey/register/options', () => HttpResponse.json({ ...passkeyOptions(), rp: { name: 'openajar' } })),
  http.post('/api/auth/passkey/register/verify', async () => {
    await delay(200);
    const passkey = { id: newId('pk'), name: 'New passkey', created_at: iso(), last_used_at: null };
    db.passkeys.push(passkey);
    db.user.has_passkey = true;
    return HttpResponse.json(passkey);
  }),
  http.get('/api/auth/passkeys', () => HttpResponse.json(db.passkeys)),
  http.delete('/api/auth/passkeys/:id', ({ params }) => {
    db.passkeys = db.passkeys.filter((item) => item.id !== params.id);
    db.user.has_passkey = db.passkeys.length > 0;
    return new HttpResponse(null, { status: 204 });
  }),
  http.post('/api/auth/step-up/passkey/options', () => HttpResponse.json(passkeyOptions())),
  http.post('/api/auth/step-up/passkey/verify', async () => {
    await delay(240);
    grantStepUp();
    return HttpResponse.json(session());
  }),
  http.post('/api/auth/step-up/magic-link', () => {
    // Stands in for the person opening the email on another tab.
    setTimeout(grantStepUp, 3_000);
    return HttpResponse.json({ message: 'Check your email for a link to continue.' }, { status: 202 });
  }),
  http.post('/api/auth/step-up/verify', async ({ request }) => {
    if (!isAuthed()) return failure(401, 'unauthenticated', 'Sign in to continue.');
    const { token } = await jsonBody<{ token: string }>(request);
    if (token === 'expired') return failure(410, 'expired', 'This link is no longer valid. Request a new one.');
    grantStepUp();
    return HttpResponse.json(session());
  }),
  http.get('/api/session', async () => {
    await delay(scenario.has('still') ? 0 : 60);
    if (!isAuthed()) return failure(401, 'unauthenticated', 'Sign in to continue.');
    return HttpResponse.json(session());
  }),
  http.delete('/api/session', () => {
    mockAuth.signedIn = false;
    return new HttpResponse(null, { status: 204 });
  }),
  http.get('/api/sessions', () => HttpResponse.json(db.devices)),
  http.delete('/api/sessions/:id', ({ params }) => {
    db.devices = db.devices.filter((item) => item.id !== params.id);
    return new HttpResponse(null, { status: 204 });
  }),
  http.patch('/api/me', async ({ request }) => {
    const body = await jsonBody<{ name?: string }>(request);
    db.user = { ...db.user, ...body };
    return HttpResponse.json(db.user);
  }),

  // Invites
  http.get('/api/invites/:token', async ({ params }) => {
    await delay(160);
    if (params.token === 'expired') return failure(410, 'expired', 'This invite has expired.');
    if (params.token === 'used') return failure(410, 'already_used', 'This invite was already used.');
    return HttpResponse.json({
      org_name: 'Northwind Group',
      role: 'approver',
      inviter: 'Mara Lindqvist',
      email: db.user.email,
      inviter_first_name: 'Mara',
      business: 'Aster Coffee',
      workspaces: ['Aster Coffee'],
      email_hint: 'de***@openajar.local',
      // The link signs a new person in; someone already signed in accepts as before.
      one_click: !isAuthed(),
      expires_at: iso(6 * 86_400_000),
    });
  }),
  http.post('/api/invites/:token/accept', async () => {
    await delay(240);
    return HttpResponse.json({ org: 'northwind-group' });
  }),

  // Orgs
  http.post('/api/orgs', async ({ request }) => {
    const body = await jsonBody<{ name: string; slug?: string; workspace?: string; goal?: Org['goal']; goal_note?: string | null }>(request);
    const base = body.slug || body.name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
    // As the cloud does: without a slug, the next free address after the name.
    let slug = base;
    for (let n = 2; !body.slug && db.orgs.has(slug); n += 1) slug = `${base}-${n}`;
    if (db.orgs.has(slug)) return failure(409, 'slug_taken', 'That URL is taken. Try another.');
    const data = emptyOrg(Date.now(), slug, body.name, 'owner', 'Free');
    Object.assign(data.org, { goal: body.goal ?? null, goal_note: body.goal_note ?? null });
    // First run names the first workspace, made with the org.
    const workspace = body.workspace ? body.workspace.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'personal' : null;
    if (workspace && body.workspace) data.workspaces.push({ id: workspace, name: body.workspace, color: '#a0763f' });
    data.members = [
      { id: newId('mem'), user: { id: db.user.id, email: db.user.email, name: db.user.name }, role: 'owner', workspaces: null, joined_at: iso(), last_active_at: iso() },
    ];
    data.limits = { nodes: 2, active_lanes: 40, workspaces: 10, members: 5, retention_days: 7 };
    db.orgs.set(slug, data);
    await delay(320);
    return HttpResponse.json({ ...data.org, role: 'owner', workspace }, { status: 201 });
  }),
  http.get('/api/orgs/:org', org((data) => HttpResponse.json(data.org))),
  http.patch(
    '/api/orgs/:org',
    org(async (data, { request }) => {
      Object.assign(data.org, await jsonBody<{ name?: string }>(request));
      return HttpResponse.json(data.org);
    }),
  ),
  http.delete(
    '/api/orgs/:org',
    org((data) => {
      if (!stepUpValid()) return stepUpNeeded();
      db.orgs.delete(data.org.slug);
      return new HttpResponse(null, { status: 204 });
    }),
  ),
  http.get('/api/orgs/:org/status', org((data) => HttpResponse.json(status(data)))),
  http.get('/api/orgs/:org/members', org((data) => HttpResponse.json(data.members))),
  http.patch(
    '/api/orgs/:org/members/:id',
    org(async (data, { request, params }) => {
      const body = await jsonBody<Partial<Member> & { workspace?: string }>(request);
      const member = data.members.find((item) => item.id === params.id);
      if (!member) return notFound();
      const owners = data.members.filter((item) => item.role === 'owner');
      if (member.role === 'owner' && body.role && body.role !== 'owner' && owners.length === 1) {
        return failure(409, 'last_owner', 'An org needs at least one owner.');
      }
      // Like the node: a role with a business changes that business only, and the others keep theirs.
      if (body.workspace !== undefined) {
        if (!body.role) return failure(400, 'invalid', 'Say which role the person has, and in which business.');
        const rest = (member.roles_by_workspace ?? []).filter((row) => row.workspace !== body.workspace);
        member.roles_by_workspace = [...rest, { workspace: body.workspace, role: body.role }];
        return HttpResponse.json(member);
      }
      Object.assign(member, body);
      return HttpResponse.json(member);
    }),
  ),
  http.delete(
    '/api/orgs/:org/members/:id',
    org((data, { params }) => {
      if (!stepUpValid()) return stepUpNeeded();
      data.members = data.members.filter((item) => item.id !== params.id);
      return new HttpResponse(null, { status: 204 });
    }),
  ),
  http.get('/api/orgs/:org/invites', org((data) => HttpResponse.json(data.invites))),
  http.post(
    '/api/orgs/:org/invites',
    org(async (data, { request }) => {
      const body = await jsonBody<{ email: string; role: Role; workspaces?: string[] | null }>(request);
      const invite = {
        id: newId('inv'),
        email: body.email,
        role: body.role,
        workspaces: body.workspaces ?? null,
        invited_by: db.user.name,
        created_at: iso(),
        expires_at: iso(7 * 86_400_000),
        state: 'pending' as const,
      };
      data.invites.unshift(invite);
      return HttpResponse.json(invite, { status: 201 });
    }),
  ),
  http.delete(
    '/api/orgs/:org/invites/:id',
    org((data, { params }) => {
      // As in the API: a revoked invite stays on the list, marked as revoked.
      const invite = data.invites.find((item) => item.id === params.id);
      if (!invite) return notFound();
      if (invite.state === 'accepted') return failure(409, 'conflict', 'That invite was already accepted. Remove the member instead.');
      invite.state = 'revoked';
      return new HttpResponse(null, { status: 204 });
    }),
  ),
  http.get(
    '/api/orgs/:org/audit',
    org((data, { request }) => {
      const url = new URL(request.url);
      return HttpResponse.json(paged(data.audit, url.searchParams.get('before'), Number(url.searchParams.get('limit') ?? 50)));
    }),
  ),

  // Workspaces and lanes
  http.get('/api/orgs/:org/workspaces', org((data) => HttpResponse.json(workspaces(data)))),
  http.get(
    '/api/orgs/:org/workspaces/:id',
    org((data, { params }) => {
      const business = workspaces(data).find((item) => item.id === params.id);
      return business ? HttpResponse.json(business) : notFound();
    }),
  ),
  http.get(
    '/api/orgs/:org/workspaces/:id/queue',
    org((data, { params }) => {
      if (data.role !== 'owner') return denied();
      const business = workspaces(data).find((item) => item.id === params.id);
      if (!business) return notFound();
      const lanes = business.lanes.filter((lane) => lane.readiness && lane.readiness !== 'ready');
      const ready = business.lanes.filter((lane) => lane.readiness === 'ready').length;
      const needsLogin = lanes.filter((lane) => lane.readiness === 'needs_login').length;
      const needsAttention = lanes.filter((lane) => lane.readiness === 'needs_attention').length;
      return HttpResponse.json({
        workspace: business.id,
        summary: `${ready} of ${business.lanes.length} ready`,
        ready,
        total: business.lanes.length,
        needs_login: needsLogin,
        needs_attention: needsAttention,
        workspaces: [{ id: business.id, name: business.name, summary: `${ready} of ${business.lanes.length} ready`, ready, total: business.lanes.length }],
        lanes: lanes.map((lane) => ({
          id: lane.id,
          workspace: lane.workspace,
          name: lane.name,
          platform: lane.site?.key ?? lane.kind,
          readiness: lane.readiness,
          note: lane.readiness_note ?? null,
          login_url: lane.login_url ?? null,
        })),
      });
    }),
  ),
  http.get('/api/orgs/:org/home', org((data) => HttpResponse.json(homeSummary(data, needsYou(data), data.approvals.length + data.questions.length)))),
  http.post(
    '/api/orgs/:org/workspaces',
    org(async (data, { request }) => {
      const body = await jsonBody<{ id?: string; name: string; color?: string }>(request);
      const id = body.id || body.name.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');
      if (data.workspaces.some((item) => item.id === id)) return failure(409, 'conflict', 'A workspace with that id already exists.');
      if (data.workspaces.length >= data.limits.workspaces) {
        return failure(402, 'plan_limit', `Your plan includes ${data.limits.workspaces} workspaces.`, {
          limit: data.limits.workspaces,
          current: data.workspaces.length,
        });
      }
      data.workspaces.push({ id, name: body.name, color: body.color ?? '#6b7280' });
      return HttpResponse.json(workspaces(data).find((item) => item.id === id), { status: 201 });
    }),
  ),
  http.patch(
    '/api/orgs/:org/workspaces/:id',
    org(async (data, { params, request }) => {
      const workspace = data.workspaces.find((item) => item.id === params.id);
      if (!workspace) return notFound();
      Object.assign(workspace, await jsonBody<{ name?: string }>(request));
      return HttpResponse.json(workspaces(data).find((item) => item.id === workspace.id));
    }),
  ),
  http.delete(
    '/api/orgs/:org/workspaces/:id',
    org((data, { params }) => {
      if (!stepUpValid()) return stepUpNeeded();
      data.workspaces = data.workspaces.filter((item) => item.id !== params.id);
      data.lanes = data.lanes.filter((lane) => lane.workspace !== params.id);
      return new HttpResponse(null, { status: 204 });
    }),
  ),
  http.post(
    '/api/orgs/:org/lanes',
    org(async (data, { request }) => {
      const body = await jsonBody<{ id: string; kind: Lane['kind']; preset?: string }>(request);
      if (data.lanes.some((lane) => lane.id === body.id)) return failure(409, 'lane_exists', 'A lane with that name already exists.');
      const [workspace = '', name = ''] = body.id.split('/');
      const lane: Lane = {
        id: body.id,
        workspace,
        name,
        kind: body.kind,
        engine: body.kind === 'browser' ? 'camoufox' : null,
        preset: body.preset ?? 'account',
        state: 'cold',
        signal: 'unknown',
        proxy: null,
        exit_ip: null,
        last_used: null,
        node: data.nodes[0]?.id ?? 'unassigned',
        quarantine_reason: null,
        logged_in_hint: 'unknown',
        control: { ...idleControl },
        hold: null,
        hold_until: null,
        signal_detail: null,
        account: null,
        readiness: 'needs_login',
        readiness_note: null,
        login_url: null,
      };
      data.lanes.push(lane);
      emit(data.org.slug, { type: 'lane', payload: lane });
      return HttpResponse.json(lane, { status: 201 });
    }),
  ),
  http.get(
    '/api/orgs/:org/lanes/:ws/:name',
    org((data, { params }) => {
      const lane = data.lanes.find((item) => item.id === `${params.ws}/${params.name}`);
      return lane ? HttpResponse.json(withClaim(data, withHealth(lane))) : failure(404, 'lane_not_found', 'No such lane.');
    }),
  ),
  http.post(
    '/api/orgs/:org/lanes/:ws/:name/open',
    org((data, { params }) => {
      const id = `${params.ws}/${params.name}`;
      const lane = setLane(data, id, { state: 'warming' });
      if (!lane) return failure(404, 'lane_not_found', 'No such lane.');
      setTimeout(() => setLane(data, id, { state: 'warm', last_used: iso(), viewer: { url: `/vnc/${id}` } }), 2_400);
      return HttpResponse.json(lane);
    }),
  ),
  http.post(
    '/api/orgs/:org/lanes/:ws/:name/close',
    org((data, { params }) => {
      const lane = setLane(data, `${params.ws}/${params.name}`, { state: 'cold', viewer: null });
      return lane ? HttpResponse.json(lane) : failure(404, 'lane_not_found', 'No such lane.');
    }),
  ),
  http.get('/api/orgs/:org/lanes/:ws/:name/screenshot', ({ params }) => {
    const id = `${params.ws}/${params.name}`;
    const signal = db.orgs.get(String(params.org))?.lanes.find((lane) => lane.id === id)?.signal;
    return svg(laneScreenshot(id, signal === 'captcha' ? 'captcha' : signal === 'logged_out' ? 'logged_out' : 'none'));
  }),

  // An account's page (docs/API.md). Anyone who can see an account reads it. Every change is the owner's,
  // and the node refuses anyone else the same way, so the mock does too.
  http.get(
    '/api/orgs/:org/lanes/:ws/:name/pace',
    org((data, { params }) => onLane(data, params, (lane) => HttpResponse.json(lanePace(data, lane)))),
  ),
  http.put(
    '/api/orgs/:org/lanes/:ws/:name/pace',
    org(async (data, { params, request }) => {
      if (data.role !== 'owner') return denied();
      const body = await jsonBody<{ pace?: Pace | null; preset?: boolean }>(request);
      return onLane(data, params, (lane) => {
        emit(data.org.slug, { type: 'meter', payload: { scope: 'lane', id: lane.id, kind: 'pace' } });
        return HttpResponse.json(setLanePace(data, lane, body));
      });
    }),
  ),
  http.get(
    '/api/orgs/:org/pace/workspaces/:ws',
    org((data, { params }) => (data.role === 'owner' ? HttpResponse.json(workspaceZone(data, String(params.ws))) : denied())),
  ),
  http.put(
    '/api/orgs/:org/pace/workspaces/:ws',
    org(async (data, { params, request }) => {
      if (data.role !== 'owner') return denied();
      const { timezone } = await jsonBody<{ timezone: string | null }>(request);
      data.account.clocks[String(params.ws)] = timezone;
      emit(data.org.slug, { type: 'meter', payload: { scope: 'workspace', id: String(params.ws), kind: 'pace' } });
      return HttpResponse.json(workspaceZone(data, String(params.ws)));
    }),
  ),
  http.get(
    '/api/orgs/:org/lanes/:ws/:name/meter',
    org((data, { params }) => onLane(data, params, (lane) => HttpResponse.json(laneMeter(data, lane)))),
  ),
  http.put(
    '/api/orgs/:org/lanes/:ws/:name/meter',
    org(async (data, { params, request }) => {
      if (data.role !== 'owner') return denied();
      const { limits } = await jsonBody<{ limits: Limits | null }>(request);
      return onLane(data, params, (lane) => {
        emit(data.org.slug, { type: 'meter', payload: { scope: 'lane', id: lane.id, kind: 'limits' } });
        return HttpResponse.json(setLaneLimits(data, lane, limits));
      });
    }),
  ),
  http.get(
    '/api/orgs/:org/lanes/:ws/:name/queue',
    org((data, { params }) => onLane(data, params, (lane) => HttpResponse.json(laneQueue(data, lane))))),
  http.get(
    '/api/orgs/:org/lanes/:ws/:name/taint',
    org((data, { params }) => onLane(data, params, (lane) => HttpResponse.json(laneTaint(data, lane, data.role === 'owner'))))),
  http.post(
    '/api/orgs/:org/grants/:id/taint/clear',
    org(async (data, { params, request }) => {
      if (data.role !== 'owner') return denied();
      if (!stepUpValid()) return stepUpNeeded();
      const { confirm } = await jsonBody<{ confirm?: string }>(request);
      if ((confirm ?? '').trim().toLowerCase() !== 'clear') return failure(400, 'invalid', 'Type "clear" to confirm.');
      const cleared = clearTaint(data, String(params.id));
      return cleared ? HttpResponse.json(cleared) : notFound();
    }),
  ),
  http.get(
    '/api/orgs/:org/lanes/:ws/:name/check',
    org((data, { params }) => onLane(data, params, (lane) => HttpResponse.json(laneCheck(lane)))),
  ),
  http.get('/api/orgs/:org/lanes/checks', org((data) => HttpResponse.json({ lanes: checkBriefs(data) }))),
  http.get(
    '/api/orgs/:org/lanes/:ws/:name/keepalive',
    org((data, { params }) => onLane(data, params, (lane) => HttpResponse.json(laneKeepalive(lane)))),
  ),
  http.get(
    '/api/orgs/:org/lanes/:ws/:name/sites',
    org((data, { params }) => onLane(data, params, (lane) => HttpResponse.json(laneSites(data, lane)))),
  ),
  http.post(
    '/api/orgs/:org/lanes/:ws/:name/sites',
    org(async (data, { params, request }) => {
      if (data.role !== 'owner') return denied();
      const body = await jsonBody<{ op: 'add' | 'remove' | 'set'; sites: string[]; strict?: boolean }>(request);
      return onLane(data, params, (lane) => {
        if (body.op === 'remove') {
          const current = laneSites(data, lane).sites;
          const missing = body.sites.filter((site) => !current.includes(site));
          if (missing.length) return failure(400, 'invalid', `not on the list: ${missing.join(', ')}`);
        }
        return HttpResponse.json(setLaneSites(data, lane, body));
      });
    }),
  ),
  http.post(
    '/api/orgs/:org/lanes/:ws/:name/view-link',
    org((data, { params }) => onLane(data, params, (lane) => HttpResponse.json(viewLink(lane)))),
  ),

  // Approvals
  http.get(
    '/api/orgs/:org/approvals',
    org((data, { request }) => {
      const search = new URL(request.url).searchParams;
      const outcome = search.get('outcome');
      if (outcome) {
        // Failed or partly done after a yes, and nobody has said "Got it" yet.
        const open = (item: Approval) => (item.outcome?.state === 'failed' || item.outcome?.state === 'partial') && !item.outcome.cleared_at;
        const items = data.history.filter((item) => (outcome === 'needs_you' ? open(item) : item.outcome?.state === outcome));
        return HttpResponse.json(items.map((item) => withPlace(data, item)));
      }
      const state = search.get('state') ?? 'pending';
      const items = state === 'pending' ? data.approvals : data.history.filter((item) => state === 'all' || item.state === state);
      return HttpResponse.json(items.map((item) => withPlace(data, item)));
    }),
  ),
  http.post(
    '/api/orgs/:org/approvals/:id/undo',
    org((data, { params }) => {
      const result = undo(data, String(params.id));
      return result instanceof Response ? result : HttpResponse.json(result);
    }),
  ),
  http.post(
    '/api/orgs/:org/approvals/:id/clear',
    org((data, { params }) => {
      const index = data.history.findIndex((item) => item.id === params.id);
      const item = data.history[index];
      if (!item) return notFound();
      if (item.outcome?.state !== 'failed' && item.outcome?.state !== 'partial') return failure(400, 'invalid', 'Only a failed outcome can be cleared.');
      const cleared = withPlace(data, { ...item, outcome: { ...item.outcome, cleared_at: iso(), cleared_by: db.user.name } });
      data.history[index] = cleared;
      emit(data.org.slug, { type: 'approval', payload: cleared });
      return HttpResponse.json(cleared);
    }),
  ),
  http.post(
    '/api/orgs/:org/approvals/batch',
    org(async (data, { request }) => {
      const { ids, decision, content_hashes } = await jsonBody<{
        ids: string[];
        decision: 'approve' | 'deny';
        content_hashes?: Record<string, string>;
      }>(request);
      // Like the API: one result per id, in order. A stale or decided item fails on its own.
      const results = ids.map((id) => {
        const approval = data.approvals.find((item) => item.id === id);
        const hash = content_hashes?.[id];
        if (approval && hash !== undefined && hash !== approval.content_hash) {
          return { id, ok: false, error: { code: 'content_changed', message: 'This changed since you read it.' } };
        }
        const decided = decide(data, id, decision);
        return decided
          ? { ...decided, ok: true }
          : { id, ok: false, error: { code: 'already_decided', message: 'Someone already decided this.' } };
      });
      return HttpResponse.json(results);
    }),
  ),
  http.post(
    '/api/orgs/:org/approvals/:id/approve',
    org(async (data, { params, request }) => {
      const { content_hash } = await jsonBody<{ content_hash: string }>(request);
      const current = data.approvals.find((item) => item.id === params.id);
      if (current && current.content_hash !== content_hash) return changed();
      const heavy = current ? needsProof(withPlace(data, current)) : false;
      const short = current ? spendProof(data, current) : null;
      if (short) return failure(403, 'proof_too_weak', 'This needs you to prove it is you first.', { requires: short });
      const decided = decide(data, String(params.id), 'approve', heavy);
      return decided ? HttpResponse.json(decided) : failure(409, 'already_decided', 'Someone already decided this.');
    }),
  ),
  http.post(
    '/api/orgs/:org/approvals/:id/deny',
    org(async (data, { params, request }) => {
      const { content_hash } = await jsonBody<{ content_hash: string }>(request);
      const current = data.approvals.find((item) => item.id === params.id);
      if (current && current.content_hash !== content_hash) return changed();
      const decided = decide(data, String(params.id), 'deny');
      return decided ? HttpResponse.json(decided) : failure(409, 'already_decided', 'Someone already decided this.');
    }),
  ),
  http.get('/api/orgs/:org/approvals/:id/preview.png', ({ params }) => svg(postPreview(String(params.id)))),
  http.get(
    '/api/orgs/:org/approvals/:id',
    org((data, { params }) => {
      const item = data.approvals.find((entry) => entry.id === params.id) ?? data.history.find((entry) => entry.id === params.id);
      return item ? HttpResponse.json(withPlace(data, item)) : notFound();
    }),
  ),

  // Questions
  http.get('/api/orgs/:org/questions', org((data) => HttpResponse.json(data.questions))),
  http.post(
    '/api/orgs/:org/questions/:id/answer',
    org(async (data, { params, request }) => {
      const { answer } = await jsonBody<{ answer: string }>(request);
      const question = data.questions.find((item) => item.id === params.id);
      if (!question) return failure(409, 'conflict', 'That question was already answered.');
      if (question.options && !question.options.includes(answer)) return failure(400, 'invalid', 'Choose one of the options.');
      data.questions = data.questions.filter((item) => item.id !== params.id);
      const answered = { ...question, state: 'answered' as const, answer, answered_at: iso(), answered_by: db.user.name };
      emit(data.org.slug, { type: 'question', payload: answered });
      return HttpResponse.json(answered);
    }),
  ),

  // Freeze (break glass). The same rules as the node: an admin freezes an account or a workspace, the
  // owner freezes the computer, and only the owner lifts a freeze, in person, after a passkey check.
  http.get('/api/orgs/:org/freeze', org((data) => HttpResponse.json(freezeStatus(data)))),
  http.post(
    '/api/orgs/:org/freeze',
    org(async (data, { request }) => {
      const body = await jsonBody<{ scope: 'lane' | 'workspace' | 'node'; target?: string; reason: string }>(request);
      const allowed = body.scope === 'node' ? data.role === 'owner' : data.role === 'owner' || data.role === 'admin';
      if (!allowed) return failure(403, 'denied', 'Freezing needs the owner, or an admin of that workspace (lanes and the workspace itself).');
      if (!body.reason?.trim()) return failure(400, 'invalid', 'Say why, in a few words.');
      return freezeNow(data, body);
    }),
  ),
  http.post(
    '/api/orgs/:org/unfreeze',
    org(async (data, { request }) => {
      if (data.role !== 'owner') return failure(403, 'denied', 'Only the owner of this computer can unfreeze.');
      const body = await jsonBody<{ scope: FreezeScope; target?: string }>(request);
      if (!stepUpValid()) return stepUpNeeded();
      return unfreezeNow(data, body.scope, body.scope === 'node' ? (data.nodes[0]?.id ?? '') : (body.target ?? ''));
    }),
  ),

  // Code cards: a one-time code a site asked for. The owner types it, once; the node types it into the site.
  // The code is never kept, logged or sent back, so a card's answer is only its new state.
  http.get(
    '/api/orgs/:org/codes',
    org((data) => (data.role === 'owner' ? HttpResponse.json(openCodes(data)) : failure(403, 'denied', 'Only the owner answers a login code.'))),
  ),
  http.post(
    '/api/orgs/:org/codes/:id',
    org(async (data, { params, request }) => {
      if (data.role !== 'owner') return failure(403, 'denied', 'Only the owner answers a login code.');
      const { code } = await jsonBody<{ code: string }>(request);
      if (!/^[A-Za-z0-9]{4,12}$/.test(String(code ?? '').replace(/[\s-]/g, ''))) return failure(400, 'invalid', 'A code is 4 to 12 letters or digits.');
      const card = openCodes(data).find((item) => item.id === params.id);
      if (!card) return failure(404, 'vault_code_request_closed', 'This code request is closed. The login stopped waiting, or the code was already entered.');
      return closeCode(data, card, 'used');
    }),
  ),
  http.post(
    '/api/orgs/:org/codes/:id/decline',
    org((data, { params }) => {
      if (data.role !== 'owner') return failure(403, 'denied', 'Only the owner answers a login code.');
      const card = openCodes(data).find((item) => item.id === params.id);
      if (!card) return failure(404, 'vault_code_request_closed', 'This code request is closed. The login stopped waiting, or the code was already entered.');
      return closeCode(data, card, 'declined');
    }),
  ),

  // Repository cards: an agent asks the owner to connect a private repository (openajar.environments.connect). The
  // token is typed on the computer's own page, so the mock only opens a trip for it: the trip settles as the node's
  // would (mocks/access.ts), closing the card. No token is ever sent here.
  http.post(
    '/api/orgs/:org/repo-connect/:id/form',
    org((data, { params }) => {
      if (data.role !== 'owner') return failure(403, 'denied', 'Only the owner of this computer can do this.');
      if (!stepUpValid()) return stepUpNeeded();
      const card = repoCard(data, String(params.id));
      if (!card) return failure(404, 'not_found', 'That card is closed, or its repository is connected.');
      const { repo } = card.repo_connect;
      const id = `ft_${Math.random().toString(36).slice(2, 10)}`;
      const token = Math.random().toString(36).slice(2, 12) + Math.random().toString(36).slice(2, 12);
      const shown: FormTrip = { id, kind: 'repo_token', stage: 'waiting', subject: repo, form_url: `/api/forms/${token}`, expires_at: iso(15 * 60_000) };
      data.trips[id] = { ...shown, fields: { card: card.id, repo }, opened: Date.now() };
      return HttpResponse.json({ ...shown, repo });
    }),
  ),
  http.post(
    '/api/orgs/:org/repo-connect/:id/deploy-key',
    org((data, { params }) => {
      if (data.role !== 'owner') return failure(403, 'denied', 'Only the owner of this computer can do this.');
      const card = repoCard(data, String(params.id));
      if (!card) return failure(404, 'not_found', 'That card is closed, or its repository is connected.');
      const { repo } = card.repo_connect;
      return HttpResponse.json({
        repo,
        name: 'github-key-ssh',
        public_key: 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockMockMockMockMockMockMockMockMockMockMock openajar-github-key-ssh',
        fingerprint: 'SHA256:mockmockmockmockmockmockmockmockmockmockmo',
        settings_url: `https://github.com/${repo.split('/').slice(1).join('/')}/settings/keys/new`,
      });
    }),
  ),
  http.post(
    '/api/orgs/:org/repo-connect/:id/check',
    org((data, { params }) => {
      if (data.role !== 'owner') return failure(403, 'denied', 'Only the owner of this computer can do this.');
      const card = repoCard(data, String(params.id));
      if (!card) return failure(404, 'not_found', 'That card is closed, or its repository is connected.');
      // The first check comes before the admin added the key; the next one finds it.
      const tries = (repoChecks.get(card.id) ?? 0) + 1;
      repoChecks.set(card.id, tries);
      if (tries === 1) {
        return failure(400, 'repo_connect_failed', `Not yet: ${card.repo_connect.repo}: its deploy key github-key-ssh was refused. Add the deploy key to the repository (with write access to push), then check again.`);
      }
      data.questions = data.questions.filter((item) => item.id !== card.id);
      return HttpResponse.json({ repo: card.repo_connect.repo, connected: true, closed: [card.id] });
    }),
  ),
  http.post(
    '/api/orgs/:org/repo-connect/:id/decline',
    org((data, { params }) => {
      if (data.role !== 'owner') return failure(403, 'denied', 'Only the owner of this computer can do this.');
      const card = repoCard(data, String(params.id));
      if (!card) return failure(404, 'not_found', 'That card is closed, or its repository is connected.');
      data.questions = data.questions.filter((item) => item.id !== card.id);
      return HttpResponse.json({ repo: card.repo_connect.repo, declined: true });
    }),
  ),

  // A lane its node stopped reporting: an admin frees its id. 409 while the node still reports it.
  http.post(
    '/api/orgs/:org/lanes/:ws/:name/release',
    org((data, { params }) => {
      const lane = data.lanes.find((item) => item.id === `${params.ws}/${params.name}`);
      if (!lane) return notFound();
      if (data.role !== 'owner' && data.role !== 'admin') return failure(403, 'forbidden', 'You do not have access to do that.');
      if (!lane.missing_since) return failure(409, 'conflict', 'Its node still reports this lane, so it cannot be released.');
      data.lanes = data.lanes.filter((item) => item.id !== lane.id);
      return new HttpResponse(null, { status: 204 });
    }),
  ),

  // Lane control
  http.post(
    '/api/orgs/:org/lanes/:ws/:name/control',
    org(async (data, { params, request }) => {
      const { action } = await jsonBody<{ action: ControlAction }>(request);
      const id = `${params.ws}/${params.name}`;
      const lane = data.lanes.find((item) => item.id === id);
      if (!lane) return failure(404, 'lane_not_found', 'No such lane.');
      if (data.role === 'viewer' || data.role === 'approver') return failure(403, 'forbidden', 'You do not have access to do that.');
      await delay(scenario.has('still') ? 40 : 260);
      const now = iso();
      const control: LaneControl =
        action === 'take'
          ? { ...idleControl, state: 'human', by: db.user.name, by_id: db.user.id, since: now, last_human_input: now }
          : action === 'stop_agent'
            ? { ...idleControl, state: 'paused', by: db.user.name, by_id: db.user.id, since: now }
            : action === 'deny_request'
              ? { ...lane.control, takeback_requested: false, takeback_reason: null, takeback_by: null }
              : { ...idleControl };
      const hold = control.state === 'paused' ? `Stopped by ${db.user.name}` : null;
      const next = setLane(data, id, { control, hold: control.state === 'agent' ? null : (hold ?? lane.hold) });
      if (next) emit(data.org.slug, { type: 'control', payload: { lane: id, control } });
      return HttpResponse.json(next);
    }),
  ),

  // Logins
  http.get(
    '/api/orgs/:org/queue',
    org((data) => {
      const browser = data.lanes.filter((lane) => lane.kind === 'browser');
      const ready = browser.filter((lane) => lane.readiness === 'ready');
      const open = browser.filter((lane) => lane.readiness !== 'ready');
      const rank = (lane: Lane) => (lane.readiness === 'needs_attention' ? 0 : 1);
      const queue: LoginQueue = {
        summary: `${ready.length} of ${browser.length} ready`,
        ready: ready.length,
        total: browser.length,
        needs_login: open.filter((lane) => lane.readiness === 'needs_login').length,
        needs_attention: open.filter((lane) => lane.readiness === 'needs_attention').length,
        workspaces: data.workspaces.map((workspace) => {
          const own = browser.filter((lane) => lane.workspace === workspace.id);
          const done = own.filter((lane) => lane.readiness === 'ready').length;
          return { id: workspace.id, name: workspace.name, summary: `${done} of ${own.length} ready`, ready: done, total: own.length };
        }),
        lanes: [...open]
          .sort((a, b) => rank(a) - rank(b))
          .map((lane) => ({
            id: lane.id,
            workspace: lane.workspace,
            name: lane.name,
            platform: lane.name,
            readiness: lane.readiness ?? 'needs_login',
            note: lane.readiness_note ?? null,
            login_url: lane.login_url ?? null,
          })),
      };
      return HttpResponse.json(queue);
    }),
  ),
  http.post(
    '/api/orgs/:org/lanes/:ws/:name/readiness',
    org(async (data, { params, request }) => {
      const body = await jsonBody<{ state: Readiness; note?: string }>(request);
      const next = setLane(data, `${params.ws}/${params.name}`, { readiness: body.state, readiness_note: body.note ?? null });
      return next ? new HttpResponse(null, { status: 204 }) : failure(404, 'lane_not_found', 'No such lane.');
    }),
  ),

  // Metrics
  http.get(
    '/api/orgs/:org/metrics/activity',
    org((data, { request }) => {
      const search = new URL(request.url).searchParams;
      const range = (search.get('range') ?? '24h') as ActivityRange;
      return HttpResponse.json(activityMetrics(data, range, { workspace: search.get('workspace'), lane: search.get('lane') }));
    }),
  ),
  http.get(
    '/api/orgs/:org/nodes/:id/metrics',
    org((data, { params, request }) => {
      const node = data.nodes.find((item) => item.id === params.id);
      if (!node) return notFound();
      const range = (new URL(request.url).searchParams.get('range') ?? '24h') as NodeRange;
      return HttpResponse.json(nodeMetrics(node, range));
    }),
  ),

  // Activity and runs
  http.get(
    '/api/orgs/:org/activity',
    org((data, { request }) => {
      const search = new URL(request.url).searchParams;
      const get = (key: string) => search.get(key);
      const items = data.activity.filter(
        (item) =>
          (!get('workspace') || item.lane.startsWith(`${get('workspace')}/`)) &&
          (!get('lane') || item.lane === get('lane')) &&
          (!get('principal') || principalId(item.principal) === get('principal')) &&
          (!get('effect') || item.effect === get('effect')) &&
          (!get('status') || item.status === get('status')) &&
          (!get('from') || item.at >= String(get('from'))) &&
          (!get('to') || item.at < String(get('to'))),
      );
      return HttpResponse.json(paged(items, get('before'), Number(get('limit') ?? 50)));
    }),
  ),
  http.get(
    '/api/orgs/:org/history',
    org((data, { request }) => {
      const search = new URL(request.url).searchParams;
      return HttpResponse.json(
        historyPage(data, { workspace: search.get('workspace'), show: search.get('show'), before: search.get('before'), days: Number(search.get('days') ?? 7) }),
      );
    }),
  ),
  http.get('/api/orgs/:org/runs', org((data) => HttpResponse.json(data.runs.slice(0, 50)))),
  http.post(
    '/api/orgs/:org/runs/:id/cancel',
    org((data, { params }) => {
      const run = data.runs.find((item) => item.id === params.id);
      if (!run) return notFound();
      run.status = 'cancelled';
      run.finished_at = iso();
      emit(data.org.slug, { type: 'run', payload: run });
      return HttpResponse.json(run);
    }),
  ),
  http.get('/api/orgs/:org/runs/:id/proof.png', ({ params }) => svg(postPreview(String(params.id)))),
  http.get(
    '/api/orgs/:org/runs/:id',
    org((data, { params }) => {
      const run = data.runs.find((item) => item.id === params.id);
      return run ? HttpResponse.json(run) : notFound();
    }),
  ),

  // Flows and schedules
  http.get('/api/orgs/:org/flows', org((data) => HttpResponse.json(data.flows.map((flow) => withAsk(data, withRoutine(data, flow)))))),
  http.get('/api/orgs/:org/routines/day', org((data) => HttpResponse.json(routineDay(data)))),
  http.get('/api/orgs/:org/routines/catalog', org((data) => HttpResponse.json(routineCatalog(data)))),
  http.get('/api/orgs/:org/flows/:id/runs', org((data, { params }) => HttpResponse.json(routineRuns(data, String(params.id))))),
  http.post(
    '/api/orgs/:org/flows/:id/runs/:run/retry',
    org((data, { params }) => {
      const run = retryRun(data, String(params.id), String(params.run));
      return run ? HttpResponse.json(run) : notFound();
    }),
  ),
  http.put(
    '/api/orgs/:org/flows/:id/routine',
    org(async (data, { params, request }) => {
      const flow = data.flows.find((item) => item.id === params.id);
      if (!flow) return notFound();
      const body = await jsonBody<RoutineDefinition>(request);
      saveRoutine(data, flow.id, body);
      return HttpResponse.json(withAsk(data, withRoutine(data, flow)));
    }),
  ),
  http.post(
    '/api/orgs/:org/flows',
    org(async (data, { request }) => {
      const body = await jsonBody<RoutineDefinition>(request);
      const workspace = body.workspace ?? data.workspaces[0]?.id ?? 'aster';
      const id = `${workspace}.routine.${body.name.toLowerCase().replace(/[^a-z0-9]+/g, '-')}`;
      const lane = data.lanes.find((item) => item.workspace === workspace)?.id ?? `${workspace}/mail`;
      // Saved is not on: the node keeps a new routine off until a person turns it on.
      const flow = { id, lane, version: 1, params: {}, effect: 'commit' as const, last_run: null, recent_runs: [], enabled: false };
      data.flows.unshift(flow);
      saveRoutine(data, id, body);
      return HttpResponse.json(withAsk(data, flow), { status: 201 });
    }),
  ),
  http.post(
    '/api/orgs/:org/flows/:id/enable',
    org((data, { params }) => {
      const flow = data.flows.find((item) => item.id === params.id);
      if (!flow) return notFound();
      flow.enabled = true;
      return HttpResponse.json(withAsk(data, withRoutine(data, flow)));
    }),
  ),
  http.put(
    '/api/orgs/:org/flows/:id/ask-first',
    org(async (data, { params, request }) => {
      const { ask_first: on } = await jsonBody<{ ask_first: boolean }>(request);
      const refused = askFirstRefusal(data, on);
      if (refused) return refused;
      const flow = data.flows.find((item) => item.id === params.id);
      if (!flow) return notFound();
      if (!on && withAsk(data, flow).always_asks) return failure(400, 'invalid', ALWAYS_MESSAGE);
      const stopped = computerRefusal(data, flow.lane, on);
      if (stopped) return stopped;
      applyAsk(data, 'flow', flow.id, on, db.user.id);
      return HttpResponse.json(withAsk(data, flow));
    }),
  ),
  http.get('/api/orgs/:org/ask-first', org((data) => HttpResponse.json(data.lanes.map((lane) => laneAsk(data, lane.id))))),
  http.put(
    '/api/orgs/:org/lanes/:ws/:name/ask-first',
    org(async (data, { params, request }) => {
      const { ask_first: on } = await jsonBody<{ ask_first: boolean }>(request);
      const refused = askFirstRefusal(data, on);
      if (refused) return refused;
      const id = `${params.ws}/${params.name}`;
      if (!data.lanes.some((lane) => lane.id === id)) return notFound();
      const stopped = computerRefusal(data, id, on);
      if (stopped) return stopped;
      applyAsk(data, 'lane', id, on, db.user.id);
      return HttpResponse.json(laneAsk(data, id));
    }),
  ),
  http.post(
    '/api/orgs/:org/flows/:id/run',
    org((data, { params }) => {
      const flow = data.flows.find((item) => item.id === params.id);
      if (!flow) return notFound();
      const run = {
        id: newId('run'),
        flow: flow.id,
        lane: flow.lane,
        status: withAsk(data, flow).asks_first ? ('waiting_approval' as const) : ('queued' as const),
        summary: `Started ${flow.id}`,
        principal: { kind: 'human' as const, id: db.user.id },
        started_at: iso(),
        finished_at: null,
        cost: { tokens: 0, usd: 0 },
        proof: null,
        steps: [],
      };
      data.runs.unshift(run);
      flow.last_run = { id: run.id, status: 'running', started_at: run.started_at, cost: run.cost };
      return HttpResponse.json(run, { status: 201 });
    }),
  ),
  http.get('/api/orgs/:org/schedules', org((data) => HttpResponse.json(data.schedules))),
  http.post(
    '/api/orgs/:org/schedules',
    org(async (data, { request }) => {
      const body = await jsonBody<Omit<Schedule, 'id'>>(request);
      const schedule = { ...body, id: newId('sch'), next_run: body.cron ? iso(3_600_000) : null };
      data.schedules.push(schedule);
      return HttpResponse.json(schedule, { status: 201 });
    }),
  ),
  http.patch(
    '/api/orgs/:org/schedules/:id',
    org(async (data, { params, request }) => {
      const { enabled } = await jsonBody<{ enabled: boolean }>(request);
      const schedule = data.schedules.find((item) => item.id === params.id);
      if (!schedule) return notFound();
      schedule.enabled = enabled;
      schedule.next_fire_at = enabled && schedule.cron ? iso(3 * 3_600_000) : null;
      schedule.next_run = schedule.next_fire_at;
      emit(data.org.slug, { type: 'schedule', payload: schedule });
      return HttpResponse.json(schedule);
    }),
  ),
  http.delete(
    '/api/orgs/:org/schedules/:id',
    org((data, { params }) => {
      data.schedules = data.schedules.filter((item) => item.id !== params.id);
      return new HttpResponse(null, { status: 204 });
    }),
  ),

  // Nodes
  http.get('/api/orgs/:org/nodes', org((data) => HttpResponse.json(data.nodes.map((node) => ({ ...nodeResource(node), ...nodeBusy(data, node.id) }))))),
  http.post(
    '/api/orgs/:org/nodes/join-tokens',
    org(async (data, { request }) => {
      const body = await jsonBody<{ name?: string }>(request);
      const token = `jt_${newId('x').slice(2)}${newId('x').slice(2)}`.toLowerCase();
      const record = { id: newId('jt'), name: body.name ?? null, expires_at: iso(3_600_000) };
      data.joinTokens.push(record);
      enrollAfterDelay(data, record.id, body.name);
      return HttpResponse.json(
        { ...record, token, install_command: 'curl -fsSL https://cloud.openajar.example/install.sh -o openajar-install.sh && sh openajar-install.sh' },
        { status: 201 },
      );
    }),
  ),
  http.get(
    '/api/orgs/:org/nodes/:id',
    org((data, { params }) => {
      const node = data.nodes.find((item) => item.id === params.id);
      if (!node) return notFound();
      const lanes = data.lanes.filter((lane) => lane.node === node.id).map((lane) => ({ ...lane, ...laneShare(lane) }));
      return HttpResponse.json({ ...node, ...nodeBusy(data, node.id), lanes });
    }),
  ),
  ...nodeReport('system', (_data, node) => nodeSystem(node)),
  ...nodeReport('programs', nodePrograms),
  ...nodeReport('services', nodeServices),
  ...nodeReport('tools', () => nodeTools()),
  ...nodeReport('health', nodeHealth),
  ...nodeReport('schedules', nodeSchedules),
  http.get(
    '/api/orgs/:org/nodes/:id/connections',
    org((data, { params }) => {
      const node = data.nodes.find((item) => item.id === params.id);
      return node ? HttpResponse.json(nodeConnections(node)) : notFound();
    }),
  ),

  // The fleet map
  http.get(
    '/api/orgs/:org/topology',
    org((data, { request }) => {
      const window = new URL(request.url).searchParams.get('window') === '24h' ? 86_400 : 3_600;
      return HttpResponse.json(topology(data, window));
    }),
  ),
  http.delete(
    '/api/orgs/:org/nodes/:id',
    org((data, { params }) => {
      if (!stepUpValid()) return stepUpNeeded();
      data.nodes = data.nodes.filter((item) => item.id !== params.id);
      return new HttpResponse(null, { status: 204 });
    }),
  ),

  // Brains, vault, grants
  http.get('/api/orgs/:org/brains', org((data) => HttpResponse.json(workerBrains(data)))),
  http.post(
    '/api/orgs/:org/brains/order',
    org(async (data, { request }) => {
      const { order } = await jsonBody<{ order: string[] }>(request);
      data.brains.sort((a, b) => order.indexOf(a.name) - order.indexOf(b.name));
      return new HttpResponse(null, { status: 204 });
    }),
  ),
  http.get('/api/orgs/:org/vault', org((data) => HttpResponse.json(data.vault))),
  http.get('/api/orgs/:org/grants', org((data) => HttpResponse.json(data.grants))),
  http.post(
    '/api/orgs/:org/grants',
    org(async (data, { request }) => {
      const body = await jsonBody<NewGrant>(request);
      const heavy: Effect[] = ['pay', 'irreversible'];
      if (heavy.includes(body.ceiling) && !stepUpValid()) return stepUpNeeded();
      const id = newId('grt');
      data.grants.unshift({
        id,
        subject: body.subject,
        workspaces: body.workspaces,
        lanes: body.lanes ?? null,
        ceiling: body.ceiling,
        specials: body.specials,
        budget: body.budget ?? null,
        expires_at: iso(30 * 86_400_000),
        revoked: false,
        created_at: iso(),
      });
      return HttpResponse.json({ id, token: `agt_${newId('x').slice(2)}${newId('x').slice(2)}`.toLowerCase() }, { status: 201 });
    }),
  ),
  http.post(
    '/api/orgs/:org/grants/:id/revoke',
    org((data, { params }) => {
      const grant = data.grants.find((item) => item.id === params.id);
      if (!grant) return notFound();
      grant.revoked = true;
      return new HttpResponse(null, { status: 204 });
    }),
  ),
  // Billing
  http.post(
    '/api/orgs/:org/billing/portal',
    org((data) => {
      if (!stepUpValid()) return stepUpNeeded();
      return HttpResponse.json({ url: `/${data.org.slug}/settings/billing` });
    }),
  ),

  // Notifications
  http.get('/api/orgs/:org/settings/notifications', org((data) => HttpResponse.json(withZone(data.notifications)))),
  http.put(
    '/api/orgs/:org/settings/notifications',
    org(async (data, { request }) => {
      const body = await jsonBody<NotificationSettings & { webhook_url?: string | null }>(request);
      if (body.webhook_url) return failure(422, 'invalid', 'Add webhooks under Webhooks. This setting no longer sends anything.');
      const { timezone: _zone, timezone_source: _source, digest: _digest, ...saved } = body;
      data.notifications = saved;
      return HttpResponse.json(withZone(data.notifications));
    }),
  ),
  http.post('/api/orgs/:org/settings/notifications/test', org(() => new HttpResponse(null, { status: 204 }))),
  ...s3Handlers({ org, failure, stepUpNeeded, stepUpValid, newId }),
  ...siteApiHandlers({ org, failure }),
  ...emailHandlers({ org, failure }),
  ...memoryHandlers({ org, failure }),
  ...filesHandlers({ org, failure }),
  ...workerHandlers({ org, failure, stepUpNeeded, stepUpValid }),
  ...proofHandlers({ org, failure, newId, data: () => db }),
  ...brainTaskHandlers({ org, failure }),
  // Before the runs: an environment's own runs answer there, and every other run falls through.
  ...environmentHandlers({ org, failure, data: () => db }),
  ...jobHandlers({ org, failure }),
  ...studioHandlers({ org, failure }),
  ...brandHandlers({ org, failure }),
  ...overrideHandlers({ org, failure }),
  ...memberDeviceHandlers({ org, failure }),
  ...accessHandlers({ org, failure }),
  ...opsHandlers({ org, failure }),
  ...confirmationHandlers({ org, failure, stepUpNeeded, stepUpValid }),
];

function passkeyOptions() {
  return {
    challenge: 'bW9jay1jaGFsbGVuZ2U',
    rpId: location.hostname,
    timeout: 60_000,
    userVerification: 'preferred',
    allowCredentials: [],
    user: { id: 'dXNyX0RFTU8', name: db.user.email, displayName: db.user.name },
    pubKeyCredParams: [{ type: 'public-key', alg: -7 }],
  };
}
