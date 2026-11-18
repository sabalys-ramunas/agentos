/**
 * The concept routes of Environments (mock only; the node does not serve these yet, except the replay,
 * which the replay builder's branch serves):
 *
 *   GET  /environments/prepared                       the prepared machines and the warm pool
 *   GET  /environments/{id}/replay                    the replay's timeline (agentos.environments.replay)
 *   GET  /environments/{id}/replay/{frame}.jpg        a frame (drawn here as SVG)
 *   GET  /environments/{id}/checkpoints/{cp}/diff     one checkpoint's diff, ?against=previous|now
 *   POST /environments/saves/{id}/resume              wake a save from the app; the next agent takes it over
 *
 * A resumed save wakes in the time its kind takes (a paused machine in about 6 s, a snapshot in about a
 * minute), its bring-up growing step by step, then waits for an agent with the owner watching.
 */
import { http, HttpResponse } from 'msw';
import type { EnvDetail, EnvMachine, PreparedList } from '@/api/types';
import { checkpointDiff } from './env-diffs';
import { framePicture, replayTimeline } from './env-replay';
import { PLANS, bringupAt } from './env-stories';
import type { MockEnv } from './environments';
import { browserLane, mockEnvironments, summaryOf } from './environments';
import type { Helpers } from './s3';
import type { MockDb, OrgData } from './seed';

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
const MORPH: EnvMachine = { provider: 'morph', server_type: 'morph-4x8', cpu: 4, memory_gb: 8, location: 'us-west' };

export function preparedList(now: number): PreparedList {
  const iso = (ms: number) => new Date(now + ms).toISOString();
  return {
    eur_per_gb_month: 0.0119,
    machines: [
      { repo: 'github.com/harbor-supply/shop', state: 'ready', commit: '3f9a1c0e72b4', ref: null, built_at: iso(-2 * DAY), last_used_at: iso(-14 * MINUTE), uses: 31, gb: 11.4, cost_eur_month: 0.14, key: 'a41c9e2b7d03', age: '2 days', setup_s: 192.4 },
      { repo: 'github.com/harbor-supply/shop', state: 'ready', commit: '91c2e5f0a7b3', ref: 'feat/carrier-rates', built_at: iso(-5 * HOUR), last_used_at: iso(-41 * MINUTE), uses: 2, gb: 11.6, cost_eur_month: 0.14, key: 'c07e3d91fa25', age: '5 h', setup_s: 171.2 },
      { repo: 'github.com/meridian-travel/booking', state: 'building', commit: '2d7e4a1b9c03', ref: null, built_at: null, last_used_at: null, uses: 0, gb: null, cost_eur_month: 0, key: 'e93b0a17c4d8', step: 'running setup: pnpm install (52 s so far)' },
      { repo: 'github.com/meridian-travel/back-office', state: 'failed', commit: '7aa0c3e15b92', ref: null, built_at: null, last_used_at: iso(-6 * DAY), uses: 9, gb: 8.2, cost_eur_month: 0.1, key: '58d1e0b2f7c6',
        error: 'setup failed with exit code 1: pnpm install\n ERR_PNPM_FETCH_404  GET https://registry.npmjs.org/@meridian%2Finvoice-pdf: Not Found - 404\n\nThis error happened while installing a direct dependency of /work/back-office\n\n@meridian/invoice-pdf is not in the npm registry, or you have no permission to fetch it.' },
      { repo: 'github.com/solstice-studio/mailers', state: 'ready', commit: '1f7a9e3c5b2d', ref: null, built_at: iso(-19 * DAY), last_used_at: iso(-17 * DAY), uses: 4, gb: 6.9, cost_eur_month: 0.08, key: '4c2b9e70d1a3', age: '19 days', setup_s: 64.8 },
    ],
    pool: {
      warm: 1, target: 1, minutes: 55,
      machines: [{ vm: 'vm-warm-8e21', state: 'ready', since: iso(-12 * MINUTE), image: 'agentos-prepared-a41c9e2b7d03', server_type: 'cpx22', location: 'nbg1', cost_eur: 0.0024, price_hourly_eur: 0.0121 }],
    },
    month: {
      opens: 64, from_prepared: 41, from_warm: 19, setup_saved_s: 41 * 186, warm_cost_eur: 0.34, recent: recentOpens(now),
    },
  };
}

/** The last 40 opens, newest last: seconds to ready and where each started from. */
function recentOpens(now: number): NonNullable<PreparedList['month']>['recent'] {
  const kinds = ['warm', 'prepared', 'warm', 'cold', 'prepared', 'warm', 'warm', 'prepared', 'cold', 'warm'];
  return Array.from({ length: 40 }, (_, index) => {
    const from = index < 6 ? (index % 2 ? 'cold' : 'prepared') : kinds[index % kinds.length];
    const base = from === 'warm' ? 4 : from === 'prepared' ? 38 : 214;
    const seconds = Math.round((base + ((index * 37) % 17) * (from === 'cold' ? 3.1 : from === 'prepared' ? 0.6 : 0.12)) * 10) / 10;
    return { at: new Date(now - (40 - index) * 17 * HOUR).toISOString(), seconds, from: from as 'warm' | 'prepared' | 'cold' };
  });
}

/** Moves a resumed or starting environment along its plan. True when it handled the environment. */
export function advanceStory(record: OrgData, item: MockEnv, now: number): boolean {
  const env = item.detail;
  if (!item.plan || env.state !== 'starting') return false;
  const elapsed = (now - item.plan.start) / 1000;
  env.bringup = bringupAt(PLANS[item.plan.id], elapsed, env.bringup?.cold_s ?? undefined);
  if (!env.resumed || env.bringup.now) return true;
  env.state = 'ready';
  item.until = undefined;
  env.viewer = { path: `/api/orgs/${record.org.slug}/lanes/${env.lanes.browser}/vnc` };
  env.calls = [{ at: new Date(now).toISOString(), tool: 'environment', summary: `resumed by you from its ${env.resumed.from === 'paused' ? 'paused machine' : 'snapshot'}, waiting for an agent`, ok: true }, ...env.calls];
  const lane = record.lanes.find((entry) => entry.id === env.lanes.browser);
  if (lane) Object.assign(lane, { state: 'warm', viewer: { url: `/api/orgs/${record.org.slug}/lanes/${env.lanes.browser}/vnc` } });
  return true;
}

function resume(record: OrgData, saveId: unknown, now: number): EnvDetail | null {
  const saves = record.environmentSaves ?? [];
  const save = saves.find((entry) => entry.id === saveId);
  if (!save) return null;
  const items = mockEnvironments(record);
  const kind = save.paused ? 'paused' : 'snapshot';
  const eta = Math.ceil(bringupAt(PLANS[kind], Number.POSITIVE_INFINITY).total_s);
  let item = save.env_id ? items.find((entry) => entry.detail.id === save.env_id) : undefined;
  if (!item) {
    const summary = summaryOf(now, {
      id: 'k7pz', title: save.title, state: 'starting', repo: save.repo ?? null, branch: save.branch ?? null, age: 0, lastCall: null, left: 60,
      machine: MORPH, agent: save.agent ?? { name: 'Claude Code', client: null }, keep: 'save',
    });
    item = { jobs: [], detail: { ...summary, ports: [], git: { branch: save.branch ?? null, ahead: 0, dirty: 0, pushed: [] }, calls: [], commands: [], viewer: null, closed_reason: null, unsaved: null, checkpoints: [] } };
    items.unshift(item);
  }
  const env = item.detail;
  Object.assign(env, {
    state: 'starting', closed_at: null, closed_reason: null, expires_at: new Date(now + HOUR).toISOString(),
    // a new session of the same environment: its clock and cost start again
    created_at: new Date(now).toISOString(), cost_eur: 0, last_call_at: null,
    resumed: { by: 'You', at: new Date(now).toISOString(), from: kind, eta_s: eta },
    watching: [{ name: 'You', you: true, since: new Date(now).toISOString() }],
    ports: save.services?.map((service) => ({ port: service.port ?? 0, process: service.cmd, link: null })).filter((port) => port.port) ?? [],
    bringup: bringupAt(PLANS[kind], 0),
    app: undefined,
  } satisfies Partial<EnvDetail>);
  item.plan = { id: kind, start: now };
  item.until = now + eta * 1000;
  record.environmentSaves = saves.filter((entry) => entry !== save);
  const sibling = record.lanes.find((lane) => lane.kind === 'browser');
  if (!record.lanes.some((lane) => lane.id === env.lanes.browser)) record.lanes.push(browserLane(sibling, env, now));
  return env;
}

export function conceptHandlers({ org, failure }: Pick<Helpers, 'org' | 'failure'> & { data: () => MockDb }) {
  const find = (record: OrgData, id: unknown) => mockEnvironments(record).find((item) => item.detail.id === id);
  return [
    http.get('/api/orgs/:org/environments/prepared', org(() => HttpResponse.json(preparedList(Date.now())))),
    http.get(
      '/api/orgs/:org/environments/:id/replay',
      org((record, { params }) => {
        if (record.role !== 'owner') return failure(403, 'denied', 'Only the owner can watch a replay.');
        const item = find(record, params.id);
        const timeline = item ? replayTimeline(record.org.slug, item.detail) : null;
        if (!timeline) return failure(404, 'not_found', 'This environment has no replay.');
        return HttpResponse.json(timeline);
      }),
    ),
    http.get(
      '/api/orgs/:org/environments/:id/replay/:frame',
      org((_record, { params }) => {
        const picture = framePicture(String(params.frame).replace(/\.jpg$/, ''));
        if (!picture) return failure(404, 'not_found', 'No such frame.');
        return new HttpResponse(picture, { headers: { 'Content-Type': 'image/svg+xml', 'Cache-Control': 'private, max-age=3600' } });
      }),
    ),
    http.get(
      '/api/orgs/:org/environments/:id/checkpoints/:cp/diff',
      org((_record, { params, request }) => {
        const against = new URL(request.url).searchParams.get('against') === 'now' ? 'now' : 'previous';
        const diff = checkpointDiff(String(params.id), String(params.cp), against);
        if (!diff) return failure(404, 'not_found', 'No such checkpoint.');
        return HttpResponse.json(diff);
      }),
    ),
    http.post(
      '/api/orgs/:org/environments/saves/:id/resume',
      org((record, { params }) => {
        if (record.role !== 'owner') return failure(403, 'denied', 'Only the owner can resume a saved environment from the app.');
        const env = resume(record, params.id, Date.now());
        if (!env) return failure(404, 'not_found', 'No save with that id on this computer.');
        return HttpResponse.json(env);
      }),
    ),
  ];
}
