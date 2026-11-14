/**
 * What the Workers concept shows: the browser worker on the Mac mini, part way through posting Aster
 * Coffee's weekly photo, with a day of steps behind it, and the Android and desktop workers said
 * calmly. Sites and people come from the demo. Built once per org on first use, so a switch sticks
 * until the page is reloaded. No handler here reads a key, because the hosted panel never holds one.
 *
 * Scenarios, set with `?mock=`:
 *   workers-new      nothing that can decide a step yet, so the way in is adding an AI service
 *   workers-ready    an AI service is there, and nobody has turned the worker on
 *   workers-asked    it stopped and asked a person
 *   workers-robot    a robot check stopped it, which always goes to a person
 *   workers-offline  its computer is offline
 *   workers-limit    it used up today's steps
 *   key-refused      the AI service does not accept the key
 *   worker-alone     a computer that lets a post go after a page, so "Runs on its own" means it
 */
import { delay, http, HttpResponse } from 'msw';
import type { Brain, BrainHandover, HistoryStory, Question, WorkerAccount, WorkerJob, WorkerLimits, WorkerOnComputer, WorkerOverview, WorkerPlace, WorkerStep, WorkerStepKind, WorkerThing } from '@/api/types';
import { mayTurn } from './ask-first';
import { scenario } from './scenario';
import type { OrgData } from './seed';
import type { Helpers } from './s3';

const MINUTE = 60_000;
const iso = (at: number) => new Date(at).toISOString();
const LIMIT = 60;

const MODEL: Record<WorkerStepKind, string | null> = { decide: 'jev', write: 'mistral-small', rescue: 'claude-sonnet-5-5', you: null, yes: null };
const USD: Record<WorkerStepKind, number> = { decide: 0.0002, write: 0.0011, rescue: 0.021, you: 0, yes: 0 };

const step = (kind: WorkerStepKind, what: string, index = 0): WorkerStep => ({
  kind,
  what,
  model: MODEL[kind],
  ms: kind === 'rescue' ? 2_400 : kind === 'write' ? 880 : 90 + ((index * 37) % 140),
  usd: USD[kind],
});

const steps = (items: [WorkerStepKind, string][]) => items.map(([kind, what], index) => step(kind, what, index));

/** Aster Coffee's weekly photo: what the worker does, one step at a time. */
const WEEKLY_POST: [WorkerStepKind, string][] = [
  ['decide', 'Opened Instagram'],
  ['decide', 'Checked it is signed in as Aster Coffee'],
  ['decide', 'Pressed Create'],
  ['decide', 'Chose Post'],
  ['decide', 'Waited for the upload window'],
  ['decide', 'Opened the photo folder'],
  ['decide', 'Found this week\'s photo'],
  ['decide', 'Chose the photo'],
  ['decide', 'Waited for the preview'],
  ['rescue', 'A new pop-up covered the page. A stronger AI model said to close it'],
  ['decide', 'Closed the pop-up'],
  ['decide', 'Checked the photo is still there'],
  ['decide', 'Set the crop to square'],
  ['decide', 'Pressed Next'],
  ['decide', 'Skipped the filters'],
  ['decide', 'Pressed Next'],
  ['decide', 'Pressed into the caption box'],
  ['write', 'Pasted the caption Claude Code wrote, and fitted it to the limit'],
  ['decide', 'Checked the caption reads the same'],
  ['decide', 'Opened Add location'],
  ['decide', 'Typed the café\'s name'],
  ['decide', 'Chose Aster Coffee, Vilnius'],
  ['write', 'Wrote the description for people who cannot see the photo'],
  ['decide', 'Opened Accessibility'],
  ['decide', 'Pasted the description'],
  ['decide', 'Went back to the post'],
  ['decide', 'Checked the post against what it was told'],
  ['yes', 'Stopped at Share. This account asks you first'],
];

/** How far the live thing has got. Held still for a picture, and moving otherwise. */
const STILL_AT = 25;
const started = Date.now();
const liveCount = () => (scenario.has('still') ? STILL_AT : Math.min(WEEKLY_POST.length, STILL_AT + Math.floor((Date.now() - started) / 2_600)));

const claudeCode = { kind: 'agent', id: 'claude-code', thread: 'T-91' } as const;

/** A real computer's default: a post that follows a page asks, and the worker always reads one first. */
const afterAPageAsks = () => !scenario.has('worker-alone');

/** The last step is the account's Ask me first and the computer's own rule: it stops at Share, or presses it. */
function lastStep(asks: boolean): [WorkerStepKind, string] {
  if (asks) return ['yes', 'Stopped at Share. This account asks you first'];
  return afterAPageAsks() ? ['yes', 'Stopped at Share. It read a page first, so the post asks you'] : ['decide', 'Pressed Share. This account runs on its own'];
}

function liveThing(now: number, asks = true): WorkerThing {
  const count = liveCount();
  const post: [WorkerStepKind, string][] = [...WEEKLY_POST.slice(0, -1), lastStep(asks)];
  return {
    id: 'wt_LIVE',
    worker: 'browser',
    lane: 'aster/instagram',
    node: 'home-mini',
    said_by: claudeCode,
    told: 'Post this week\'s photo on Aster Coffee\'s Instagram, with the caption I wrote',
    state: count < post.length ? 'working' : asks || afterAPageAsks() ? 'asked' : 'done',
    steps: steps(post.slice(0, count)),
    stop: null,
    limit: LIMIT,
    started_at: iso(now - 4 * MINUTE),
    ended_at: null,
    run_id: null,
  };
}

const INVOICES: [WorkerStepKind, string][] = [
  ['decide', 'Opened the supplier portal'],
  ['decide', 'Checked it is signed in as Aster Coffee'],
  ['decide', 'Opened Orders'],
  ['decide', 'Filtered to this month'],
  ['decide', 'Opened the list of open orders'],
  ['decide', 'Looked for the Guji natural order'],
  ['decide', 'Found two orders with the same name'],
  ['rescue', 'A stronger AI model read both, and could not tell which one was meant'],
  ['you', 'Asked you which order is meant'],
];

/** It stopped at a person. The question sits in Needs you, where a person already looks. */
export function askingThing(now: number): WorkerThing {
  return {
    id: 'wt_ASKING',
    worker: 'browser',
    lane: 'aster/portal',
    node: 'home-mini',
    said_by: claudeCode,
    told: 'Add two more bags to the Guji natural order',
    state: 'asked',
    steps: steps(INVOICES),
    stop: 'unsure',
    limit: LIMIT,
    started_at: iso(now - 42 * MINUTE),
    ended_at: iso(now - 40 * MINUTE),
    run_id: null,
  };
}

const ROBOT: [WorkerStepKind, string][] = [
  ['decide', 'Opened Google Sheets'],
  ['decide', 'Opened the stock sheet'],
  ['decide', 'Waited for the sheet to load'],
  ['decide', 'Found this week\'s column'],
  ['decide', 'Copied the numbers'],
  ['decide', 'Opened the shop page'],
  ['you', 'The shop asked a robot check'],
];

function robotThing(now: number): WorkerThing {
  return { ...askingThing(now), id: 'wt_ROBOT', lane: 'aster/sheets', told: 'Copy this week\'s stock numbers to the shop', steps: steps(ROBOT), stop: 'robot_check' };
}

function doneThing(now: number): WorkerThing {
  const done: [WorkerStepKind, string][] = [...WEEKLY_POST.slice(0, 27), ['yes', 'Demo Owner said yes, and the post went out'], ['decide', 'Opened the post, and it is live']];
  return { ...liveThing(now), id: 'wt_DONE', state: 'done', steps: steps(done), started_at: iso(now - 58 * MINUTE), ended_at: iso(now - 52 * MINUTE), run_id: 'run_DONE' };
}

function limitThing(now: number): WorkerThing {
  return { ...liveThing(now), id: 'wt_LIMIT', state: 'stopped', stop: 'limit', steps: steps(WEEKLY_POST.slice(0, 19)), ended_at: iso(now - 12 * MINUTE) };
}

/** The question a worker asks in the demo. It goes in with the rest of Needs you. */
export function workerQuestion(now: number, id: string): Question {
  return {
    id,
    state: 'open',
    lane: 'aster/portal',
    question: 'Two orders are called Guji natural. Which one should get the two extra bags?',
    options: ['Order 4471, due Friday', 'Order 4480, due next week', 'Neither, leave them'],
    answer: null,
    asked_by: { kind: 'worker', id: 'browser', thread: null },
    created_at: iso(now - 40 * MINUTE),
    expires_at: iso(now + 20 * 60 * MINUTE),
    answered_at: null,
    answered_by: null,
    worker: askingThing(now),
  };
}

/** The services this demo adds beside the ones the fleet already had. */
const ADDED: Brain[] = [
  { name: 'typesafe', kind: 'metered', provider: 'typesafe', state: 'ready', seats: null, window_used_pct: null, computer: 'home-mini', models: [{ id: 'jev', can: ['decide'], usd_per_step: 0.0002 }] },
  {
    name: 'openrouter',
    kind: 'metered',
    provider: 'openrouter',
    state: 'ready',
    seats: null,
    window_used_pct: null,
    computer: 'home-mini',
    models: [
      { id: 'jev', can: ['decide'], usd_per_step: 0.00025 },
      { id: 'mistral-small', can: ['write'], usd_per_step: 0.0011 },
    ],
  },
];

const KNOWN: Record<string, Pick<Brain, 'computer' | 'models'>> = {
  'claude-max': { computer: 'home-mini', models: [{ id: 'claude-sonnet-5-5', can: ['agent', 'rescue', 'write'], usd_per_step: null }] },
  'ollama-local': { computer: 'home-mini', models: [{ id: 'llama-3.3-8b', can: ['write'], usd_per_step: 0 }] },
  'anthropic-api': { computer: 'hetzner-1', models: [{ id: 'claude-sonnet-5-5', can: ['agent', 'rescue', 'write'], usd_per_step: 0.021 }] },
};

interface WorkerState {
  on: Set<string>;
  /** Accounts a person took it off. */
  off: Set<string>;
  jobs: Record<WorkerJob, string | null>;
  limits: WorkerLimits;
  handovers: Map<string, BrainHandover & { opened: number }>;
  added: Set<string>;
}

const state = new WeakMap<OrgData, WorkerState>();

function stateOf(data: OrgData): WorkerState {
  let found = state.get(data);
  if (!found) {
    const fresh = scenario.has('workers-new') || scenario.has('workers-ready');
    found = {
      on: new Set(fresh ? [] : ['home-mini']),
      off: new Set(fresh ? [] : ['aster/linkedin']),
      jobs: { decide: scenario.has('workers-new') ? null : 'typesafe', write: scenario.has('workers-new') ? 'claude-max' : 'openrouter', rescue: 'claude-max' },
      limits: { steps_per_day: 5_000, help_per_day: 40, steps_per_thing: LIMIT, usd_per_day: 2 },
      handovers: new Map(),
      added: new Set(scenario.has('workers-new') ? [] : ADDED.map((item) => item.name)),
    };
    state.set(data, found);
  }
  return found;
}

/** The fleet's own services, with what the concept knows about each, and whatever was added. */
export function workerBrains(data: OrgData): Brain[] {
  if (!data.brains.length) return [];
  const mine = stateOf(data);
  return [...data.brains.map((item) => ({ ...item, ...KNOWN[item.name] })), ...ADDED.filter((item) => mine.added.has(item.name))];
}

const nodeState = (node: OrgData['nodes'][number]): WorkerPlace['state'] => (node.id === 'home-mini' && scenario.has('workers-offline') ? 'down' : node.state);

/** Turned on in this demo a moment ago, so it has done nothing yet and says so. */
const justStarted = () => scenario.has('workers-new') || scenario.has('workers-ready');

const laneAsks = (data: OrgData, lane: string) => data.askFirst.some((item) => item.scope === 'lane' && item.id === lane && item.ask_first);

function latestThing(data: OrgData, now: number): WorkerThing | null {
  const mine = stateOf(data);
  if (!mine.on.size || justStarted()) return null;
  if (scenario.has('workers-asked')) return askingThing(now);
  if (scenario.has('workers-robot')) return robotThing(now);
  if (scenario.has('workers-limit')) return limitThing(now);
  if (scenario.has('workers-offline')) return doneThing(now);
  const live = liveThing(now, laneAsks(data, 'aster/instagram'));
  // Taken off the account part way through: it stops there, and the agent carries on directly.
  return mine.off.has(live.lane) ? { ...live, state: 'stopped', ended_at: iso(now) } : live;
}

function accountOf(data: OrgData, id: string | null): WorkerAccount | null {
  const mine = stateOf(data);
  const lane = data.lanes.find((item) => item.id === id);
  const node = data.nodes.find((item) => item.id === lane?.node);
  if (!lane || !node || lane.kind !== 'browser' || lane.site?.key === 'terminal' || !mine.on.has(node.id)) return null;
  return { lane: lane.id, on: !mine.off.has(lane.id), online: nodeState(node) !== 'down', after_a_page_asks: afterAPageAsks() };
}

function overview(data: OrgData): WorkerOverview {
  const now = Date.now();
  const mine = stateOf(data);
  const brains = workerBrains(data);
  const things = scenario.has('workers-limit') ? 212 : 46;
  const canDecide = (node: string) => brains.some((item) => item.computer === node && item.models?.some((model) => model.can.includes('decide')));
  const lanesOn = (node: string) => data.lanes.filter((lane) => lane.node === node && lane.kind === 'browser');
  const worksOn = (node: string) => lanesOn(node).filter((lane) => !mine.off.has(lane.id));
  const places = data.nodes.map((node): WorkerPlace => ({
    node: node.id,
    state: nodeState(node),
    worker: mine.on.has(node.id) ? 'on' : canDecide(node.id) ? 'off' : 'needs_service',
    missing: canDecide(node.id) ? null : 'decide',
    things_today: mine.on.has(node.id) && !justStarted() ? things : 0,
    accounts: worksOn(node.id).length,
    accounts_off: lanesOn(node.id).filter((lane) => mine.off.has(lane.id)).map((lane) => lane.id),
    accounts_asking: worksOn(node.id).filter((lane) => laneAsks(data, lane.id)).length,
    after_a_page_asks: afterAPageAsks(),
    suggested_by: null,
  }));
  const running = mine.on.size > 0 && !justStarted();
  const limited = scenario.has('workers-limit');
  const busy = scenario.has('workers-asked') || scenario.has('workers-robot');
  const latest = latestThing(data, now);
  return {
    plugin: 'agentos.worker.browser',
    version: '0.3.0',
    places,
    jobs: (['decide', 'write', 'rescue'] as const).map((job) => {
      const brain = brains.find((item) => item.name === mine.jobs[job]);
      return { job, brain: brain?.name ?? null, model: brain?.models?.find((model) => model.can.includes(job))?.id ?? null };
    }),
    limits: mine.limits,
    today: running
      ? { things, steps: limited ? mine.limits.steps_per_day : 1_138, helped: limited ? 31 : 9, asked: busy ? 2 : 1, waiting: data.questions.filter((item) => item.state === 'open' && item.worker).length, longest: 41, usd: limited ? 1.84 : 0.41 }
      : { things: 0, steps: 0, helped: 0, asked: 0, waiting: 0, longest: 0, usd: 0 },
    latest: latest && { ...latest, limit: mine.limits.steps_per_thing },
    later: data.nodes.length
      ? [
          {
            kind: 'android',
            sentence: 'It needs a Linux computer with 16 GB to spare.',
            places: data.nodes.map((node) => ({
              node: node.id,
              why: /mac/i.test(node.os) ? 'It needs Linux, and this is a Mac' : `It needs 16 GB to spare, and this one has ${Math.floor((node.mem.total_mb - node.mem.used_mb) / 1_024)} GB`,
              fact: /mac/i.test(node.os) ? 'is a Mac' : `has ${Math.floor((node.mem.total_mb - node.mem.used_mb) / 1_024)} GB to spare`,
            })),
          },
          { kind: 'desktop', sentence: 'It will do the same for apps on a computer\'s screen.', places: [] },
        ]
      : [],
  };
}

function onComputer(data: OrgData, id: string): WorkerOnComputer {
  const view = overview(data);
  const place = view.places.find((item) => item.node === id);
  const android = view.later.find((item) => item.kind === 'android');
  return {
    node: id,
    runs: place?.worker === 'on' ? { kind: 'browser', on: true, latest: view.latest, things_today: place.things_today } : null,
    cannot: [
      ...(android?.places.filter((item) => item.node === id).map((item) => ({ kind: 'android' as const, why: item.why })) ?? []),
      ...(place && place.worker !== 'on' ? [{ kind: 'browser' as const, why: place.worker === 'needs_service' ? 'It has no AI service here to decide each step' : 'Nobody has turned it on here' }] : []),
    ],
    keeps: workerBrains(data).filter((item) => item.computer === id).map((item) => item.name),
  };
}

/** A few of the day's stories were done by the worker, on the computer it runs on. */
const STEP_WORDS = ['Opened the site', 'Checked it is the right account', 'Found the right page', 'Waited for the page to settle', 'Pressed through to the form', 'Filled in the first field', 'Filled in the next field', 'Checked the page against what it was told'];

export function storyWorker(story: HistoryStory, data: OrgData): WorkerThing | null {
  if (!stateOf(data).on.size || story.source !== 'run' || story.verdict === 'running') return null;
  if (!story.lane.startsWith('aster/') && !story.lane.startsWith('personal/')) return null;
  const seed = [...story.id].reduce((sum, char) => sum + char.charCodeAt(0), 0);
  if (seed % 3 === 0) return null;
  const length = 12 + (seed % 26);
  const list = Array.from({ length }, (_, index) => {
    if (index === length - 1) return step('decide', 'Checked the result on the page', index);
    if (index === Math.floor(length / 2) && seed % 2) return step('rescue', 'The page looked different. A stronger AI model said where to press', index);
    if (index === Math.floor(length * 0.7)) return step('write', 'Wrote the text this step needed', index);
    return step('decide', STEP_WORDS[index % STEP_WORDS.length] ?? 'Took the next step', index);
  });
  return {
    id: `wt_${story.id}`,
    worker: 'browser',
    lane: story.lane,
    node: 'home-mini',
    said_by: { kind: story.who.kind, id: story.who.id, thread: null },
    told: story.sentence,
    state: 'done',
    steps: list,
    stop: null,
    limit: LIMIT,
    started_at: story.started_at,
    ended_at: story.at,
    run_id: story.id,
  };
}

export function workerHandlers({ org, failure, stepUpNeeded, stepUpValid }: Pick<Helpers, 'org' | 'failure' | 'stepUpNeeded' | 'stepUpValid'>) {
  return [
    http.get('/api/orgs/:org/workers', org(async (data) => {
      await delay(120);
      return HttpResponse.json(overview(data));
    })),
    http.get('/api/orgs/:org/workers/computers/:id', org((data, { params }) => HttpResponse.json(onComputer(data, String(params.id))))),
    http.get('/api/orgs/:org/workers/things', org((data, { request }) => {
      const lane = new URL(request.url).searchParams.get('lane');
      const now = Date.now();
      const latest = latestThing(data, now);
      if (latest?.lane === lane) return HttpResponse.json(latest);
      if (lane === 'aster/portal' && stateOf(data).on.size) return HttpResponse.json(askingThing(now));
      return HttpResponse.json(null);
    })),
    // A person gives it a job. Here it only starts: the concept has no steps to run.
    http.post('/api/orgs/:org/workers/things', org(async (data, { request }) => {
      await delay(300);
      const body = (await request.json()) as { lane: string; told: string };
      const account = accountOf(data, body.lane);
      if (!account?.on) return failure(409, 'conflict', 'The browser worker is not running on this account.');
      const now = Date.now();
      return HttpResponse.json({
        ...askingThing(now),
        id: `thing_${now}`,
        lane: body.lane,
        told: body.told,
        state: 'working',
        steps: [],
        stop: null,
        said_by: { kind: 'human', id: 'you' },
        ended_at: null,
      } satisfies WorkerThing);
    })),
    http.get('/api/orgs/:org/workers/accounts', org((data, { request }) => HttpResponse.json(accountOf(data, new URL(request.url).searchParams.get('lane'))))),
    http.post('/api/orgs/:org/workers/accounts', org(async (data, { request }) => {
      await delay(500);
      const body = (await request.json()) as { lane: string; on: boolean };
      const account = accountOf(data, body.lane);
      if (!account) return failure(404, 'not_found', 'No worker runs on this account.');
      if (!account.online) return failure(409, 'computer_offline', 'That computer is offline.');
      if (!mayTurn(data.role, !body.on)) return failure(403, 'forbidden', 'An owner or admin can do this.');
      // As on a computer: only a person turns it on, so that direction asks who is there.
      if (body.on && !stepUpValid()) return stepUpNeeded();
      const { off } = stateOf(data);
      if (body.on) off.delete(body.lane);
      else off.add(body.lane);
      return HttpResponse.json({ ...account, on: body.on } satisfies WorkerAccount);
    })),
    http.post('/api/orgs/:org/workers/computers/:id', org(async (data, { params, request }) => {
      await delay(500);
      const body = (await request.json()) as { on: boolean };
      // Only a person turns a worker on, so that is the one direction that asks who is there.
      if (body.on && !stepUpValid()) return stepUpNeeded();
      const mine = stateOf(data);
      const id = String(params.id);
      if (body.on) mine.on.add(id);
      else mine.on.delete(id);
      return HttpResponse.json(overview(data));
    })),
    http.post('/api/orgs/:org/workers/jobs/:job', org(async (data, { params, request }) => {
      await delay(200);
      const body = (await request.json()) as { brain: string };
      stateOf(data).jobs[params.job as WorkerJob] = body.brain;
      return HttpResponse.json(overview(data));
    })),
    http.post('/api/orgs/:org/workers/limits', org(async (data, { request }) => {
      await delay(200);
      const mine = stateOf(data);
      mine.limits = { ...mine.limits, ...((await request.json()) as Partial<WorkerLimits>) };
      return HttpResponse.json(overview(data));
    })),
    // Says which service and which computer. There is nothing here a key could arrive in.
    http.post('/api/orgs/:org/brains/handovers', org(async (data, { request }) => {
      const body = (await request.json()) as { service: string; computer: string };
      const node = data.nodes.find((item) => item.id === body.computer);
      if (!node || nodeState(node) === 'down') return failure(409, 'computer_offline', 'That computer is offline.');
      const id = `ho_${Math.random().toString(36).slice(2, 10)}`;
      const made = { id, service: body.service, computer: body.computer, stage: 'waiting' as const, opened: 0 };
      stateOf(data).handovers.set(id, made);
      return HttpResponse.json({ id, service: made.service, computer: made.computer, stage: made.stage } satisfies BrainHandover);
    })),
    http.get('/api/orgs/:org/brains/handovers/:id', org((data, { params }) => {
      const mine = stateOf(data);
      const found = mine.handovers.get(String(params.id));
      if (!found) return failure(404, 'not_found', 'This hand-over has ended.');
      return HttpResponse.json({ id: found.id, service: found.service, computer: found.computer, stage: found.stage } satisfies BrainHandover);
    })),
    // What a computer would report. The drawn form stands in for it, and says only that it was used.
    http.post('/api/orgs/:org/brains/handovers/:id/filled', org(async (data, { params }) => {
      const mine = stateOf(data);
      const found = mine.handovers.get(String(params.id));
      if (!found) return failure(404, 'not_found', 'This hand-over has ended.');
      found.stage = 'checking';
      setTimeout(() => {
        found.stage = scenario.has('key-refused') ? 'failed' : 'done';
        if (found.stage === 'done') {
          mine.added.add(found.service);
          const brain = ADDED.find((item) => item.name === found.service);
          if (brain) brain.computer = found.computer;
          if (found.service === 'typesafe' && !mine.jobs.decide) mine.jobs.decide = 'typesafe';
          if (found.service === 'openrouter' && !mine.jobs.decide) mine.jobs.decide = 'openrouter';
        }
      }, 1_800);
      return HttpResponse.json({ id: found.id, service: found.service, computer: found.computer, stage: found.stage } satisfies BrainHandover);
    })),
  ];
}
