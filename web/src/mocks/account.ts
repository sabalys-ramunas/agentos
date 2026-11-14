/**
 * An account's page as the node answers it (docs/API.md "Posting pace", "Meters, queues and taint",
 * "Lane check", "Lane keepalive", "Lane control"). The records are the owner's choices and what the
 * account is doing; every answer is worked out from them, in the node's own shapes. A change by anyone
 * but the owner is refused here as the node refuses it.
 */
import type {
  Grant,
  LaneCheck,
  LaneCheckBrief,
  LaneKeepalive,
  LaneQueue,
  LaneSites,
  LaneTaint,
  LanePace,
  Limits,
  LimitName,
  Lane,
  Meter,
  Meters,
  MeterLimit,
  Pace,
  TaintFamily,
  ViewLink,
  WorkspaceZone,
} from '@/api/types';
import type { OrgData } from './seed';
import { laneSite } from './rebuild';

/** What the owner set, and what each account is doing, for one org. */
export interface AccountRecords {
  /** A lane's posting pace. `null` once the owner removed it. */
  pace: Record<string, Pace | null>;
  /** A lane's own limits. `null` once the owner removed them. */
  limits: Record<string, Limits | null>;
  sites: Record<string, { sites: string[]; strict: boolean }>;
  /** A workspace's clock, for lanes with no zone of their own. */
  clocks: Record<string, string | null>;
  /** The grant families that read outside text on an account, and what they read. */
  taint: Record<string, { grant: string; read: { kind: string; host: string | null; last: string }[] }>;
  /** Who holds an account now, and who waits for it. */
  claims: Record<string, { holder: string; purpose: string; since: string; waiting: { label: string; mode: string; waited_s: number }[] }>;
  /** Posts made today on each paced account, so the pace can say how many are left. */
  postsToday: Record<string, { count: number; last: string | null }>;
}

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

export const emptyAccount = (): AccountRecords => ({ pace: {}, limits: {}, sites: {}, clocks: {}, taint: {}, claims: {}, postsToday: {} });

/** The seed: the Aster account is paced, limited, reads a web page and is in use right now. */
export function makeAccount(now: number, grants: Grant[]): AccountRecords {
  const reader = grants.find((grant) => grant.subject === 'claude-code-aster')?.id ?? 'grt_mock';
  return {
    pace: {
      'aster/instagram': { commits_per_day: 8, min_gap_minutes: 45, active_hours: ['08:00-22:00'] },
    },
    limits: { 'aster/instagram': { commits_per_day: 6 } },
    sites: { 'aster/instagram': { sites: ['instagram.com', 'frame:www.google.com'], strict: false } },
    clocks: { aster: null },
    taint: {
      'aster/instagram': {
        grant: reader,
        read: [{ kind: 'web_page', host: 'coffee-reviews.example', last: new Date(now - 2 * HOUR).toISOString() }],
      },
    },
    claims: {
      'aster/instagram': {
        holder: 'Claude Code',
        purpose: 'Post the morning photo',
        since: new Date(now - 40_000).toISOString(),
        waiting: [{ label: 'flow run run_9 (weekly-posts)', mode: 'call', waited_s: 12 }],
      },
    },
    postsToday: { 'aster/instagram': { count: 5, last: new Date(now - 2 * HOUR).toISOString() } },
  };
}

const iso = (offset: number) => new Date(Date.now() + offset).toISOString();

/** A lane's clock: "2026-11-07T00:29+02:00", as the node writes a local time. */
export function localIso(at: Date, zone: string): string {
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone: zone,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
    timeZoneName: 'longOffset',
  }).formatToParts(at);
  const get = (type: string) => parts.find((part) => part.type === type)?.value ?? '';
  const offset = get('timeZoneName').replace('GMT', '') || '+00:00';
  return `${get('year')}-${get('month')}-${get('day')}T${get('hour')}:${get('minute')}${offset}`;
}

const zoneOf = (data: OrgData, lane: Lane) => data.nodes.find((node) => node.id === lane.node)?.timezone ?? 'UTC';
const sourceOf = (data: OrgData, lane: Lane): { timezone: string; source: 'lane' | 'pin' | 'workspace' | 'node' } => {
  const clock = data.account.clocks[lane.workspace];
  return clock ? { timezone: clock, source: 'workspace' } : { timezone: zoneOf(data, lane), source: 'node' };
};
const WORDS: Record<'lane' | 'pin' | 'workspace' | 'node', string> = {
  lane: "the account's own zone",
  pin: "its proxy's region",
  workspace: "the business's clock",
  node: "the computer's zone",
};

const clockOf = (iso: string) => iso.slice(11, 16);

/** Whether a plain window "08:00-22:00" holds a clock time, a window that runs past midnight included. */
function inWindow(time: string, window: string): boolean {
  const [from = '', to = ''] = window.split('-');
  return from <= to ? time >= from && time < to : time >= from || time < to;
}

/** The pace answer: what it allows now, and why not, in the node's words. */
export function lanePace(data: OrgData, lane: Lane): LanePace {
  const pace = data.account.pace[lane.id] ?? null;
  const zone = sourceOf(data, lane);
  const today = data.account.postsToday[lane.id] ?? { count: 0, last: null };
  const cap = pace?.commits_per_day ?? null;
  const now = new Date();
  const localNow = localIso(now, zone.timezone);
  const clock = clockOf(localNow);
  const hours = pace?.active_hours?.[0] ?? null;
  const asleep = hours !== null && /^\d{2}:\d{2}-\d{2}:\d{2}$/.test(hours) && !inWindow(clock, hours);
  const blocked = asleep ? 'active_hours' : pace && cap !== null && today.count >= cap ? 'commits_per_day' : null;
  // The next post waits for the window to open, or for tomorrow's cap to reset at midnight.
  const opens = asleep && hours ? hours.split('-')[0] ?? '08:00' : null;
  const todayLocal = localNow.slice(0, 10);
  const tomorrow = new Date(new Date(`${todayLocal}T12:00:00Z`).getTime() + DAY).toISOString().slice(0, 10);
  const nextLocal = opens
    ? `${clock < opens ? todayLocal : tomorrow}T${opens}${localNow.slice(16)}`
    : blocked
      ? `${tomorrow}T00:00${localNow.slice(16)}`
      : null;
  return {
    lane: lane.id,
    pace,
    paced: pace !== null,
    zone: {
      timezone: zone.timezone,
      source: zone.source,
      source_words: WORDS[zone.source],
      exit_timezone: null,
      unreadable: false,
      note: null,
    },
    local_now: localNow,
    workspace_timezone: data.account.clocks[lane.workspace] ?? null,
    preset: { commits_per_day: 3, min_gap_minutes: 45, active_hours: ['08:00-22:00'] },
    posts_today: today.count,
    last_post_at: today.last,
    may_post_now: blocked === null,
    blocked_by: blocked,
    next_allowed_at: nextLocal ? new Date(nextLocal).toISOString() : null,
    next_allowed_local: nextLocal,
  };
}

/** The node's answer to a pace change. A removed pace reads back as not paced. */
export function setLanePace(data: OrgData, lane: Lane, body: { pace?: Pace | null; preset?: boolean }): LanePace {
  data.account.pace[lane.id] = body.preset ? { commits_per_day: 3, min_gap_minutes: 45, active_hours: ['08:00-22:00'] } : (body.pace ?? null);
  return lanePace(data, lane);
}

export function workspaceZone(data: OrgData, workspace: string): WorkspaceZone {
  return { workspace, timezone: data.account.clocks[workspace] ?? null };
}

const LIMIT_UNITS: Record<LimitName, { unit: string; period: 'day' | 'hour' }> = {
  usd_per_day: { unit: 'dollars', period: 'day' },
  tokens_per_day: { unit: 'tokens', period: 'day' },
  commits_per_hour: { unit: 'posts', period: 'hour' },
  commits_per_day: { unit: 'posts', period: 'day' },
};

/** A lane's meter: today's use against its limits. The warning comes at 80 percent, the cutoff at 100. */
export function laneMeter(data: OrgData, lane: Lane): Meter {
  const limits = data.account.limits[lane.id] ?? null;
  const today = data.account.postsToday[lane.id]?.count ?? 0;
  const used: Partial<Record<LimitName, number>> = { commits_per_day: today, commits_per_hour: Math.min(today, 2), tokens_per_day: 18_000, usd_per_day: 0.42 };
  const rows: MeterLimit[] = Object.entries(limits ?? {}).map(([name, value]) => {
    const key = name as LimitName;
    const count = used[key] ?? 0;
    const fraction = value ? count / value : 0;
    const state = fraction >= 1 ? 'cut_off' : fraction >= 0.8 ? 'warning' : 'ok';
    return {
      name: key,
      value: value ?? 0,
      used: count,
      left: Math.max(0, (value ?? 0) - count),
      fraction,
      unit: LIMIT_UNITS[key].unit,
      period: LIMIT_UNITS[key].period,
      resets_at: iso(DAY / 2),
      set_on: 'lane',
      state,
      explain: `${lane.id} has ${count} of ${value} ${LIMIT_UNITS[key].unit} for the ${LIMIT_UNITS[key].period}.`,
    };
  });
  const worst = rows.some((row) => row.state === 'cut_off') ? 'cut_off' : rows.some((row) => row.state === 'warning') ? 'warning' : 'ok';
  const zone = zoneOf(data, lane);
  return {
    scope: 'lane',
    id: lane.id,
    label: lane.id,
    state: worst,
    explain: worst === 'ok' ? null : rows.find((row) => row.state !== 'ok')?.explain ?? null,
    usage: { usd: 0.42, tokens: 18_000, commits_today: today, commits_this_hour: Math.min(today, 2), actions: { read: 30, commit: today }, actions_today: 30 + today },
    limits: rows,
    limits_from: limits ? 'owner' : null,
    day: new Date().toISOString().slice(0, 10),
    timezone: zone,
    warn_at: [0.8],
    resets_at: iso(DAY / 2),
    hour_resets_at: iso(HOUR),
  };
}

export function setLaneLimits(data: OrgData, lane: Lane, limits: Limits | null): Meter {
  data.account.limits[lane.id] = limits;
  return laneMeter(data, lane);
}

/** Every lane's meter for the caller. A member or a grant would see fewer; the mock keeps one view. */
export function allMeters(data: OrgData, lanes: Lane[]): Meters {
  const zone = data.nodes[0]?.timezone ?? 'UTC';
  return {
    timezone: zone,
    day: new Date().toISOString().slice(0, 10),
    resets_at: iso(DAY / 2),
    warn_at: [0.8],
    grants: [],
    lanes: lanes.map((lane) => laneMeter(data, lane)),
  };
}

export function laneQueue(data: OrgData, lane: Lane): LaneQueue {
  const record = data.account.claims[lane.id];
  if (!record) return { lane: lane.id, claim: null, queue_length: 0, queue: [], inside: [], call_estimate_s: null };
  const since = new Date(record.since).getTime();
  const held = Math.round((Date.now() - since) / 1000);
  return {
    lane: lane.id,
    claim: {
      holder: record.holder,
      kind: 'agent',
      ref: 'a1b2c3d4',
      since: record.since,
      purpose: record.purpose,
      held_s: held,
      implicit: false,
      ttl_s: 120,
      expires_in_s: Math.max(0, 120 - held),
      renewable_until_s: Math.max(0, 900 - held),
    },
    queue_length: record.waiting.length,
    queue: record.waiting.map((item, index) => ({
      position: index + 1,
      mode: item.mode,
      label: item.label,
      kind: 'flow',
      ref: `w${index}e5f6a7`,
      waited_s: item.waited_s,
      estimate_s: 20,
      kept_out_by_claim: true,
    })),
    inside: [],
    call_estimate_s: 4,
  };
}

/** The lane's claim and queue, for the lane record. */
export function withClaim(data: OrgData, lane: Lane): Lane {
  const record = data.account.claims[lane.id];
  return { ...lane, claim: record ? laneQueue(data, lane).claim : null, queue_length: record?.waiting.length ?? 0 };
}

/** Taint is kept on the family. The owner sees the grant; anyone else sees what was read, not whose. */
export function laneTaint(data: OrgData, lane: Lane, owner: boolean): LaneTaint {
  const record = data.account.taint[lane.id];
  const grant = data.grants.find((item) => item.id === record?.grant);
  const families: TaintFamily[] = record
    ? [
        {
          ...(owner ? { grant_id: record.grant, grants: [{ id: record.grant, label: grant?.subject ?? 'an agent' }] } : {}),
          context: 'family',
          labels: [],
          untrusted: true,
          since: record.read[0]?.last ?? null,
          follows: record.read,
          after_untrusted: record.read.map((item) => (item.host ? `a web page at ${item.host}` : 'a web page')).join(' and ') || null,
          waits_for_a_person: ['commit', 'pay', 'irreversible'],
          clean_context: 'a new turn token, a fresh grant, or the owner clearing it in person',
        },
      ]
    : [];
  return {
    lane: lane.id,
    tainted: families.length > 0,
    families,
    waits_for_a_person: ['commit', 'pay', 'irreversible', 'exec', 'service', 'install_overlay', 'install_base'],
  };
}

export function clearTaint(data: OrgData, grant: string): { grant_id: string; family: string; origins_cleared: number } | null {
  const lane = Object.keys(data.account.taint).find((id) => data.account.taint[id]?.grant === grant);
  if (!lane) return null;
  const count = data.account.taint[lane]?.read.length ?? 0;
  delete data.account.taint[lane];
  return { grant_id: grant, family: `fam_${grant}`, origins_cleared: count };
}

/** The checklist of one account. The Aster account shows one warning and nothing that stops it. */
export function laneCheck(lane: Lane): LaneCheck {
  const warned = lane.id === 'aster/instagram';
  const level = warned ? 'warn' : 'ok';
  return {
    lane: lane.id,
    level,
    safe: true,
    state: lane.state,
    at: iso(-5 * MINUTE),
    summary: warned
      ? 'WebRTC can send this node\'s own IP through a blank frame. Normal pages stay behind the proxy.'
      : 'Everything the lane needs is in place.',
    counts: { ok: warned ? 7 : 8, unknown: 0, warn: warned ? 1 : 0, fail: 0 },
    checks: [
      { id: 'proxy', title: 'Proxy', level: 'ok', detail: 'The proxy answers.', fix: null },
      {
        id: 'webrtc',
        title: 'WebRTC',
        level,
        detail: warned ? 'A page can make WebRTC send UDP from the node\'s own address.' : 'WebRTC stays behind the proxy.',
        fix: warned ? 'Add the WebRTC setting to the profile, then check again.' : null,
      },
      { id: 'hold', title: 'Hold', level: 'ok', detail: 'No hold, no quarantine; agents have control.', fix: null },
    ],
  };
}

/** The brief each account's health comes from, with no probe. */
export function checkBriefs(data: OrgData): LaneCheckBrief[] {
  return data.lanes.map((lane) => {
    const warned = lane.id === 'aster/instagram';
    return {
      lane: lane.id,
      level: warned ? 'warn' : 'ok',
      safe: true,
      state: lane.state,
      at: iso(-5 * MINUTE),
      failing: [],
      warning: warned ? ['webrtc'] : [],
    };
  });
}

export function laneKeepalive(lane: Lane): LaneKeepalive {
  if (lane.kind !== 'browser') return { available: true, lane: lane.id, enabled: false, opted_out: false, site: null, home: null, status: null, status_reason: null, signed_in: null, session_expires_at: null, blocked: { code: 'no_site', reason: 'This account names no site, so there is no sign-in to keep warm.' }, last_visit: null, visits: [], next_at: null, due_at: null, visiting: false };
  const site = laneSite(lane)?.key ?? null;
  const visit = (ago: number) => ({
    at: iso(-ago),
    finished_at: iso(-ago + 40_000),
    trigger: 'schedule',
    site,
    outcome: 'ok',
    reason: null,
  });
  const visits = site === 'instagram' ? [visit(2 * HOUR), visit(26 * HOUR)] : [];
  return {
    available: true,
    lane: lane.id,
    enabled: true,
    opted_out: false,
    site,
    home: site ? `https://www.${site}.com/` : null,
    status: null,
    status_reason: null,
    signed_in: Boolean(site),
    session_expires_at: site ? iso(9 * DAY) : null,
    blocked: null,
    last_visit: visits[0] ?? null,
    visits,
    next_at: site ? iso(3 * HOUR) : null,
    due_at: site ? iso(3 * HOUR) : null,
    visiting: false,
  };
}

export function laneSites(data: OrgData, lane: Lane): LaneSites {
  const record = data.account.sites[lane.id] ?? { sites: [], strict: false };
  return { lane: lane.id, sites: record.sites, strict: record.strict };
}

/** Owner only, as the node holds it. `remove` names only what is on the list. */
export function setLaneSites(
  data: OrgData,
  lane: Lane,
  body: { op: 'add' | 'remove' | 'set'; sites: string[]; strict?: boolean },
): { lane: string; sites: string[]; strict: boolean; changed: boolean; running: boolean } {
  const current = laneSites(data, lane);
  const next =
    body.op === 'add'
      ? [...current.sites, ...body.sites.filter((site) => !current.sites.includes(site))]
      : body.op === 'remove'
        ? current.sites.filter((site) => !body.sites.includes(site))
        : body.sites;
  const strict = body.strict ?? current.strict;
  data.account.sites[lane.id] = { sites: next, strict };
  return {
    lane: lane.id,
    sites: next,
    strict,
    changed: next.join() !== current.sites.join() || strict !== current.strict,
    running: lane.state === 'warm' || lane.state === 'active',
  };
}

export function viewLink(lane: Lane): ViewLink {
  return { url: `/view/${lane.id}?t=mock-view-link`, expires_at: iso(15 * MINUTE), mode: 'view', owner: false };
}
