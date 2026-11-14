/**
 * The API's additions for the design rebuild, as the mock answers them: lane health, an approval's place and account
 * state, the outcome of a decision, and the digest's time zone. Derived on the way out, so the
 * seed stays as it is and every lane change is reflected at once.
 */
import type { Approval, ApprovalLaneState, HomeFace, Lane, LaneHealth, NotificationSettings, Outcome, Site, Workspace } from '@/api/types';
import { siteOf } from '@/lib/sites';
import { withAsk } from './ask-first';
import { methodsFor, requiresOf } from './confirmations';
import type { OrgData } from './seed';

const REASONS: Partial<Record<LaneHealth['state'], string>> = {
  quarantined: 'The account is switched off for safety.',
  wrong_account: 'The account is signed in as someone else.',
  logged_out: 'The account is signed out.',
  captcha: 'The site wants proof that a real person is there.',
  checkpoint: 'The site wants to check that it is really you.',
  blocked: 'The site is refusing the account for now.',
  rate_limited: 'The site asked the account to slow down.',
  offline: 'The computer it runs on is not connected.',
  missing: 'The computer stopped reporting this account.',
};

const BLOCKS = new Set<LaneHealth['state']>(['quarantined', 'wrong_account', 'logged_out', 'captcha', 'checkpoint', 'blocked', 'offline', 'missing']);

export function laneHealth(lane: Lane): LaneHealth {
  const state: LaneHealth['state'] = lane.missing_since
    ? 'missing'
    : lane.state === 'quarantined'
      ? 'quarantined'
      : lane.account?.status === 'wrong_account'
        ? 'wrong_account'
        : lane.signal === 'logged_out'
          ? 'logged_out'
          : lane.signal === 'captcha' || lane.signal === 'checkpoint' || lane.signal === 'blocked' || lane.signal === 'rate_limited'
            ? lane.signal
            : lane.state === 'active'
              ? 'working'
              : lane.state === 'cold'
                ? 'asleep'
                : 'ok';
  return { state, blocks: BLOCKS.has(state), reason: REASONS[state] ?? null };
}

export function laneSite(lane: Lane): Site | null {
  const known = siteOf(lane.account?.site ?? lane.name);
  if (!known || known.tool) return null;
  return { key: known.key, label: known.name, domain: known.domains[0] ?? null, source: lane.account?.site ? 'account' : 'name' };
}

export function withHealth(lane: Lane): Lane {
  return { ...lane, health: laneHealth(lane), site: laneSite(lane) };
}

/** Where an approval lands, and how its account is right now. */
export function withPlace(data: OrgData, approval: Approval): Approval {
  const [ws = '', name = ''] = approval.lane.split('/');
  const lane = data.lanes.find((item) => item.id === approval.lane);
  const workspace = data.workspaces.find((item) => item.id === ws);
  // An account the demo has no lane for (Gmail, Shopify) still names its site.
  const known = !lane && name ? siteOf(name) : null;
  const site = lane ? laneSite(lane) : known && !known.tool ? { key: known.key, label: known.name, domain: known.domains[0] ?? null, source: 'name' } : null;
  const health = lane ? laneHealth(lane) : null;
  const laneState: ApprovalLaneState | null = health
    ? { ...health, warning: health.blocks && health.reason ? `${health.reason} If you say yes now, nothing happens until it is fixed.` : null }
    : null;
  const failed = approval.outcome && (approval.outcome.state === 'failed' || approval.outcome.state === 'partial') && !approval.outcome.cleared_at;
  const requires = requiresOf(data, approval);
  return {
    ...approval,
    requires,
    methods: methodsFor(data, requires),
    workspace: ws || null,
    workspace_name: workspace?.name ?? null,
    place: workspace && name ? `${workspace.name}'s ${site?.label ?? name}` : null,
    site,
    lane_state: laneState,
    needs_you: approval.state === 'pending' || Boolean(failed),
  };
}

const VERB: Partial<Record<Approval['effect'], [string, string, string]>> = {
  // [doing, done, could not]
  commit: ['Posting to', 'Posted to', "Couldn't post to"],
  pay: ['Paying from', 'Paid from', "Couldn't pay from"],
};

/** The same verbs for the kinds beyond posts, as the cloud words them. */
const KIND_VERB: Partial<Record<NonNullable<Approval['preview']['kind']>, [string, string, string]>> = {
  email: ['Sending from', 'Sent from', "Couldn't send from"],
  message: ['Replying on', 'Replied on', "Couldn't reply on"],
  form: ['Submitting on', 'Submitted on', "Couldn't submit on"],
  order: ['Refunding on', 'Refunded on', "Couldn't refund on"],
  payment: ['Paying from', 'Paid from', "Couldn't pay from"],
  delete: ['Deleting on', 'Deleted on', "Couldn't delete on"],
  command: ['Running on', 'Ran on', "Couldn't run on"],
  files: ['Sending to', 'Sent to', "Couldn't send to"],
};

const verbsFor = (approval: Approval) => (approval.preview.kind ? KIND_VERB[approval.preview.kind] : undefined) ?? VERB[approval.effect];

/** The outcome right after a decision: a yes on a blocked account fails, as on the real stack. */
export function outcomeOf(approval: Approval, decision: 'approve' | 'deny', who: string): Outcome {
  const placed = approval.place ?? approval.lane;
  const [doing, , failed] = verbsFor(approval) ?? ['Doing it on', 'Done on', "Couldn't do it on"];
  const base = { detail: null, at: new Date().toISOString(), run_id: null, proof: null, cleared_at: null, cleared_by: null };
  if (decision === 'deny') return { ...base, state: 'not_run', title: `Nothing was done on ${placed}`, reason: `${who} said no, so nothing was done.`, source: 'decision' };
  if (approval.lane_state?.blocks) {
    return { ...base, state: 'failed', title: `${failed} ${placed}`, reason: approval.lane_state.reason, detail: `lane ${approval.lane} cannot act: ${approval.lane_state.state}`, source: 'run' };
  }
  return { ...base, state: 'pending', title: `${doing} ${placed}`, reason: null, source: 'decision' };
}

/** The handle each demo business posts as (api-for-rebuild.md, section 5). */
export const HANDLES: Record<string, string> = {
  aster: 'astercoffee',
  harbor: 'harborsupply',
  meridian: 'meridiantravel',
  solstice: 'solsticestudio',
  personal: 'personal',
};

/** It happened, and the site shows it: what the node reports a moment after a yes is sent. */
export function doneOutcome(approval: Approval): Outcome {
  const placed = approval.place ?? approval.lane;
  const [, done] = verbsFor(approval) ?? ['', 'Done on'];
  const [ws = '', name = ''] = approval.lane.split('/');
  const domain = approval.site?.domain ?? `${name}.com`;
  const url = approval.effect === 'commit' && (!approval.preview.kind || approval.preview.kind === 'post') ? `https://${domain}/${HANDLES[ws] ?? ws}/p/${approval.id.slice(-8).toLowerCase()}` : null;
  return {
    state: 'done',
    title: `${done} ${placed}`,
    reason: 'Checked on the site: it is live.',
    detail: null,
    source: 'run',
    at: new Date().toISOString(),
    run_id: null,
    proof: url ? { url, screenshot: null } : null,
    cleared_at: null,
    cleared_by: null,
  };
}

/** The digest's zone, as the cloud now sends it. The mock org lives in Vilnius. */
export function withZone(settings: NotificationSettings): NotificationSettings {
  const zone = settings.quiet_hours.timezone && settings.quiet_hours.timezone !== 'UTC' ? settings.quiet_hours.timezone : 'Europe/Vilnius';
  const city = zone.split('/').pop()?.replaceAll('_', ' ') ?? zone;
  return {
    ...settings,
    timezone: zone,
    timezone_source: settings.quiet_hours.timezone && settings.quiet_hours.timezone !== 'UTC' ? 'person' : 'org',
    digest: { time: '08:00', timezone: zone, label: `08:00, ${city} time` },
  };
}

/** Which of Home's states an account counts as (api-for-rebuild.md section 7). */
export function homeFace(lane: Lane, health: LaneHealth = laneHealth(lane), running = false): HomeFace {
  if (health.state === 'quarantined' || health.state === 'blocked' || health.state === 'rate_limited' || health.state === 'offline' || health.state === 'missing') return 'stuck';
  if (health.state === 'logged_out' || health.state === 'captcha' || health.state === 'checkpoint' || health.state === 'wrong_account') return 'waiting';
  if (running || lane.state === 'active') return 'working';
  if (health.state === 'asleep') return 'sleeping';
  return 'all_good';
}

const WEEK_MS = 7 * 24 * 3600_000;

/** A lane as `GET /workspaces` sends it: its health with Home's face, its last picture and its screen now. */
export function withPictures(data: OrgData, lane: Lane): Lane {
  const running = data.runs.some((run) => run.lane === lane.id && run.status === 'running');
  const health = laneHealth(lane);
  const proof = data.runs.find(
    (run) => run.lane === lane.id && run.proof?.screenshot && Date.now() - Date.parse(run.finished_at ?? run.started_at) < WEEK_MS,
  );
  const awake = lane.kind === 'browser' && (lane.state === 'warm' || lane.state === 'active');
  return {
    ...lane,
    health: { ...health, face: homeFace(lane, health, running) },
    site: laneSite(lane),
    picture: proof
      ? { kind: 'proof', url: proof.proof!.screenshot!, at: proof.finished_at ?? proof.started_at, run_id: proof.id, post_url: proof.proof?.url ?? null }
      : null,
    live_picture: awake ? `/api/orgs/${data.org.slug}/lanes/${lane.workspace}/${lane.name}/screenshot` : null,
  };
}

const ORDER: HomeFace[] = ['stuck', 'waiting', 'working', 'all_good', 'sleeping'];

/** A business's one word, its counts and what needs a person there. */
export function homeFields(data: OrgData, id: string, lanes: Lane[]): Pick<Workspace, 'state' | 'needs_you' | 'counts'> {
  const counts: Record<HomeFace, number> = { all_good: 0, waiting: 0, stuck: 0, sleeping: 0, working: 0 };
  for (const lane of lanes) counts[lane.health?.face ?? homeFace(lane)] += 1;
  const asks =
    data.approvals.filter((item) => item.lane.startsWith(`${id}/`)).length +
    data.questions.filter((item) => item.lane?.startsWith(`${id}/`)).length +
    data.history.filter((item) => item.lane.startsWith(`${id}/`) && item.outcome && (item.outcome.state === 'failed' || item.outcome.state === 'partial') && !item.outcome.cleared_at).length;
  const state =
    counts.stuck > 0
      ? 'stuck'
      : counts.waiting > 0 || asks > 0
        ? 'waiting'
        : (ORDER.slice(2).find((face) => (face === 'sleeping' ? lanes.length > 0 && counts.sleeping === lanes.length : counts[face] > 0)) ?? 'sleeping');
  return { state, needs_you: asks + counts.waiting, counts };
}

/** "One", "Two", "31": as the cloud says small counts at the start of a sentence. */
const WORDS = ['No', 'One', 'Two', 'Three'];

/**
 * The cloud's rule (home_router.hero_sentence): when nothing urgent waits, lead with what got done
 * on its own, and say the questions after it.
 */
export function heroOf(needsYou: number, decisions: number, offline: string | null, alone: number) {
  const urgent = Boolean(offline) || needsYou > decisions;
  if (alone && !urgent) {
    const asks = needsYou ? `${WORDS[needsYou] ?? needsYou} ${needsYou === 1 ? 'needs' : 'need'} you.` : 'Nothing needs you.';
    return { lead: `Your agents did ${alone} ${alone === 1 ? 'thing' : 'things'} on their own today.`, rest: asks };
  }
  if (needsYou) {
    return { lead: needsYou === 1 ? 'One thing needs you.' : `${needsYou} things need you.`, rest: offline ? `${offline} is offline.` : 'Everything else is fine.' };
  }
  if (offline) return { lead: `${offline} is offline.`, rest: 'Its accounts are paused until it is back.' };
  return { lead: 'All good.', rest: 'Nothing needs you.' };
}

/** `GET /home` as the cloud answers it (api-for-rebuild.md section 10), from the mock's own day. */
export function homeSummary(data: OrgData, needsYou: number, decisions = needsYou) {
  const midnight = new Date();
  midnight.setHours(0, 0, 0, 0);
  const since = midnight.getTime();
  const runs = data.activity.filter((item) => item.run_id && Date.parse(item.at) >= since && item.status !== 'running');
  const reads = data.activity.filter((item) => !item.run_id && Date.parse(item.at) >= since);
  let done = runs.length + reads.length;
  let checked = runs.filter((item) => item.status === 'succeeded_verified').length + reads.length;
  const posts = runs.filter((item) => item.effect === 'commit').length;
  const drafts = runs.filter((item) => item.effect === 'draft').length;
  const down = data.nodes.filter((node) => node.state === 'down');
  const names = new Map(data.workspaces.map((item) => [item.id, item.name]));
  const coming = data.schedules
    .filter((item) => item.next_run)
    .sort((a, b) => (a.next_run ?? '').localeCompare(b.next_run ?? ''))
    .slice(0, 3)
    .map((item) => {
      const [ws = '', site = '', verb = ''] = item.flow.split('.');
      const title = `${siteOf(site)?.name ?? site} ${verb}`;
      const at = new Date(item.next_run!);
      const today = at.toDateString() === new Date().toDateString();
      const when = `${today ? 'Today' : at.toLocaleDateString('en', { weekday: 'long' })} at ${at.toLocaleTimeString('en', { hour: '2-digit', minute: '2-digit', hour12: false })}`;
      return { schedule_id: item.id, flow: item.flow, lane: item.lane ?? null, workspace_name: names.get(ws) ?? null, place: null, title, at: item.next_run!, when, sentence: `${names.get(ws) ?? ws}: ${title} · ${when}` };
    });
  // The demo org's other work today (History's examples): not only posts.
  const other = data.org.slug === 'northwind-group' ? { emails: 4, invoices: 6, forms: 2, prices: 3, orders: 1 } : { emails: 0, invoices: 0, forms: 0, prices: 0, orders: 0 };
  const extra = other.emails + other.invoices + other.forms + other.prices + other.orders;
  const chips = [
    other.emails ? `${other.emails} emails sent` : '',
    posts ? `${posts} posts published` : '',
    other.invoices ? `${other.invoices} invoices collected` : '',
    other.forms ? `${other.forms} forms filled` : '',
    other.prices ? `${other.prices} prices checked` : '',
    other.orders ? `${other.orders} order handled` : '',
    drafts ? `${drafts} drafts saved` : '',
    reads.length ? `${reads.length} checks` : '',
  ].filter(Boolean);
  done += extra;
  checked += Math.max(0, extra - 2);
  // What asked a person first today: payments, and the routines whose Ask me first is on.
  const asking = data.flows.map((flow) => withAsk(data, flow)).filter((flow) => flow.asks_first);
  const asked = runs.filter((item) => item.effect === 'pay' || asking.some((flow) => flow.lane === item.lane)).length;
  const alone = Math.max(0, done - asked);
  const sentence = heroOf(needsYou, decisions, down[0]?.id ?? null, alone);
  const leadsWithDone = sentence.lead.startsWith('Your agents did');
  return {
    timezone: 'Europe/Vilnius',
    date: midnight.toISOString().slice(0, 10),
    needs_you: needsYou,
    all_fine: down.length === 0,
    sentence,
    problems: down.map((node) => ({ kind: 'computer_offline', node: node.id, lead: `${node.id} is offline.`, rest: 'Its accounts are paused until it is back.' })),
    today: {
      done,
      on_its_own: alone,
      went_out_on_its_own: Math.max(0, posts + other.emails + other.forms - asked),
      checked,
      not_checked: done - checked,
      by_kind: { posts, replies: 0, drafts, payments: 0, changes: 0, tasks: 0, checks: reads.length },
      chips,
      working_now: data.lanes.filter((lane) => withPictures(data, lane).health?.face === 'working').length,
      line: leadsWithDone
        ? `${checked} of them were checked on the real site afterwards.`
        : done
          ? `Your agents did ${done} things across ${data.workspaces.length} businesses today, and ${checked} were checked on the real site afterwards.`
          : 'Your agents have not done anything yet today.',
    },
    coming_up: coming,
    ai_usage: data.role === 'owner' || data.role === 'admin' ? { today_usd: data.costToday.usd, usual_usd: 1.92, ratio: 0.97, label: `AI usage today: $${data.costToday.usd.toFixed(2)}, about the same as a usual day.`, unusual: false } : null,
  };
}
