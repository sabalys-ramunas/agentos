/**
 * The engagement concept on the mock: accounts that like, follow, comment and post by a plan. One
 * account runs an approved plan with a rate-limit pause, one has a plan waiting for the owner, one
 * has a captcha pause a person must clear, and one has no plan at all. The mock is the owner's
 * view, so every write works here; the node refuses a token and a member, as tested on the node.
 * The numbers are a demonstration of a node that runs its sessions, not a measurement.
 */
import { http, HttpResponse } from 'msw';
import type {
  ConnectorOperation,
  ConnectorSurface,
  EngageAccountDay,
  EngageCapRow,
  EngageCapUse,
  EngageDay,
  EngageDays,
  EngageDraftWaiting,
  EngageHold,
  EngageItem,
  EngageKind,
  EngageOperation,
  EngageProgram,
  EngageVersion,
  EngageWeekDay,
  EngageWindow,
  NewSurface,
  Surface,
} from '@/api/types';
import type { OrgData } from './seed';
import type { Helpers } from './s3';

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** The engine's hard ceilings on the sites the mock engages (docs/ENGAGEMENT.md "Caps"). */
const CEILING: Partial<Record<string, Partial<Record<EngageKind, number>>>> = {
  instagram: { like: 80, comment: 10, follow: 20, story_view: 80, post: 3 },
  reddit: { comment: 8, join: 3, post: 1 },
};
const HOURLY: Partial<Record<string, Partial<Record<EngageKind, number>>>> = {
  instagram: { like: 15, comment: 3, follow: 5, story_view: 30, post: 1 },
  reddit: { comment: 2, join: 1, post: 1 },
};
const KINDS: Record<string, EngageKind[]> = {
  instagram: ['like', 'comment', 'follow', 'story_view'],
  reddit: ['comment', 'join'],
};

interface Account {
  surface: string;
  site: string;
  lane: string;
  acts_as: Surface['acts_as'];
  versions: (EngageVersion & { yaml: string; summary: string; risk: 'careful' | 'normal' | 'off' })[];
  caps: Partial<Record<EngageKind, number>>;
  holds: (EngageHold & { cleared: boolean })[];
  items: EngageItem[];
  draftsWaiting: EngageDraftWaiting[];
  /** Days from today, back to the first day the account ran (a weekly rhythm). */
  seed: number;
}

interface EngageStore {
  accounts: Map<string, Account>;
  surfaces: Map<string, Surface>;
}

const stores = new WeakMap<OrgData, EngageStore>();

const PROGRAM = {
  aster: (approved: boolean) => ({
    id: 'aster-ig-coffee',
    yaml: 'program: aster-ig-coffee\nsurface: aster/instagram\nsite: instagram\nrisk: careful\ntimezone: Europe/Vilnius\nphases:\n- kind: steady\n  daily:\n    like: 15\n    comment: 3\n    follow: 2\n    story_view: 10\n  targets:\n  - source: own_feed\n',
    summary: 'aster/instagram on instagram. Careful pace. Then about 3 comments, 2 follows, 15 likes, 10 story views a day, spread over 2 to 4 short visits. Finds posts in: own feed. You approve every comment before it goes out.',
    approved,
  }),
  harbor: {
    id: 'harbor-reddit-supply',
    yaml: 'program: harbor-reddit-supply\nsurface: harbor/reddit\nsite: reddit\nrisk: careful\ntimezone: UTC\nphases:\n- kind: steady\n  daily:\n    comment: 1\n    join: 1\n  targets:\n  - source: communities\n    values: [supplies, woodworking]\n',
    summary: 'harbor/reddit on reddit. Careful pace. Then about 1 comment and 1 join a day, spread over 1 to 2 short visits. Finds posts in: supplies and woodworking. You approve every comment before it goes out.',
  },
  meridian: {
    id: 'meridian-ig-travel',
    yaml: 'program: meridian-ig-travel\nsurface: meridian/instagram\nsite: instagram\nrisk: careful\ntimezone: UTC\nphases:\n- kind: steady\n  daily:\n    like: 5\n    follow: 1\n  targets:\n  - source: own_feed\n',
    summary: 'meridian/instagram on instagram. Careful pace. Then about 5 likes and 1 follow a day. Finds posts in: own feed.',
  },
};

/** A seeded number, so a day reads the same every time it is opened. */
function rand(seed: string): () => number {
  let h = 2166136261;
  for (const ch of seed) h = Math.imul(h ^ ch.charCodeAt(0), 16777619);
  return () => {
    h = Math.imul(h ^ (h >>> 15), 2246822507);
    h = Math.imul(h ^ (h >>> 13), 3266489909);
    return ((h >>> 0) % 10_000) / 10_000;
  };
}

function dayOf(now: number, back: number): string {
  return new Date(now - back * DAY).toISOString().slice(0, 10);
}

function ts(day: string, hour: number, minute = 0): string {
  return `${day}T${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}:00+00:00`;
}

function accountsOf(now: number): Account[] {
  const created = now - 12 * DAY;
  return [
    {
      surface: 'aster/instagram', site: 'instagram', lane: 'aster/instagram', seed: 1,
      acts_as: { kind: 'ig_business', name: '@aster.coffee', id: '17841400000101', user_id: null },
      versions: [{ program: PROGRAM.aster(true).id, version: 1, state: 'approved', hash: 'sha256:9d6e692c9ab22916', created_at: new Date(created).toISOString(), created_by: 'owner', approved_at: new Date(created + HOUR).toISOString(), approved_by: 'owner', yaml: PROGRAM.aster(true).yaml, summary: PROGRAM.aster(true).summary, risk: 'careful' }],
      caps: { like: 20 }, holds: [{
        id: 1, scope: 'surface', kind: null, signal: 'rate_limit', reason: 'signal', cleared: false,
        detail: 'A rate limit rests the account for a few hours. Nothing is sent until it ends.', since: ts(dayOf(now, 0), 6), until: new Date(now + 4 * HOUR).toISOString(),
        cleared_at: null, created_that_day: true, active_now: true,
      }], items: [], draftsWaiting: [
        { id: 'dr_7c1e02', kind: 'comment', approval_id: 'ap_comment_1', target: 'a latte art post by a neighbour', created_at: ts(dayOf(now, 1), 18, 40), run_id: 'run_ig_1', preview: 'Those rosettas are crisp. Is that the new milk jug?', day: dayOf(now, 1) },
      ],
    },
    {
      surface: 'harbor/reddit', site: 'reddit', lane: 'harbor/reddit', seed: 2,
      acts_as: { kind: 'account', name: 'u/harbor_supply', id: null, user_id: 'harbor_supply' },
      versions: [{ program: PROGRAM.harbor.id, version: 1, state: 'approved', hash: 'sha256:4b1a0c9e7d2f5a31', created_at: new Date(created).toISOString(), created_by: 'owner', approved_at: new Date(created + HOUR).toISOString(), approved_by: 'owner', yaml: PROGRAM.harbor.yaml, summary: PROGRAM.harbor.summary, risk: 'careful' }],
      caps: {}, holds: [{
        id: 2, scope: 'surface', kind: null, signal: 'captcha', reason: 'signal', cleared: false,
        detail: 'The site showed a robot check. A person looks at the account first.', since: ts(dayOf(now, 0), 9, 12), until: null,
        cleared_at: null, created_that_day: true, active_now: true,
      }], items: [
        { id: 'it_4a10d9', surface: 'harbor/reddit', kind: 'post', status: 'queued', title: 'New stock on the bench', targets: ['r/woodworking'], attachments: [], files: [], expires_at: new Date(now + 4 * DAY).toISOString(), submitted_by: 'owner', submitted_at: new Date(now - 2 * HOUR).toISOString(), requested_by: null, used_run_id: null, settled_at: null, text: 'Two new bench planes are in, and the walnut came in on Monday. Come and see them on Saturday.' },
      ], draftsWaiting: [],
    },
    {
      surface: 'meridian/instagram', site: 'instagram', lane: 'meridian/instagram', seed: 3,
      acts_as: { kind: 'ig_business', name: '@meridian.travel', id: '17841400000303', user_id: null },
      versions: [{ program: PROGRAM.meridian.id, version: 1, state: 'draft', hash: 'sha256:c08e3f11a4b2d7e9', created_at: new Date(now - 2 * HOUR).toISOString(), created_by: 'claude-code', approved_at: null, approved_by: null, yaml: PROGRAM.meridian.yaml, summary: PROGRAM.meridian.summary, risk: 'careful' }],
      caps: {}, holds: [], items: [], draftsWaiting: [],
    },
  ];
}

function storeOf(data: OrgData, now = Date.now()): EngageStore {
  const known = stores.get(data);
  if (known) return known;
  const accounts = new Map(accountsOf(now).map((account) => [account.surface, account]));
  const surfaces = new Map<string, Surface>();
  for (const account of accounts.values()) {
    const [workspace = '', name = ''] = account.surface.split('/');
    surfaces.set(account.surface, {
      id: account.surface, workspace, name, site: account.site, lane: account.lane, acts_as: account.acts_as,
      account_language: 'en', title: null,
    });
  }
  const store = { accounts, surfaces };
  stores.set(data, store);
  return store;
}

function programOf(account: Account): EngageProgram['program'] {
  const shown = account.versions.find((v) => v.state === 'approved') ?? account.versions[account.versions.length - 1];
  if (!shown) return null;
  return {
    id: shown.program, version: shown.version, hash: shown.hash, state: shown.state, approved_at: shown.approved_at,
    approved_by: shown.approved_by, risk: shown.risk, summary: shown.summary, yaml: shown.yaml,
  };
}

function stateOf(account: Account): EngageProgram['state'] {
  const shown = account.versions.find((v) => v.state === 'approved') ? 'approved' : account.versions.length ? 'draft' : null;
  return shown;
}

function capRows(account: Account): EngageCapRow[] {
  const kinds = KINDS[account.site] ?? [];
  return kinds.map((kind) => ({
    kind, careful_start: 1, careful_ceiling: Math.max(1, Math.floor((CEILING[account.site]?.[kind] ?? 1) / 2)),
    normal_start: 2, normal_ceiling: CEILING[account.site]?.[kind] ?? 1, hard_ceiling: CEILING[account.site]?.[kind] ?? 1,
    hourly: HOURLY[account.site]?.[kind] ?? 1, manual: account.caps[kind] ?? null,
    today: account.versions.some((v) => v.state === 'approved') ? Math.min(CEILING[account.site]?.[kind] ?? 1, (HOURLY[account.site]?.[kind] ?? 1) * 4) : null,
    hourly_today: HOURLY[account.site]?.[kind] ?? 1,
  }));
}

function dayRecord(account: Account, day: string, now: number): EngageDay {
  const approved = account.versions.some((v) => v.state === 'approved');
  const rng = rand(`${account.surface}:${day}`);
  const kinds = KINDS[account.site] ?? [];
  const ran = approved;
  const operations: EngageOperation[] = ran ? kinds.map((kind) => {
    const planned = Math.max(1, Math.round((CEILING[account.site]?.[kind] ?? 2) * (0.2 + rng() * 0.4)));
    const attempted = Math.max(0, planned - Math.floor(rng() * 2));
    const verified = Math.max(0, attempted - Math.floor(rng() * 2));
    const skipped: Record<string, number> = planned > attempted ? { cap: planned - attempted } : {};
    return {
      kind, planned, moved_in: 0, attempted, verified, unverified: attempted - verified, failed: 0,
      awaiting_approval: kind === 'comment' ? 1 : 0, no_outcome: 0, held: 0, skipped: planned - attempted,
      skip_reasons: skipped, pending: 0, failed_reasons: {}, held_reasons: {},
    };
  }) : [];
  const windows: EngageWindow[] = ran ? [0, 1, 2].map((idx) => ({
    idx, origin: 'plan', planned_start: ts(day, 8 + idx * 5, 15 + idx * 20), minutes: 9 + idx * 3,
    started_at: ts(day, 8 + idx * 5, 16 + idx * 20), ended_at: ts(day, 8 + idx * 5, 26 + idx * 20),
    status: 'done', end_reason: null, operations: {},
  })) : [];
  // Used never goes past the limit the account runs under (the owner's cap, else the plan's, else the ceiling).
  const caps: EngageCapUse[] = capRows(account).map((row) => {
    const limit = row.manual ?? row.today ?? row.hard_ceiling;
    return {
      kind: row.kind, used: ran ? Math.min(limit, Math.floor(rng() * limit)) : 0,
      cap: row.manual ?? row.today, ceiling: row.hard_ceiling, hourly: row.hourly_today,
    };
  });
  const waiting = account.draftsWaiting.filter((d) => d.day === day);
  return {
    schema: 1, surface: account.surface, site: account.site, day, timezone: 'UTC', timezone_fallback: null,
    program: programOf(account) ? { id: programOf(account)!.id, version: programOf(account)!.version, approved } : null,
    sessions_on: true, plan_saved: ran, quiet: false, phase: approved ? 'steady' : null,
    sessions: {
      planned: windows.length, rescheduled: 0, run: windows.length, scheduled: 0, running: 0, done: windows.length,
      ended_early: 0, aborted: 0, refused: 0, missed: 0, windows,
    },
    totals: {
      planned: operations.reduce((n, op) => n + op.planned, 0), attempted: operations.reduce((n, op) => n + op.attempted, 0),
      verified: operations.reduce((n, op) => n + op.verified, 0), failed: 0,
      awaiting_approval: operations.reduce((n, op) => n + op.awaiting_approval, 0), skipped: operations.reduce((n, op) => n + op.skipped, 0),
    },
    operations, caps,
    drafts: { awaiting_approval: waiting.length, created: waiting.length, failed: 0, expired: 0, provider_paused: false, waiting },
    awaiting_now: { count: account.draftsWaiting.length, days: {}, drafts: account.draftsWaiting },
    items: {
      submitted: account.items.length, used: 0, failed: 0, expired: 0, withdrawn: account.items.filter((i) => i.status === 'withdrawn').length,
      queued_now: account.items.filter((i) => i.status === 'queued').length, awaiting_now: 0,
    },
    holds: account.holds.map((h) => ({ ...h, active_now: !h.cleared && (h.until === null || Date.parse(h.until) > now), created_that_day: h.since.slice(0, 10) === day })),
    signals: [], notices: [],
  };
}

function weekOf(account: Account, day: string, now: number): EngageWeekDay[] {
  const end = Date.parse(`${day}T12:00:00Z`);
  const days = Array.from({ length: 7 }, (_, i) => dayRecord(account, new Date(end - (6 - i) * DAY).toISOString().slice(0, 10), now));
  return days.map((d) => ({
    day: d.day, sessions_planned: d.sessions.planned, sessions_rescheduled: 0, sessions_run: d.sessions.run,
    attempted: d.totals.attempted, verified: d.totals.verified, awaiting_approval: d.totals.awaiting_approval,
    holds: d.holds.length, signals: 0,
  }));
}

const ALL_OPERATIONS: ConnectorOperation[] = [
  { operation: 'reddit.post_text', tool: 'reddit_post_text', title: 'Post text to a subreddit', effect: 'commit', acts_as: 'account', version: '2026.11', contract: 'sha256:5a2e', surfaces: [{ surface: 'harbor/reddit', acts_as: 'u/harbor_supply', health: 'healthy', lane: 'harbor/reddit' }] },
  { operation: 'reddit.comment', tool: 'reddit_comment', title: 'Comment on a post', effect: 'commit', acts_as: 'account', version: '2026.11', contract: 'sha256:7f10', surfaces: [{ surface: 'harbor/reddit', acts_as: 'u/harbor_supply', health: 'held', lane: 'harbor/reddit', health_detail: { since: '2026-11-07T09:12:00Z', code: 'captcha' } }] },
  { operation: 'reddit.browse_subreddit', tool: 'reddit_browse_subreddit', title: 'Read the newest posts in a subreddit', effect: 'read', acts_as: 'account', version: '2026.11', contract: 'sha256:0c3d', surfaces: [{ surface: 'harbor/reddit', acts_as: 'u/harbor_supply', health: 'unknown', lane: 'harbor/reddit' }] },
];

/** The surface and its operations' health, so one account shows what it can do and what is held. */
function operationsOn(surface: string): ConnectorOperation[] {
  return ALL_OPERATIONS.flatMap((op) => {
    const here = op.surfaces.filter((s: ConnectorSurface) => s.surface === surface);
    return here.length ? [{ ...op, surfaces: here }] : [];
  });
}

function json<T>(request: Request): Promise<T> {
  return request.clone().json() as Promise<T>;
}

export function engageHandlers({ org, failure }: Pick<Helpers, 'org' | 'failure'>) {
  const notFound = () => failure(404, 'not_found', 'Not found.');
  const now = () => Date.now();
  const dayParam = (url: URL) => url.searchParams.get('day') ?? dayOf(now(), 1);
  const surfaceOf = (ws: string, name: string) => `${ws}/${name}`;

  return [
    http.get(
      '/api/orgs/:org/engage/digest',
      org((data, { request }) => {
        const store = storeOf(data);
        const day = dayParam(new URL(request.url));
        const surfaces: EngageDay[] = [...store.accounts.values()].map((a) => dayRecord(a, day, now()));
        const out: EngageDays = { available: true, schema: 1, sessions_on: true, surfaces };
        return HttpResponse.json(out);
      }),
    ),
    http.get(
      '/api/orgs/:org/engage/digest/:ws/:name',
      org((data, { params, request }) => {
        const account = storeOf(data).accounts.get(surfaceOf(String(params.ws), String(params.name)));
        if (!account) return notFound();
        const day = dayParam(new URL(request.url));
        const out: EngageAccountDay = { available: true, record: dayRecord(account, day, now()), week: weekOf(account, day, now()) };
        return HttpResponse.json(out);
      }),
    ),
    http.get(
      '/api/orgs/:org/engage/programs/:ws/:name',
      org((data, { params }) => {
        const surface = surfaceOf(String(params.ws), String(params.name));
        const account = storeOf(data).accounts.get(surface);
        if (!account) {
          const out: EngageProgram = { available: true, surface, site: null, state: null, program: null, versions: [], caps: [], items: {}, may_change: true };
          return HttpResponse.json(out);
        }
        const out: EngageProgram = {
          available: true, surface, site: account.site, state: stateOf(account), program: programOf(account),
          versions: account.versions.map(({ yaml: _yaml, summary: _summary, risk: _risk, ...v }) => v as EngageVersion),
          caps: capRows(account), items: { queued: account.items.filter((i) => i.status === 'queued').length }, may_change: true,
        };
        return HttpResponse.json(out);
      }),
    ),
    http.post(
      '/api/orgs/:org/engage/programs/:program/approve',
      org(async (data, { params, request }) => {
        const body: { version?: number } = await json<{ version?: number }>(request).catch(() => ({}));
        const store = storeOf(data);
        const account = [...store.accounts.values()].find((a) => a.versions.some((v) => v.program === params.program));
        if (!account) return notFound();
        const version = body.version ? account.versions.find((v) => v.version === body.version) : account.versions[account.versions.length - 1];
        if (!version) return failure(404, 'not_found', `Program ${params.program} has no version ${body.version}.`);
        for (const v of account.versions) if (v.state === 'approved' && v !== version) v.state = 'superseded';
        version.state = 'approved';
        version.approved_at = new Date(now()).toISOString();
        version.approved_by = 'owner';
        const out: EngageProgram = {
          available: true, surface: account.surface, site: account.site, state: 'approved', program: programOf(account),
          versions: account.versions.map(({ yaml: _yaml, summary: _summary, risk: _risk, ...v }) => v as EngageVersion),
          caps: capRows(account), items: {}, may_change: true,
        };
        return HttpResponse.json(out);
      }),
    ),
    http.put(
      '/api/orgs/:org/engage/caps/:ws/:name/:kind',
      org(async (data, { params, request }) => {
        const body = await json<{ value: number | null }>(request);
        const kind = String(params.kind) as EngageKind;
        const account = storeOf(data).accounts.get(surfaceOf(String(params.ws), String(params.name)));
        if (!account) return notFound();
        if (!KINDS[account.site]?.includes(kind)) return failure(400, 'invalid', `${kind} is not a kind of action an account can take`);
        const ceiling = CEILING[account.site]?.[kind] ?? 0;
        if (body.value !== null && (body.value < 0 || body.value > ceiling)) {
          return failure(400, 'cap_ceiling', `${kind} on ${account.site} can go up to ${ceiling} a day (the hard ceiling), not ${body.value}`);
        }
        if (body.value === null) delete account.caps[kind];
        else account.caps[kind] = body.value;
        return HttpResponse.json({ surface: account.surface, kind, cap: body.value, hard_ceiling: ceiling });
      }),
    ),
    http.post(
      '/api/orgs/:org/engage/holds/:ws/:name/clear',
      org(async (data, { params, request }) => {
        const body: { kind?: string | null; id?: number | null } = await json<{ kind?: string | null; id?: number | null }>(request).catch(() => ({}));
        const account = storeOf(data).accounts.get(surfaceOf(String(params.ws), String(params.name)));
        if (!account) return notFound();
        let cleared = 0;
        for (const hold of account.holds) {
          if (hold.cleared) continue;
          if (body.id != null && hold.id !== body.id) continue;
          if (body.kind && hold.kind !== body.kind) continue;
          hold.cleared = true;
          hold.cleared_at = new Date(now()).toISOString();
          cleared += 1;
        }
        return HttpResponse.json({ surface: account.surface, cleared });
      }),
    ),
    http.get(
      '/api/orgs/:org/engage/items/:ws/:name',
      org((data, { params, request }) => {
        const account = storeOf(data).accounts.get(surfaceOf(String(params.ws), String(params.name)));
        if (!account) return notFound();
        const status = new URL(request.url).searchParams.get('status');
        const items = account.items.filter((i) => !status || i.status === status);
        return HttpResponse.json({ available: true, surface: account.surface, items });
      }),
    ),
    http.post(
      '/api/orgs/:org/engage/items/:item/withdraw',
      org((data, { params }) => {
        const store = storeOf(data);
        const item = [...store.accounts.values()].flatMap((a) => a.items).find((i) => i.id === params.item);
        if (!item) return notFound();
        if (item.status !== 'queued') return failure(400, 'invalid', `${item.id} is ${item.status}, so it cannot be withdrawn now`);
        item.status = 'withdrawn';
        return HttpResponse.json(item);
      }),
    ),
    http.get(
      '/api/orgs/:org/surfaces',
      org((data) => HttpResponse.json([...storeOf(data).surfaces.values()])),
    ),
    http.get(
      '/api/orgs/:org/surfaces/:ws/:name',
      org((data, { params }) => {
        const surface = storeOf(data).surfaces.get(surfaceOf(String(params.ws), String(params.name)));
        return surface ? HttpResponse.json(surface) : notFound();
      }),
    ),
    http.post(
      '/api/orgs/:org/surfaces',
      org(async (data, { request }) => {
        const body = await json<NewSurface>(request);
        const [workspace = '', name = ''] = body.id.split('/');
        const surface: Surface = {
          id: body.id, workspace, name, site: body.site, lane: body.lane,
          acts_as: { kind: body.acts_as.kind, name: body.acts_as.name ?? null, id: body.acts_as.id ?? null, user_id: body.acts_as.user_id ?? null },
          account_language: body.account_language ?? 'en', title: body.title ?? null,
        };
        storeOf(data).surfaces.set(surface.id, surface);
        return HttpResponse.json(surface);  // the contract answers 200, as the node's route does through it
      }),
    ),
    http.delete(
      '/api/orgs/:org/surfaces/:ws/:name',
      org((data, { params }) => {
        const id = surfaceOf(String(params.ws), String(params.name));
        const store = storeOf(data);
        if (!store.surfaces.has(id)) return notFound();
        store.surfaces.delete(id);
        return HttpResponse.json({ deleted: id });
      }),
    ),
    http.get(
      '/api/orgs/:org/connectors',
      org(() => HttpResponse.json(ALL_OPERATIONS)),
    ),
    http.post(
      '/api/orgs/:org/connectors/:operation/run',
      org(async (_data, { params, request }) => {
        const body = await json<{ on: string }>(request);
        const operation = ALL_OPERATIONS.find((op) => op.operation === params.operation);
        if (!operation) return notFound();
        if (!operationsOn(body.on).length) return failure(400, 'invalid', `${body.on} does not run ${operation.operation}`);
        return HttpResponse.json({ id: `run_${Math.random().toString(36).slice(2, 10)}`, flow: `connector.${operation.operation}`, status: 'queued', duplicate: false }, { status: 202 });
      }),
    ),
  ];
}

/** The day's account rows for the account page's strip and the day list (exported for tests). */
export const engageMock = { accountsOf, dayRecord, storeOf };
