import { screen, waitFor, within } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import type { EnvCheckpoint, EnvReplayTimeline } from '@/api/types';
import { renderApp } from '@/test/render';
import { server } from '@/test/setup';
import { checkpointRows, sinceOf } from './checkpoint-words';
import { diffCut, diffFiles, patchFiles } from './diff-words';
import { allowCommand } from './network-card';
import { openedWords, otherPortWords, scaleEnd, seconds, skipped, stepViews, withoutSkips } from './opened-words';
import { clipLength } from './proofs';
import { logItems } from './replay';
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
      bringup: { total_s: 4.1, ok: true, steps: [] },
      prepared: { used: true, warm: 'warm pool', from: { commit: 'abc', age: '2 days' }, setup_s: { build: 192.4, here: 1.4, saved: 191 } },
    });
    expect(words.lead).toBe('Opened in 4.1 s on a warm, prepared machine.');
    expect(words.detail).toBe('Setup ran when it was built 2 days ago, so here it only refreshed: 1.4 s instead of 3 min 12 s.');
    const cold = openedWords({ state: 'ready', bringup: { total_s: 123.8, ok: true, steps: [] }, prepared: { used: false, why: 'no prepared machine yet', setup: { ok: true, seconds: 51.6 }, build: { started: true } } });
    expect(cold.lead).toBe('Opened in 2 min 4 s.');
    expect(cold.detail).toBe('No prepared machine yet. Setup took 52 s. A prepared machine is being built from it, so the next open skips setup.');
    const waking = openedWords({ state: 'starting', resumed_by: 'owner', resumed: { from: 'snapshot' }, bringup: { total_s: 20, ok: null, steps: [], running: [{ step: 'VM ready', at_s: 11.2, so_far_s: 8.8 }] } });
    expect(waking.lead).toBe('Boot: 8.8 s so far, 20 s since you resumed it.');
    const woke = openedWords({ state: 'ready', bringup: { total_s: 5.9, ok: true, steps: [], restore_from: 'paused' } });
    expect(woke.lead).toBe('Woke in 5.9 s from its paused machine.');
    const failed = openedWords({ state: 'failed', bringup: { total_s: 40, ok: false, steps: [{ step: 'clone', at_s: 30, seconds: 9.5, ok: false }] } });
    expect(failed.lead).toBe('Clone the repository failed after 9.5 s.');
  });

  it('counts what a skipped step took when it last ran, and shows what is still to come while it starts', () => {
    const bringup = { total_s: 4.1, ok: true, steps: [{ step: 'VM create', at_s: 0, seconds: 0, skipped: 'warm machine', saved_s: 9.1 }, { step: 'setup', at_s: 0, seconds: 0, skipped: 'prepared machine', saved_s: 190.9 }, { step: 'app', at_s: 2.9, seconds: 1.2, ok: true }] };
    expect(skipped(bringup).seconds).toBe(200);
    expect(withoutSkips(bringup)).toBeCloseTo(204.1);
    expect(withoutSkips({ total_s: 60, ok: true, steps: [] })).toBeNull();
    const starting = stepViews({ state: 'starting', repo: 'github.com/a/b', bringup: { total_s: 12, ok: null, steps: [{ step: 'VM create', at_s: 0, seconds: 9, ok: true }], running: [{ step: 'VM ready', at_s: 9, so_far_s: 3 }] } });
    expect(starting.map((step) => `${step.step}${step.running ? ' running' : ''}${step.pending ? ' to come' : ''}`)).toEqual(['VM create', 'VM ready running', 'tunnels and git to come', 'browser to come', 'clone to come']);
  });

  it('names the port the app listens on instead, and the one line that fixes it', () => {
    expect(otherPortWords({ command: 'pnpm dev', port: 5173, listening: false, listening_instead: [3000] })).toEqual({ sentence: expect.stringMatching(/^Port 3000 listens instead/), fix: 'port = 3000' });
    expect(otherPortWords({ command: 'pnpm dev', port: 5173, listening: false, listening_instead: [3000, 9229] })).toBeNull();
    expect(otherPortWords({ command: 'pnpm dev', port: 3000, listening: true })).toBeNull();
  });
});

describe('checkpoints as a line of work', () => {
  const change = (files: number, added: number) => ({ files_added: 0, files_changed: files, files_removed: 0, lines_added: added, lines_removed: 1 });
  const cp = (id: number, extra: Partial<EnvCheckpoint> = {}): EnvCheckpoint => ({ id, at: iso(id * 10), note: `c${id}`, auto: null, repos: [{ dir: '/work/shop', head: 'abc', files: 1, diff: change(id, id * 10) }], ...extra });

  it('reads a rollback from the checkpoint openajar made before it, and puts the work it went back past on the side line', () => {
    const rows = checkpointRows([cp(1), cp(2), cp(3), cp(4), cp(5, { auto: 'before rollback to 3' }), cp(6)]);
    expect(rows.map((row) => (row.kind === 'checkpoint' ? `${row.number}${row.side ? ' side' : ''}${row.forks ? ' fork' : ''}` : row.kind))).toEqual(['now', '6', 'restore', '5 side', '4 side', '3 fork', '2', '1']);
    expect(rows[0]).toMatchObject({ kind: 'now', base: { number: 6 }, since: { files: 6, added: 60, removed: 1 } });
    expect(rows[2]).toMatchObject({ kind: 'restore', to: 3, at: iso(50) });
  });

  it('without a rollback, the working copy builds on the newest', () => {
    const rows = checkpointRows([cp(2), cp(1)]);
    expect(rows[0]).toMatchObject({ kind: 'now', base: { number: 2 } });
    expect(rows.some((row) => row.kind === 'restore')).toBe(false);
  });

  it('sums what changed since over its repositories, and says nothing when the machine did not', () => {
    expect(sinceOf(cp(1, { repos: [{ dir: '/work/a', head: null, files: 0, diff: change(2, 5) }, { dir: '/work/b', head: null, files: 0, diff: change(1, 3) }] }))).toEqual({ files: 3, added: 8, removed: 2 });
    expect(sinceOf(cp(1, { repos: [{ dir: '/work/a', head: null, files: 0, diff: null }] }))).toBeNull();
  });
});

describe('a checkpoint diff', () => {
  const patch = [
    'diff --git a/src/a.ts b/src/a.ts',
    'index 1..2 100644',
    '--- a/src/a.ts',
    '+++ b/src/a.ts',
    '@@ -1,2 +1,2 @@',
    ' keep',
    '-old',
    '+new',
    '\\ No newline at end of file',
    'diff --git a/logo.png b/logo.png',
    'Binary files a/logo.png and b/logo.png differ',
  ].join('\n');
  const diff = {
    checkpoint: 3, max_bytes: 200_000, max_files: 500,
    repos: [{ dir: '/work/shop', files_total: 4, files: [{ path: 'src/a.ts', status: 'M' as const, lines_added: 1, lines_removed: 1 }, { path: 'logo.png', status: 'M' as const, lines_added: null, lines_removed: null }, { path: 'src/b.ts', status: 'A' as const, lines_added: 9, lines_removed: 0 }], patch, patch_bytes: patch.length, truncated: true }],
  };

  it('cuts the patch by file, from the first hunk on', () => {
    expect(patchFiles(patch).get('src/a.ts')).toEqual([{ kind: 'hunk', text: '@@ -1,2 +1,2 @@' }, { kind: 'ctx', text: 'keep' }, { kind: 'del', text: 'old' }, { kind: 'add', text: 'new' }]);
    expect(patchFiles(patch).get('logo.png')).toEqual([]);
  });

  it('gives every file its lines when the patch holds them, and says what the cap left out', () => {
    expect(diffFiles(diff).map((file) => [file.repo, file.path, file.status, file.lines?.length ?? null])).toEqual([['shop', 'src/a.ts', 'M', 4], ['shop', 'logo.png', 'M', 0], ['shop', 'src/b.ts', 'A', null]]);
    expect(diffCut(diff)).toEqual({ more: 1, truncated: true });
  });
});

describe('a video proof', () => {
  it('shows its length as a player does', () => {
    expect(clipLength(42.6)).toBe('0:43');
    expect(clipLength(3909)).toBe('1:05:09');
    expect(clipLength(null)).toBeNull();
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
    expect(actionWords('click f4e8')).toBe('Clicked on the page');
  });

  it('keeps the log readable: a call its frame pictures is one row, a reload goes on the call, and changes on their own are counted', () => {
    const log = buildReplay(
      {
        ...timeline,
        timeline: [
          { kind: 'call', at: iso(4), tool: 'browser_act', summary: 'click "Pay"', ok: true },
          { kind: 'frame', id: 'a', at: iso(5), cause: 'agent', tool: 'browser_act', summary: 'click "Pay"' },
          { kind: 'call', at: iso(10), tool: 'lane_exec', summary: 'apply_patch src/cart.ts', ok: true },
          { kind: 'frame', id: 'b', at: iso(12), cause: 'change' },
          { kind: 'frame', id: 'c', at: iso(60), cause: 'change' },
          { kind: 'frame', id: 'd', at: iso(70), cause: 'change' },
          { kind: 'frame', id: 'e', at: iso(80), cause: 'change' },
        ],
      },
      [],
      iso(120),
    );
    expect(logItems(log).map((item) => [item.title, item.frame, item.last, Boolean(item.changed)])).toEqual([
      ['Clicked "Pay"', 0, 0, false],
      ['Run a command: apply_patch src/cart.ts', 1, 1, true],
      ['The page changed on its own, 3 times', 2, 4, false],
    ]);
  });
});

describe('the environment concepts on the mock', () => {
  it('shows how the offer page opened: 4.1 s on a warm, prepared machine, its app listening', async () => {
    const { user } = renderApp(`/${ORG}/environments/e7k2`);
    await user.click(await screen.findByRole('button', { name: /Opened in 4\.1 s on a warm, prepared machine/ }, T));
    expect(screen.getByText('Opened in 4.1 s on a warm, prepared machine.')).toBeInTheDocument();
    const app = screen.getByRole('region', { name: 'The app' });
    expect(within(app).getByText('Listening')).toBeInTheDocument();
    expect(within(app).getByRole('link', { name: 'Open the app on port 3000' })).toBeInTheDocument();
    expect(screen.getByText(/Watched now \(1 live view\), so it stays open/)).toBeInTheDocument();
  });

  it('folds how it opened to one line once it is up, and opens it again', async () => {
    const { user } = renderApp(`/${ORG}/environments/e7k2`);
    const fold = await screen.findByRole('button', { name: /Opened in 4\.1 s on a warm, prepared machine/ }, T);
    expect(fold).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByRole('region', { name: 'The app' })).not.toBeInTheDocument();
    await user.click(fold);
    expect(screen.getByRole('region', { name: 'The app' })).toBeInTheDocument();
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
    expect(screen.getByRole('img', { name: 'Network: limited' })).toBeInTheDocument(); // its machine is up: the level shows
    expect(within(refused).getAllByText('api.segment.io').length).toBeGreaterThan(0);
    expect(within(refused).getByText('41×')).toBeInTheDocument();
    expect(within(refused).getByText(/A secret's placeholder went here/)).toBeInTheDocument();
    expect(refused.textContent).toContain(allowCommand('api.segment.io'));
    await user.click(within(refused).getByRole('button', { name: 'Copy the command that allows fonts.googleapis.com' }));
    expect(await navigator.clipboard.readText()).toBe('openajar environments allow --host fonts.googleapis.com');
    const secrets = screen.getByRole('region', { name: 'App secrets' });
    expect(within(secrets).getByText('PARCELPOINT_API_KEY')).toBeInTheDocument();
    expect(within(secrets).queryByRole('textbox')).not.toBeInTheDocument();
  });

  it('draws the checkpoints with the rollback, and opens what changed since one', async () => {
    const { user } = renderApp(`/${ORG}/environments/c5kp`);
    const list = await screen.findByRole('list', { name: 'Checkpoints, newest first' }, T);
    expect(within(list).getByText('Went back to 3')).toBeInTheDocument();
    expect(within(list).getByText('Made by openajar')).toBeInTheDocument();
    await user.click(within(list).getByRole('button', { name: 'Show what changed since checkpoint 3' }));
    const dialog = await screen.findByRole('dialog', undefined, T);
    expect(await within(dialog).findByRole('region', { name: 'shop/src/cart/totals.ts' }, T)).toBeInTheDocument();
    expect(within(dialog).getByText(/A fixed discount that empties the cart still ships/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: /summary\.tsx/ }));
    expect(within(dialog).getByRole('region', { name: 'shop/src/cart/summary.tsx' })).toBeInTheDocument();
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

  it('keeps a resume the node refuses in its dialog, with the fix', async () => {
    const full = 'All 3 machines this node allows at once are in use (3 open). Close one, or raise the limit on the node: `openajar environments allow --max-vms 4`.';
    server.use(http.post('/api/orgs/:org/environments/saves/:id/resume', () => HttpResponse.json({ error: { code: 'environment_full', message: full } }, { status: 409 })));
    const { user } = renderApp(`/${ORG}/environments`);
    await user.click(await screen.findByRole('button', { name: 'Resume Checkout redesign' }, T));
    const dialog = await screen.findByRole('dialog', { name: 'Resume Checkout redesign?' });
    await user.click(within(dialog).getByRole('button', { name: 'Resume' }));
    expect(await within(dialog).findByRole('alert', undefined, T)).toHaveTextContent('No room for another machine now. Nothing was rented.');
    expect(within(dialog).getByText('openajar environments allow --max-vms 4')).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: 'Try again' })).toBeInTheDocument();
  });

  it('lists the prepared machines with what each costs, and a failed build with the end of its log', async () => {
    renderApp(`/${ORG}/environments/prepared`);
    const table = await screen.findByRole('table', { name: 'Prepared machines' }, T);
    expect(within(table).getAllByText('harbor-supply/shop')).toHaveLength(2);
    expect(within(table).getByText('Build failed')).toBeInTheDocument();
    expect(within(table).getByText(/ERR_PNPM_FETCH_404/)).toBeInTheDocument();
    expect(document.body.textContent).toContain('openajar environments allow --warm 0');
    expect(screen.getByText('At most a month').parentElement).toHaveTextContent('€8.71');
  });
});
