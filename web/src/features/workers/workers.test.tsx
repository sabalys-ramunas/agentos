import { screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import type { Brain, LaneAskFirst, WorkerOverview, WorkerThing } from '@/api/types';
import { accountAsk } from '@/lib/ask-first';
import { db, resetDb } from '@/mocks/handlers';
import { server } from '@/test/setup';
import { renderApp } from '@/test/render';
import { accountWorkerSentence, addCommand, inWords, limitsTitle, modelsAnswer, placeAsks, placeSentence, SERVICES, serviceInfo, serviceName, servicesFor, thingLine, workerAnswer } from './words';

const ORG = '/northwind-group';

vi.setConfig({ testTimeout: 20_000 });

const here = __dirname;
const sources = readdirSync(here).filter((name) => /\.(ts|tsx)$/.test(name) && !name.includes('.test.'));
const read = (name: string) => readFileSync(join(here, name), 'utf8');
const mock = readFileSync(join(here, '../../mocks/workers.ts'), 'utf8');
const models = readFileSync(join(here, '../settings/brains.tsx'), 'utf8');
const types = readFileSync(join(here, '../../api/types.ts'), 'utf8');

const thing = (over: Partial<WorkerThing> = {}): WorkerThing => ({
  id: 't',
  worker: 'browser',
  lane: 'aster/instagram',
  node: 'home-mini',
  said_by: { kind: 'agent', id: 'claude-code', thread: null },
  told: 'Post it',
  state: 'working',
  steps: [{ kind: 'decide', what: 'Opened it', model: 'jev', ms: 90, usd: 0.0002 }],
  stop: null,
  limit: 60,
  started_at: new Date().toISOString(),
  ended_at: null,
  run_id: null,
  ...over,
});

const overview = (over: Partial<WorkerOverview> = {}): WorkerOverview => ({
  plugin: 'agentos.worker.browser',
  version: '0.3.0',
  places: [{ node: 'home-mini', state: 'up', worker: 'on', missing: null, things_today: 46, accounts: 13, accounts_off: [], accounts_asking: 0, after_a_page_asks: false, suggested_by: null }],
  jobs: [],
  limits: { steps_per_day: 5_000, help_per_day: 40, steps_per_thing: 60, usd_per_day: 2 },
  today: { things: 46, steps: 1_138, helped: 9, asked: 1, waiting: 1, longest: 41, usd: 0.41 },
  latest: thing(),
  later: [],
  ...over,
});

const brain = (name: string, can: ('agent' | 'decide' | 'write' | 'rescue')[], over: Partial<Brain> = {}): Brain => ({
  name,
  kind: 'metered',
  provider: name,
  state: 'ready',
  seats: null,
  window_used_pct: null,
  models: [{ id: `${name}-model`, can }],
  ...over,
});

describe('what the hosted panel must never hold', () => {
  it('has no field a key could enter, the drawn form included', () => {
    for (const name of sources) expect(`${name}: ${read(name)}`).not.toMatch(/type=["']password["']|<input|<Input|<textarea|<Textarea/);
    expect(models).not.toMatch(/type=["']password["']|<input|<Input|<textarea|<Textarea/);
  });

  it('takes no key in any type, route or handler', () => {
    const concept = types.slice(types.indexOf('Workers, at concept stage'));
    expect(concept).not.toMatch(/\b(key|token|secret|password)\??:\s*string/);
    expect(mock).not.toMatch(/body\.(key|secret|token)|\{ key\b/);
    // A key, a password, a passphrase or a token is never held in state by the app: it is typed on the computer.
    expect(read('add-service.tsx')).not.toMatch(/useState[^\n]*(key|password|passphrase|secret|token)/i);
  });

  it('keeps the real command to the one place it is written, and behind Technical details', () => {
    expect(addCommand(SERVICES[0]!)).toBe('agentos brains add typesafe --kind metered --provider typesafe --stdin');
    expect(read('add-service.tsx')).toMatch(/label="Technical details" open=\{technical\}/);
  });
});

describe('words', () => {
  it('answers before it lists, and leads with what needs a person', () => {
    expect(workerAnswer(overview())).toMatchObject({ state: 'working', lead: 'It did 46 things today in 1,138 steps.', rest: '9 needed help, and 1 stopped for you.' });
    expect(workerAnswer(overview({ latest: thing({ state: 'asked', stop: 'unsure' }) }))).toMatchObject({ state: 'waiting', lead: 'It stopped and asked you.', next: 'needs-you' });
    expect(workerAnswer(overview({ latest: thing({ state: 'asked', stop: 'robot_check' }) }))).toMatchObject({ lead: 'A robot check stopped it.', rest: 'That always goes to a person.' });
  });

  it('says what stands in the way of turning it on, and offers the way past', () => {
    const off = overview({ latest: null, places: [{ node: 'home-mini', state: 'up', worker: 'off', missing: null, things_today: 0, accounts: 13, accounts_off: [], accounts_asking: 0, after_a_page_asks: false, suggested_by: null }] });
    expect(workerAnswer(off)).toMatchObject({ label: 'Off', next: 'turn-on' });
    const none = overview({ latest: null, places: [{ node: 'home-mini', state: 'up', worker: 'needs_service', missing: 'decide', things_today: 0, accounts: 13, accounts_off: [], accounts_asking: 0, after_a_page_asks: false, suggested_by: null }] });
    expect(workerAnswer(none)).toMatchObject({ next: 'add-service', rest: 'It needs an AI service to decide each step.' });
  });

  it('never reads an offline computer or a spent limit as fine', () => {
    const away = overview({ places: [{ node: 'home-mini', state: 'down', worker: 'on', missing: null, things_today: 46, accounts: 13, accounts_off: [], accounts_asking: 0, after_a_page_asks: false, suggested_by: null }] });
    expect(workerAnswer(away)).toMatchObject({ state: 'stuck', label: 'Offline', lead: 'Mac mini (home) is offline.' });
    const spent = overview({ today: { things: 212, steps: 5_000, helped: 31, asked: 1, waiting: 0, longest: 41, usd: 1.84 } });
    expect(workerAnswer(spent)).toMatchObject({ label: 'Stopped for today', rest: 'It starts again at midnight.' });
    expect(limitsTitle(spent)).toBe('It reached a limit today');
    expect(limitsTitle(overview({ today: { things: 1, steps: 4_100, helped: 1, asked: 0, waiting: 0, longest: 1, usd: 0 } }))).toBe('Close to its limits today');
    expect(limitsTitle(overview())).toBe('Inside its limits today');
  });

  it('only offers a service for a job it can do', () => {
    const brains = [brain('typesafe', ['decide']), brain('openrouter', ['decide', 'write']), brain('claude-max', ['agent', 'rescue', 'write'])];
    expect(servicesFor('write', brains).map((item) => item.name)).toEqual(['openrouter', 'claude-max']);
    expect(servicesFor('decide', brains).map((item) => item.name)).toEqual(['typesafe', 'openrouter']);
    expect(servicesFor('rescue', brains).map((item) => item.name)).toEqual(['claude-max']);
  });

  it('says the state of the services before the list', () => {
    expect(modelsAnswer([brain('typesafe', ['decide'])])).toBe('Your AI service is ready.');
    expect(modelsAnswer([brain('typesafe', ['decide']), brain('anthropic-api', ['agent'], { provider: 'anthropic', state: 'cooling_down' })])).toBe('1 of 2 are ready. Claude (API) is cooling down, and comes back by itself.');
  });

  it('keeps engine words out of what a person reads', () => {
    const words = (read('words.ts').replace(/\/\*\*[\s\S]*?\*\//g, '').replaceAll("\\'", '').replace(/\$\{[^}]*\}/g, '').replace(/^import .*$/gm, '').match(/'[^'\n]*'|`[^`\n]*`/g) ?? []).filter((text) => !text.includes('agentos brains add')).join(' ');
    expect(words.length).toBeGreaterThan(500);
    expect(words).not.toMatch(/\bjev\b|\btokens?\b|\bmilliseconds?\b|\bplugins?\b|\bbrains?\b|\bescalat/i);
    expect(words).not.toMatch(/ladder/i);
    expect(serviceName({ name: 'typesafe', kind: 'metered', provider: 'typesafe' })).toBe('TypeSafe');
    expect(serviceInfo('nothing')).toBeUndefined();
    expect(inWords(['a', 'b', 'c'])).toBe('a, b and c');
    expect(placeSentence({ node: 'hetzner-1', state: 'down', worker: 'on', missing: null, things_today: 0, accounts: 1, accounts_off: [], accounts_asking: 0, after_a_page_asks: false, suggested_by: null })).toMatch(/^Offline/);
    expect(thingLine(thing({ state: 'asked', stop: 'robot_check' }))).toBe('The browser worker stopped after 1 step, because this needs a person.');
  });

  it('counts how its posts go in the words of Ask me first, and says what off means either way round', () => {
    expect(placeAsks({ accounts: 12, accounts_asking: 3, after_a_page_asks: false })).toBe('Posts: Asks you first on 3 of 12 accounts.');
    expect(placeAsks({ accounts: 12, accounts_asking: 0, after_a_page_asks: false })).toBe('Posts: Runs on its own on all 12 accounts.');
    expect(placeAsks({ accounts: 12, accounts_asking: 12, after_a_page_asks: false })).toBe('Posts: Asks you first on all 12 accounts.');
    expect(placeAsks({ accounts: 1, accounts_asking: 0, after_a_page_asks: false })).toBe('Posts: Runs on its own on its one account.');
    expect(placeAsks({ accounts: 0, accounts_asking: 0, after_a_page_asks: false })).toBe('');
    expect(placeAsks({ accounts: 12, accounts_asking: 0, after_a_page_asks: true })).toBe('Posts: Asks you first on all 12 accounts, because it reads a page first.');
    expect(accountWorkerSentence({ on: false, online: true })).toBe('An agent can still work here directly, and you can take over.');
    expect(accountWorkerSentence({ on: true, online: true })).toMatch(/^With it off, /);
    expect(accountWorkerSentence({ on: true, online: false })).toMatch(/offline/);
  });
});

describe('the worker on one account', () => {
  const INSTAGRAM = `${ORG}/lanes/aster/instagram`;
  const LINKEDIN = `${ORG}/lanes/aster/linkedin`;
  const ASK: LaneAskFirst = { lane: 'a/b', ask_first: false, asks_first: false, ask_first_set_by: null, ask_first_set_at: null, ask_first_supported: true, ask_first_online: true };
  const row = async () => within(await screen.findByRole('region', { name: 'Browser worker' }, { timeout: 15_000 }));
  const steppedUp = () => {
    db.stepUpUntil = new Date(Date.now() + 600_000).toISOString();
  };

  beforeEach(() => resetDb());

  it('names the worker in Ask me first only while it works on the account', async () => {
    const { user } = renderApp(INSTAGRAM);
    const switchRow = await row();
    const ask = within(await screen.findByRole('region', { name: 'Ask me first' }));
    expect(await ask.findByText(/The browser worker reads a page first, so its posts still ask you\./)).toBeInTheDocument();
    await user.click(switchRow.getByRole('switch', { name: 'Browser worker on Instagram' }));
    await waitFor(() => expect(ask.queryByText(/browser worker/)).toBeNull());
    expect(ask.getAllByRole('switch')).toHaveLength(1);
    expect(await switchRow.findByText('An agent can still work here directly, and you can take over.')).toBeInTheDocument();
  });

  it('takes a job from a person, and closes once it has started', async () => {
    const { user } = renderApp(INSTAGRAM);
    const region = await row();
    await user.click(region.getByRole('button', { name: 'Give it a job' }));
    const dialog = within(await screen.findByRole('dialog'));
    expect(dialog.getByRole('button', { name: 'Start' })).toBeDisabled();
    await user.type(dialog.getByLabelText('The job'), 'Post the weekly photo');
    await user.click(dialog.getByRole('button', { name: 'Start' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  });

  it('says its posts wait on an account that asks first', async () => {
    db.orgs.get('northwind-group')!.askFirst.push({ scope: 'lane', id: 'aster/instagram', ask_first: true, set_by: { kind: 'user', id: 'usr_DEMO' }, set_at: new Date().toISOString() });
    renderApp(INSTAGRAM);
    const ask = within(await screen.findByRole('region', { name: 'Ask me first' }, { timeout: 15_000 }));
    expect(await ask.findByText(/The browser worker's posts wait for it too\./)).toBeInTheDocument();
  });

  it('promises a post goes alone only on a computer that lets it', async () => {
    expect(accountAsk(ASK, { lane: 'a/b', on: true, online: true, after_a_page_asks: false })?.sentence).toMatch(/posts go out on their own too\.$/);
    expect(accountAsk(ASK, { lane: 'a/b', on: true, online: true, after_a_page_asks: true })?.sentence).toMatch(/so its posts still ask you\.$/);
    expect(accountAsk(ASK, { lane: 'a/b', on: false, online: true, after_a_page_asks: true })?.sentence).toBe('Outside a routine, agents act here without waiting for you.');
    expect(accountAsk(ASK, null)?.sentence).toBe('Outside a routine, agents act here without waiting for you.');
  });

  it('lets a member take it off, and leaves turning it on to an owner or admin', async () => {
    db.orgs.get('northwind-group')!.role = 'member';
    renderApp(LINKEDIN);
    const region = await row();
    const control = region.getByRole('switch', { name: 'Browser worker on LinkedIn' });
    expect(control).toHaveAttribute('aria-disabled', 'true');
    expect(region.getByText(/An owner or admin can turn this on\./)).toBeInTheDocument();
  });

  it('asks who is there before it turns back on, and a cancelled check changes nothing', async () => {
    const { user } = renderApp(LINKEDIN);
    const control = (await row()).getByRole('switch', { name: 'Browser worker on LinkedIn' });
    expect(control).not.toBeChecked();
    await user.click(control);
    const dialog = await screen.findByRole('dialog', { name: /Confirm it is you/ });
    expect(within(dialog).getByText(/The worker will act on this account again/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: 'Close' }));
    await waitFor(() => expect(screen.getByRole('switch', { name: 'Browser worker on LinkedIn' })).not.toBeChecked());
    expect(screen.queryByText(/did not answer/)).toBeNull();
  });

  it('rolls back with a line when the computer does not answer', async () => {
    steppedUp();
    server.use(http.post('/api/orgs/:org/workers/accounts', () => HttpResponse.json({ error: { code: 'unavailable', message: 'No.' } }, { status: 503 })));
    const { user } = renderApp(INSTAGRAM);
    const control = (await row()).getByRole('switch', { name: 'Browser worker on Instagram' });
    await user.click(control);
    expect(await screen.findByText('Mac mini (home) did not answer, so the worker stays on here.')).toBeInTheDocument();
    await waitFor(() => expect(control).toBeChecked());
    expect(control).toHaveFocus();
  });

  it('shows the state with no switch to someone who cannot change it', async () => {
    db.orgs.get('northwind-group')!.role = 'viewer';
    renderApp(INSTAGRAM);
    const region = await row();
    expect(region.getByText('On')).toBeInTheDocument();
    expect(region.queryByRole('switch')).toBeNull();
    expect(region.getByText(/An owner or admin can change this\./)).toBeInTheDocument();
  });

  it('has no row on a computer that runs no worker', async () => {
    renderApp(`${ORG}/lanes/harbor/quickbooks`);
    await screen.findByRole('region', { name: 'Ask me first' }, { timeout: 15_000 });
    expect(screen.queryByRole('region', { name: 'Browser worker' })).toBeNull();
    expect(screen.queryByText(/browser worker/i)).toBeNull();
  });

  it('shows the same state under where it runs, with a link to the switch', async () => {
    renderApp(`${ORG}/settings/workers`);
    const link = await screen.findByRole('link', { name: 'Aster Coffee LinkedIn' }, { timeout: 15_000 });
    expect(link).toHaveAttribute('href', expect.stringContaining('/accounts/aster/linkedin'));
    expect(screen.getByText(/Posts: Asks you first on all \d+ accounts, because it reads a page first\./)).toBeInTheDocument();
  });
});

describe('the Workers page', () => {
  it('draws a switch only where it would change something', async () => {
    renderApp(`${ORG}/settings/workers`);
    await screen.findByText(/It did 46 things today/, undefined, { timeout: 15_000 });
    expect(screen.getByRole('switch', { name: 'Browser worker on Mac mini (home)' })).toBeChecked();
    expect(screen.queryByRole('switch', { name: 'Browser worker on Cloud server' })).toBeNull();
    expect(screen.getByText(/Nothing here can decide each step yet/)).toBeInTheDocument();
  });

  it('says the Android worker calmly: words, no button and no dead switch', async () => {
    renderApp(`${ORG}/settings/workers`);
    const later = await screen.findByRole('region', { name: 'Later' }, { timeout: 15_000 });
    expect(within(later).getByText(/needs a Linux computer with 16 GB/)).toBeInTheDocument();
    expect(within(later).queryByRole('button')).toBeNull();
    expect(within(later).queryByRole('switch')).toBeNull();
  });

  it('keeps cost and model names out until Technical details is on', async () => {
    renderApp(`${ORG}/settings/workers`);
    await screen.findByText(/It did 46 things today/, undefined, { timeout: 15_000 });
    expect(screen.queryByText(/\$/)).toBeNull();
    expect(screen.queryByText(/\bjev\b/)).toBeNull();
  });

  it('reads each step in one fixed place, by keyboard too', async () => {
    renderApp(`${ORG}/settings/workers`);
    const slider = await screen.findByRole('slider', {}, { timeout: 15_000 });
    await waitFor(() => expect(slider).toHaveAttribute('aria-valuetext', expect.stringContaining('Step')));
  });
});

describe('AI models', () => {
  it('reads AI models, not brains, and hands a key over rather than taking one', async () => {
    renderApp(`${ORG}/settings/brains`);
    await screen.findByRole('heading', { name: 'AI models' }, { timeout: 15_000 });
    expect(await screen.findByText('TypeSafe')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Add/ })).toHaveAttribute('href', expect.stringContaining('/settings/brains/add'));
    expect(screen.queryByText(/brain/i)).toBeNull();
  });
});
