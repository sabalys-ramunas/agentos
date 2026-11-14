/**
 * How a person says yes, as a node would answer it: the setting, a real-looking day, the methods,
 * and the fresh proof a heavy yes needs. Concept stage: the mock only, with no cloud route behind it.
 * `?mock=no-mac` is a person with no Mac, and
 * `?mock=proof-fails` is a check that doesn't go through.
 */
import { delay, http, HttpResponse } from 'msw';
import type { Approval, ConfirmationOverlay, ConfirmationSetting, ConfirmationView, ConfirmDay, ConfirmDayStop, ConfirmDayTick, ConfirmLevel, ConfirmMethodId, ConfirmMethodView, Effect } from '@/api/types';
import { businessRungs, FLOORS, needsProof, reaches, rungIndex, rungsOf } from '@/lib/confirm';
import { reviewPath } from './s3';
import type { Helpers } from './s3';
import { scenario } from './scenario';
import { rng } from './seed';
import type { OrgData } from './seed';

export const SYSTEM_SETTING: ConfirmationSetting = {
  commit: { when: 'auto', level: 'chat' },
  pay: { when: 'ask', level: 'present' },
  irreversible: { when: 'ask', level: 'present' },
  after_untrusted: { level: 'signed_in' },
  chat: true,
};

const MINUTE = 60_000;
const at = (hour: number, minute: number) => hour * 60 + minute;

/** Yesterday in the demo org: a day of real work, and the eleven things that stopped at a person. */
const STOPS: ConfirmDayStop[] = [
  { id: 'stop_1', minute: at(7, 42), row: 'marked', business: 'aster', site: 'instagram', title: "Post the morning photo on Aster Coffee's Instagram", who: 'Tomas Reyes', said: 'yes', method: 'chat', where: 'Claude Code' },
  { id: 'stop_2', minute: at(8, 50), row: 'after_untrusted', business: 'meridian', site: 'tripadvisor', title: "Reply to Jonas Petraitis's review on Tripadvisor", who: 'Ines Okafor', said: 'yes', method: 'panel', where: null },
  { id: 'stop_3', minute: at(9, 35), row: 'pay', business: 'aster', site: 'quickbooks', title: 'Pay €1,280 to Bean & Co Roasters', who: 'Demo Owner', said: 'yes', method: 'mac', where: "Demo's MacBook Pro" },
  { id: 'stop_4', minute: at(10, 30), row: 'marked', business: 'solstice', site: 'gmail', title: 'Send the follow-up to 83 galleries', who: 'Jun Park', said: 'yes', method: 'panel', where: null },
  { id: 'stop_5', minute: at(11, 40), row: 'marked', business: 'meridian', site: 'booking', title: 'Raise the Lisbon apartment to €142 a night', who: 'Ines Okafor', said: 'yes', method: 'chat', where: 'ChatGPT' },
  { id: 'stop_6', minute: at(12, 25), row: 'irreversible', business: 'harbor', site: 'blog', title: "Delete 3 old drafts from Harbor Supply's blog", who: 'Mara Lindqvist', said: 'yes', method: 'passkey', where: "Mara's iPhone" },
  { id: 'stop_7', minute: at(13, 45), row: 'after_untrusted', business: 'harbor', site: 'gmail', title: 'Answer a new customer who wrote to Harbor Supply', who: 'Tomas Reyes', said: 'no', method: 'panel', where: null },
  { id: 'stop_8', minute: at(14, 40), row: 'pay', business: 'harbor', site: 'shopify', title: 'Refund €24 to Lina Berg for order #1432', who: 'Mara Lindqvist', said: 'yes', method: 'passkey', where: "Mara's iPhone" },
  { id: 'stop_9', minute: at(16, 5), row: 'marked', business: 'harbor', site: 'linkedin', title: 'Post the hiring update on LinkedIn', who: 'Mara Lindqvist', said: 'yes', method: 'chat', where: 'Claude Code' },
  { id: 'stop_10', minute: at(17, 40), row: 'marked', business: 'aster', site: 'reddit', title: 'Reply in the weekly thread on Reddit', who: 'Tomas Reyes', said: 'yes', method: 'mac', where: "Tomas's Mac mini" },
  { id: 'stop_11', minute: at(19, 15), row: 'irreversible', business: 'harbor', site: 'gov', title: 'File the October VAT return', who: 'Demo Owner', said: 'yes', method: 'mac', where: "Demo's MacBook Pro" },
];

const BUSINESSES = ['aster', 'harbor', 'meridian', 'solstice'];

/** Busy mornings and afternoons, a lunch, and a little overnight: what routines really do. */
const BUSY = [0.4, 0.3, 0.2, 0.2, 0.3, 0.5, 1.4, 3, 6, 9, 8, 6, 3.5, 5, 8, 9, 7, 5, 3.5, 2.5, 2, 1.4, 1, 0.6];

function ticks(): ConfirmDayTick[] {
  const random = rng(4471);
  const out: ConfirmDayTick[] = [];
  BUSY.forEach((weight, hour) => {
    const count = Math.round(weight * 2.5);
    for (let index = 0; index < count; index += 1) {
      out.push({
        minute: hour * 60 + Math.floor(random() * 60),
        business: BUSINESSES[Math.floor(random() * BUSINESSES.length)] ?? 'aster',
        kind: random() < 0.42 ? 'commit' : 'look',
      });
    }
  });
  return out.sort((a, b) => a.minute - b.minute);
}

function dayOf(data: OrgData): ConfirmDay {
  const yesterday = new Date(Date.now() - 24 * 60 * MINUTE);
  const date = yesterday.toISOString().slice(0, 10);
  if (data.org.slug !== 'northwind-group') return { label: 'Yesterday', date, ticks: [], stops: [] };
  return { label: 'Yesterday', date, ticks: ticks(), stops: STOPS };
}

interface State {
  setting: ConfirmationSetting;
  overlays: Record<string, ConfirmationOverlay>;
}

const states = new WeakMap<OrgData, State>();

function stateOf(data: OrgData): State {
  let state = states.get(data);
  if (!state) {
    const demo = data.org.slug === 'northwind-group';
    state = { setting: structuredClone(SYSTEM_SETTING), overlays: demo ? { harbor: { commit: { when: 'auto', level: 'signed_in' } } } : {} };
    states.set(data, state);
  }
  return state;
}

function view(data: OrgData): ConfirmationView {
  const state = stateOf(data);
  const demo = data.org.slug === 'northwind-group';
  const ago = (minutes: number) => new Date(Date.now() - minutes * MINUTE).toISOString();
  const review = data.approvals.find((item) => item.effect === 'pay' && /Nordic Ropeworks/.test(item.summary));
  return {
    setting: state.setting,
    system: SYSTEM_SETTING,
    overlays: state.overlays,
    day: dayOf(data),
    clients: demo
      ? [
          { name: 'Claude Code', agent: 'claude-code', answers: 41, last_at: ago(23 * 60) },
          { name: 'ChatGPT', agent: 'chatgpt', answers: 12, last_at: ago(27 * 60) },
        ]
      : [],
    sample_review: review ? `${reviewPath(data.org.slug, review.id)}&from=chat` : null,
  };
}

const MY_METHODS: Partial<Record<ConfirmMethodId, { detail: string | null; ago: number }>> = {
  chat: { detail: 'Claude Code, ChatGPT', ago: 23 * 60 },
  panel: { detail: null, ago: 3 },
  passkey: { detail: "Demo's MacBook Pro, Chrome", ago: 5 * 24 * 60 },
  mac: { detail: "Demo's MacBook Pro, with Touch ID", ago: 19 * 60 },
  cli: { detail: 'home-mini', ago: 9 * 24 * 60 },
};

const REGISTRY: [ConfirmMethodId, ConfirmLevel, 'ready' | 'planned'][] = [
  ['chat', 'chat', 'ready'],
  ['panel', 'signed_in', 'ready'],
  ['cli', 'signed_in', 'ready'],
  ['passkey', 'present', 'ready'],
  ['mac', 'device_key', 'ready'],
  ['ios', 'device_key', 'planned'],
  ['android', 'device_key', 'planned'],
  ['watch', 'device_key', 'planned'],
  ['totp', 'present', 'planned'],
];

function methods(data: OrgData): ConfirmMethodView[] {
  const chatOn = stateOf(data).setting.chat;
  return REGISTRY.map(([id, level, status]) => {
    const mine = MY_METHODS[id];
    const gone = (id === 'mac' && scenario.has('no-mac')) || (id === 'chat' && !chatOn);
    return {
      id,
      level,
      status,
      yours: status === 'ready' && !gone,
      detail: gone ? null : (mine?.detail ?? null),
      last_used_at: gone || !mine ? null : new Date(Date.now() - mine.ago * MINUTE).toISOString(),
    };
  });
}

const UNTRUSTED = /untrusted|private data/i;
const ROW_OF: Partial<Record<Effect, 'pay' | 'irreversible'>> = { pay: 'pay', irreversible: 'irreversible' };

/** The strictest level that applies: the org, raised by the business, raised by a safety check. */
export function requiresOf(data: OrgData, approval: Pick<Approval, 'effect' | 'lane' | 'reason'>): ConfirmLevel | null {
  const { setting, overlays } = stateOf(data);
  const rungs = businessRungs(setting, overlays[approval.lane.split('/')[0] ?? '']);
  const row = ROW_OF[approval.effect];
  if (row) return rungs[row] === 'auto' ? 'present' : rungs[row];
  if (approval.effect !== 'commit') return null;
  const asked = UNTRUSTED.test(approval.reason ?? '') ? rungs.after_untrusted : rungs.marked;
  return asked === 'auto' ? 'chat' : asked;
}

/** The ready methods that could reach this for the person looking. */
export function methodsFor(data: OrgData, requires: ConfirmLevel | null): ConfirmMethodId[] | null {
  if (!requires) return null;
  return methods(data)
    .filter((method) => method.yours && method.id !== 'cli' && reaches(method.level, requires))
    .map((method) => method.id);
}

/** Fresh proofs, used once and gone in two minutes, as the contract's `present` window. */
const proven = new Map<string, number>();

/** A heavy yes spends its proof. Null when it may go ahead; the refusal otherwise. */
export function spendProof(data: OrgData, approval: Approval): ConfirmLevel | null {
  const requires = requiresOf(data, approval);
  if (!needsProof({ requires })) return null;
  const until = proven.get(approval.id) ?? 0;
  proven.delete(approval.id);
  return until > Date.now() ? null : requires;
}

/** A row may never go under its floor, and lowering what money needs is a step-up. */
function refusal(helpers: Pick<Helpers, 'failure' | 'stepUpNeeded' | 'stepUpValid'>, current: ConfirmationSetting, next: ConfirmationSetting): Response | null {
  const rungs = rungsOf(next);
  for (const [row, floor] of Object.entries(FLOORS) as [keyof typeof FLOORS, (typeof FLOORS)[keyof typeof FLOORS]][]) {
    if (rungIndex(rungs[row]) < rungIndex(floor)) return helpers.failure(409, 'below_floor', 'That is lower than this can ever go, so nothing changed.');
  }
  const before = rungsOf(current);
  const lowered = (['pay', 'irreversible', 'after_untrusted'] as const).some((row) => rungIndex(rungs[row]) < rungIndex(before[row]));
  const widened = next.chat && !current.chat;
  return (lowered || widened) && !helpers.stepUpValid() ? helpers.stepUpNeeded() : null;
}

export function confirmationHandlers(helpers: Pick<Helpers, 'org' | 'failure' | 'stepUpNeeded' | 'stepUpValid'>) {
  const { org } = helpers;
  return [
    http.get('/api/orgs/:org/confirmation', org((data) => HttpResponse.json(view(data)))),
    http.get('/api/orgs/:org/confirmation/methods', org((data) => HttpResponse.json(methods(data)))),
    http.put(
      '/api/orgs/:org/confirmation',
      org(async (data, { request }) => {
        const body = (await request.json()) as { setting?: ConfirmationSetting; business?: string; overlay?: ConfirmationOverlay };
        const state = stateOf(data);
        if (body.business) {
          state.overlays = { ...state.overlays, [body.business]: body.overlay ?? {} };
          return HttpResponse.json(view(data));
        }
        if (body.setting) {
          const refused = refusal(helpers, state.setting, body.setting);
          if (refused) return refused;
          state.setting = body.setting;
        }
        return HttpResponse.json(view(data));
      }),
    ),
    // Stands in for the passkey check: a real one signs a challenge bound to this approval.
    http.post(
      '/api/orgs/:org/approvals/:id/proof/options',
      org(() => HttpResponse.json({ challenge: 'mock-challenge', rpId: 'localhost', allowCredentials: [], userVerification: 'required', timeout: 60000 })),
    ),
    http.post(
      '/api/orgs/:org/approvals/:id/proof',
      org(async (_data, { params }) => {
        await delay(scenario.has('still') ? 0 : 520);
        if (scenario.has('proof-fails')) return helpers.failure(403, 'proof_refused', 'That did not work.');
        proven.set(String(params.id), Date.now() + 2 * MINUTE);
        return HttpResponse.json({ ok: true, level: 'present' });
      }),
    ),
  ];
}
