import { screen, waitFor, within } from '@testing-library/react';
import type { EnvCheckpoint, EnvReplayTimeline } from '@/api/types';
import { renderApp } from '@/test/render';
import { checkpointRows } from './checkpoint-words';
import { allowCommand } from './network-card';
import { openedWords, otherPortWords, scaleEnd, seconds } from './opened-words';
import { actionWords, buildReplay, causeWords, dwell, frameAt, jump, offset } from './replay-model';

const ORG = 'northwind-group';
const T = { timeout: 4000 };
const iso = (s: number) => new Date(Date.UTC(2026, 10, 18, 10, 0, s)).toISOString();

describe('the words of how one opened', () => {
  it('reads seconds as a person does', () => {
    expect(seconds(0.64)).toBe('0.6 s');
    expect(seconds(52.4)).toBe('52 s');
    expect(seconds(192.4)).toBe('3 min 12 s');
    expect(seconds(120)).toBe('2 min');
    expect(scaleEnd(4.1)).toBe(5);
    expect(scaleEnd(123.8)).toBe(180);
  });

  it('leads with how long and from what, then where the time went', () => {
    const words = openedWords({
      state: 'working',
      bringup: { total_s: 4.1, steps: [] },
      prepared: { used: true, warm: 'warm pool', from: { commit: 'abc', age: '2 days' }, setup_s: { build: 192.4, here: 1.4, saved: 191 } },
    });
    expect(words.lead).toBe('Opened in 4.1 s on a warm, prepared machine.');
    expect(words.detail).toBe('Setup ran when it was built 2 days ago, so here it only refreshed: 1.4 s instead of 3 min 12 s.');
    const cold = openedWords({ state: 'ready', bringup: { total_s: 123.8, steps: [] }, prepared: { used: false, why: 'no prepared machine yet', setup: { ok: true, seconds: 51.6 }, build: { started: true } } });
    expect(cold.lead).toBe('Opened in 2 min 4 s.');
    expect(cold.detail).toBe('No prepared machine yet. Setup took 52 s. A prepared machine is being built from it, so the next open skips setup.');
    const waking = openedWords({ state: 'starting', resumed: { by: 'You', at: iso(0), from: 'snapshot', eta_s: 56 }, bringup: { total_s: 20, steps: [], now: { step: 'VM ready', so_far_s: 8.8 } } });
    expect(waking.lead).toBe('Boot: 8.8 s so far, 20 s since you resumed it.');
  });

  it('names the port the app listens on instead, and the one line that fixes it', () => {
    expect(otherPortWords({ command: 'pnpm dev', port: 5173, listening: false, listening_instead: [3000] })).toEqual({ sentence: expect.stringMatching(/^Port 3000 listens instead/), fix: 'port = 3000' });
    expect(otherPortWords({ command: 'pnpm dev', port: 5173, listening: false, listening_instead: [3000, 9229] })).toBeNull();
    expect(otherPortWords({ command: 'pnpm dev', port: 3000, listening: true })).toBeNull();
  });
});

describe('checkpoints as a line of work', () => {
  const cp = (id: string, s: number, extra: Partial<EnvCheckpoint> = {}): EnvCheckpoint => ({ id, at: iso(s), note: id, repos: [], since: { files: 1, added: 1, removed: 0 }, ...extra });

  it('puts the work after the one it went back to on the side line, and the line goes on from that one', () => {
    const rows = checkpointRows([cp('c1', 0), cp('c2', 10), cp('c3', 20, { restored_at: iso(45) }), cp('c4', 30), cp('c5', 45, { auto: true })]);
    expect(rows.map((row) => (row.kind === 'checkpoint' ? `${row.number}${row.side ? ' side' : ''}${row.forks ? ' fork' : ''}` : row.kind))).toEqual(['now', 'restore', '5 side', '4 side', '3 fork', '2', '1']);
    expect(rows[0]).toMatchObject({ kind: 'now', base: { number: 3 } });
  });

  it('without a rollback, the working copy builds on the newest', () => {
    const rows = checkpointRows([cp('c1', 0), cp('c2', 10)]);
    expect(rows[0]).toMatchObject({ kind: 'now', base: { number: 2 } });
    expect(rows.some((row) => row.kind === 'restore')).toBe(false);
  });
});

describe('the replay model', () => {
  const timeline: EnvReplayTimeline = {
    environment: { id: 'x', title: 'X', state: 'closed', created_at: iso(0), closed_at: iso(120) },
    replay: { enabled: true, frames: 6, since: iso(5), until: iso(110), bytes: 1, kept_until: null },
    next: null,
    timeline: [
      { kind: 'frame', id: 'f0', at: iso(5), cause: 'agent', tool: 'browser_navigate', summary: 'http://localhost:3000/' },
      { kind: 'call', at: iso(8), tool: 'lane_exec', summary: 'git status', ok: true },
      { kind: 'frame', id: 'f1', at: iso(20), cause: 'change' },
      { kind: 'frame', id: 'f2', at: iso(40), cause: 'person' },
      { kind: 'frame', id: 'f3', at: iso(46), cause: 'person' },
      { kind: 'frame', id: 'f4', at: iso(80), cause: 'agent', tool: 'browser_act', summary: 'click "Pay"' },
      { kind: 'frame', id: 'f5', at: iso(110), cause: 'close' },
    ],
  };
  const model = buildReplay(timeline, [], timeline.environment.closed_at);

  it('finds the take-over from the run of person frames', () => {
    expect(model.takeovers).toEqual([{ from: Date.parse(iso(37)), to: Date.parse(iso(49)) }]);
  });

  it('steps, jumps to the next thing done, and finds the frame at a moment', () => {
    expect(jump(model, 0, 1)).toBe(2);
    expect(jump(model, 2, 1)).toBe(4);
    expect(jump(model, 4, -1)).toBe(2);
    expect(frameAt(model, Date.parse(iso(45)))).toBe(2);
    expect(offset(model, Date.parse(iso(80)))).toBe('1:20');
  });

  it('cuts quiet stretches short and plays faster with speed', () => {
    expect(dwell(model, 3, 1)).toBe(2_400);
    expect(dwell(model, 3, 4)).toBe(600);
  });

  it('says what led to a frame in plain words', () => {
    expect(actionWords('http://localhost:3000/cart')).toBe('Opened localhost:3000/cart');
    expect(actionWords('click "Add to cart"')).toBe('Clicked "Add to cart"');
    expect(actionWords('type "ada@example.com" into Email')).toBe('Typed "ada@example.com" into Email');
    expect(causeWords({ cause: 'change' })).toBe('The page changed on its own');
  });
});

describe('the environment concepts on the mock', () => {
  it('shows how the offer page opened: 4.1 s on a warm, prepared machine, its app listening', async () => {
    renderApp(`/${ORG}/environments/e7k2`);
    expect(await screen.findByText('Opened in 4.1 s on a warm, prepared machine.', undefined, T)).toBeInTheDocument();
    const app = screen.getByRole('region', { name: 'The app' });
    expect(within(app).getByText('Listening')).toBeInTheDocument();
    expect(within(app).getByRole('link', { name: 'Open the app on port 3000' })).toBeInTheDocument();
    expect(screen.getByText(/You are watching, so it stays open/)).toBeInTheDocument();
  });

  it('says why the app did not listen, with the end of its log and the line that fixes it', async () => {
    renderApp(`/${ORG}/environments/h5ap`);
    const app = await screen.findByRole('region', { name: 'The app' }, T);
    expect(within(app).getByText('Not listening')).toBeInTheDocument();
    expect(within(app).getByText(/Port 3000 listens instead/)).toBeInTheDocument();
    expect(within(app).getByText('port = 3000')).toBeInTheDocument();
    expect(screen.getByLabelText("The end of pnpm dev's output")).toBeInTheDocument();
  });

  it('lists the refused hosts of a limited network with the command that allows one, and secrets by name only', async () => {
    const { user } = renderApp(`/${ORG}/environments/n3lm`);
    const refused = await screen.findByRole('region', { name: 'Refused hosts' }, T);
    expect(within(refused).getAllByText('api.segment.io').length).toBeGreaterThan(0);
    expect(within(refused).getByText('41×')).toBeInTheDocument();
    expect(within(refused).getByText(/A secret's placeholder went here/)).toBeInTheDocument();
    expect(refused.textContent).toContain(allowCommand('api.segment.io'));
    await user.click(within(refused).getByRole('button', { name: 'Copy the command that allows fonts.googleapis.com' }));
    expect(await navigator.clipboard.readText()).toBe('agentos environments allow --host fonts.googleapis.com');
    const secrets = screen.getByRole('region', { name: 'App secrets' });
    expect(within(secrets).getByText('PARCELPOINT_API_KEY')).toBeInTheDocument();
    expect(within(secrets).queryByRole('textbox')).not.toBeInTheDocument();
  });

  it('draws the checkpoints with the rollback, and opens one checkpoint\'s diff', async () => {
    const { user } = renderApp(`/${ORG}/environments/c5kp`);
    const list = await screen.findByRole('list', { name: 'Checkpoints, newest first' }, T);
    expect(within(list).getByText('Went back to 3')).toBeInTheDocument();
    expect(within(list).getByText('Made by agentos')).toBeInTheDocument();
    await user.click(within(list).getByRole('button', { name: 'Show the changes of checkpoint 3' }));
    const dialog = await screen.findByRole('dialog', undefined, T);
    expect(await within(dialog).findByRole('region', { name: 'shop/src/cart/totals.ts' }, T)).toBeInTheDocument();
    await user.click(within(dialog).getByRole('radio', { name: 'From it to now' }));
    expect(await within(dialog).findByText(/A fixed discount that empties the cart still ships/, undefined, T)).toBeInTheDocument();
  });

  it('replays a closed environment frame by frame, from the keyboard', async () => {
    const { user } = renderApp(`/${ORG}/environments/r9cl`);
    const slider = await screen.findByRole('slider', { name: 'Replay position' }, { timeout: 6000 });
    const last = Number(slider.getAttribute('aria-valuemax'));
    expect(slider).toHaveAttribute('aria-valuenow', String(last));
    slider.focus();
    await user.keyboard('{Home}');
    expect(slider).toHaveAttribute('aria-valuenow', '0');
    await user.keyboard('{ArrowRight}');
    expect(slider).toHaveAttribute('aria-valuenow', '1');
    expect(slider.getAttribute('aria-valuetext')).toMatch(/^Frame 2 of \d+, 1:35: Clicked "Canvas field jacket"$/);
    await user.keyboard('{Shift>}{ArrowRight}{/Shift}');
    expect(slider).toHaveAttribute('aria-valuenow', '2');
    // the log seeks too: the person's take-over
    await user.click(screen.getAllByRole('button', { name: /A person took control/ })[0]!);
    expect(slider.getAttribute('aria-valuetext')).toMatch(/A person had control$/);
  });

  it('resumes a paused save from the app, after saying what it bills, and shows it waking', async () => {
    const { user, router } = renderApp(`/${ORG}/environments`);
    await user.click(await screen.findByRole('button', { name: 'Resume Checkout redesign' }, T));
    const dialog = await screen.findByRole('dialog', { name: 'Resume Checkout redesign?' });
    expect(dialog.textContent).toMatch(/It bills €0\.0480 an hour while it is awake, instead of €2\.40 a month saved\./);
    await user.click(within(dialog).getByRole('button', { name: 'Resume' }));
    await waitFor(() => expect(router.state.location.pathname).toBe(`/${ORG}/environments/k7pz`), T);
    expect(await screen.findByText('Waking its paused machine', undefined, T)).toBeInTheDocument();
  });

  it('lists the prepared machines with what each costs, and a failed build with the end of its log', async () => {
    renderApp(`/${ORG}/environments/prepared`);
    const table = await screen.findByRole('table', { name: 'Prepared machines' }, T);
    expect(within(table).getAllByText('harbor-supply/shop')).toHaveLength(2);
    expect(within(table).getByText('Build failed')).toBeInTheDocument();
    expect(within(table).getByText(/ERR_PNPM_FETCH_404/)).toBeInTheDocument();
    expect(document.body.textContent).toContain('agentos environments allow --warm 0');
  });
});
