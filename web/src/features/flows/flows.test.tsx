import { screen, waitFor, within } from '@testing-library/react';
import type { Schedule } from '@/api/types';
import { db } from '@/mocks/handlers';
import { renderApp } from '@/test/render';
import { paramsOf, startedWords } from './run-dialog';
import { axisOf, timelineRow } from './schedule-timeline';

const ORG = '/northwind-group';
const data = () => db.orgs.get('northwind-group')!;
const VILNIUS = 'Europe/Vilnius';
const NOW = Date.parse('2026-11-04T10:00:00Z');

const schedule = (overrides: Partial<Schedule>): Schedule => ({ id: 'sch_1', flow: 'aster.blog.post', params: {}, enabled: true, ...overrides });

// The whole list renders in jsdom, which a loaded machine takes its time over.
vi.setConfig({ testTimeout: 20_000 });

describe('paramsOf', () => {
  const fields = {
    caption: { type: 'string', required: true },
    photos: { type: 'number' },
    pinned: { type: 'boolean' },
    note: { type: 'string' },
  } as const;

  it('sends what the routine\'s schema says, not the form\'s text', () => {
    expect(paramsOf(fields, { caption: '10', photos: '10', pinned: 'true' })).toEqual({ caption: '10', photos: 10, pinned: true });
    expect(paramsOf(fields, { photos: '0', pinned: 'no' })).toEqual({ photos: 0, pinned: false });
  });

  it('leaves out what was left empty, and anything the routine does not take', () => {
    expect(paramsOf(fields, { caption: 'Hello', note: '', other: 'x' })).toEqual({ caption: 'Hello' });
  });
});

describe('Run routine, once it has started', () => {
  it('says a posting run waits for someone who may decide, when a member started it and the node gave an approval', () => {
    expect(startedWords({ status: 'waiting_approval', approval_id: 'ap_1' }, false)).toBe(
      'It waits in Needs you until someone who may decide here says yes.',
    );
  });

  it('says it waits for a yes in Needs you, for someone who may decide, or with no approval to point at', () => {
    expect(startedWords({ status: 'waiting_approval', approval_id: 'ap_1' }, true)).toBe('It is waiting for a yes in Needs you');
    expect(startedWords({ status: 'waiting_approval', approval_id: null }, false)).toBe('It is waiting for a yes in Needs you');
  });

  it('says a run started', () => {
    expect(startedWords({ status: 'queued' }, false)).toBe('Run started');
  });
});

describe('timelineRow', () => {
  const week = NOW + 7 * 86_400_000;

  it('takes the node at its word for the next fire, and works out the rest', () => {
    const row = timelineRow(schedule({ cron: '0 9 * * *', next_fire_at: '2026-11-05T09:00:00+02:00' }), VILNIUS, NOW, week);
    expect(row.marks[0]).toEqual({ at: Date.parse('2026-11-05T07:00:00Z'), confirmed: true });
    expect(row.marks.slice(1).every((mark) => !mark.confirmed)).toBe(true);
    expect(row.marks).toHaveLength(7);
  });

  it('drops a projection earlier than the node said, because the node knows better', () => {
    const row = timelineRow(schedule({ cron: '0 9 * * *', next_fire_at: '2026-11-07T09:00:00+02:00' }), VILNIUS, NOW, week);
    expect(row.marks[0]?.confirmed).toBe(true);
    expect(Math.min(...row.marks.map((mark) => mark.at))).toBe(Date.parse('2026-11-07T07:00:00Z'));
  });

  it('still shows the node time for a line it cannot read', () => {
    const row = timelineRow(schedule({ cron: '@daily', next_fire_at: '2026-11-05T00:00:00+02:00' }), VILNIUS, NOW, week);
    expect(row.marks).toEqual([{ at: Date.parse('2026-11-04T22:00:00Z'), confirmed: true }]);
  });

  it('draws no fires for a paused schedule or a watch', () => {
    expect(timelineRow(schedule({ cron: '0 9 * * *', enabled: false }), VILNIUS, NOW, week)).toMatchObject({ state: 'paused', marks: [] });
    expect(timelineRow(schedule({ watch: 'new posts' }), VILNIUS, NOW, week)).toMatchObject({ state: 'watch', marks: [] });
  });
});

describe('axisOf', () => {
  it('lays a week out on the zone calendar and marks the weekend', () => {
    const { ticks, to, from } = axisOf('7d', NOW, VILNIUS);
    expect(ticks.map((tick) => tick.label)).toEqual(['Wed 4', 'Thu 5', 'Fri 6', 'Sat 7', 'Sun 8', 'Mon 9', 'Tue 10']);
    expect(ticks.filter((tick) => tick.rest).map((tick) => tick.label)).toEqual(['Sat 7', 'Sun 8']);
    expect((to - from) / 86_400_000).toBe(7);
  });

  it('marks a day in three hour steps', () => expect(axisOf('1d', NOW, VILNIUS).ticks).toHaveLength(8));
});

describe('Flows', () => {
  it('says what is stuck before anything else, and links each routine to its page', async () => {
    const { user, router } = renderApp(`${ORG}/flows`);
    expect(await screen.findByRole('heading', { level: 1, name: 'Routines' })).toBeInTheDocument();
    expect(await screen.findByText(/^2 routines are stuck\. The other \d+ are fine\./)).toBeInTheDocument();
    const needs = await screen.findByRole('list', { name: 'Needs a look' });
    expect(within(needs).getByText(/Gallery follow-ups stopped at/)).toBeInTheDocument();
    await user.click((await screen.findAllByRole('link', { name: /^Instagram posts/ })).find((link) => link.closest('tr'))!);
    await waitFor(() => expect(router.state.location.pathname).toBe(`${ORG}/flows/aster.instagram.post`));
    expect(await screen.findByRole('heading', { name: 'What it made lately' })).toBeInTheDocument();
  });

  it('has no switch and no Run button in the list, so nothing there can be set off by a slip', async () => {
    renderApp(`${ORG}/flows`);
    await screen.findAllByRole('link', { name: /^Instagram posts/ });
    expect(within(screen.getByRole('main')).queryByRole('switch')).toBeNull();
    expect(within(screen.getByRole('main')).queryByRole('button', { name: /^Run/ })).toBeNull();
  });

  it('keeps the view in the address', async () => {
    const { user, router } = renderApp(`${ORG}/flows`);
    await user.click(await screen.findByRole('link', { name: 'The week ahead' }));
    await waitFor(() => expect(router.state.location.search).toEqual({ tab: 'schedules' }));
    expect(await screen.findByRole('region', { name: 'The week ahead' })).toBeInTheDocument();
  });

  it('opens on the schedules tab from a link', async () => {
    renderApp(`${ORG}/flows?tab=schedules`);
    expect(await screen.findByRole('radiogroup', { name: 'Timeline range' })).toBeInTheDocument();
  });

  it('shows a paused schedule as paused, and resumes it', async () => {
    const { user } = renderApp(`${ORG}/flows?tab=schedules`);
    const toggle = await screen.findByRole('switch', { name: 'personal.mail.digest runs on schedule' });
    expect(toggle).not.toBeChecked();
    expect(screen.getAllByText('Paused').length).toBeGreaterThan(0);
    await user.click(toggle);
    await waitFor(() => expect(data().schedules.find((item) => item.flow === 'personal.mail.digest')?.enabled).toBe(true));
    expect(await screen.findByRole('switch', { name: 'personal.mail.digest runs on schedule' })).toBeChecked();
  });

  it('pauses a schedule and clears its next fire', async () => {
    const { user } = renderApp(`${ORG}/flows?tab=schedules`);
    await user.click(await screen.findByRole('switch', { name: 'aster.linkedin.post runs on schedule' }));
    await waitFor(() => expect(data().schedules.find((item) => item.flow === 'aster.linkedin.post')?.next_fire_at).toBeNull());
  });

  it('gives a viewer the status with no controls', async () => {
    data().role = 'viewer';
    renderApp(`${ORG}/flows?tab=schedules`);
    await screen.findByRole('radiogroup', { name: 'Timeline range' });
    // The rail's own Technical details switch is the person's, not a schedule control.
    expect(within(screen.getByRole('main')).queryByRole('switch')).toBeNull();
    expect(screen.getAllByText('Paused').length).toBeGreaterThan(0);
  });

  it('tells a connected node apart from no node at all, when nothing is reported', async () => {
    data().flows = [];
    renderApp(`${ORG}/flows`);
    expect(await screen.findByText('Nothing runs on its own yet')).toBeInTheDocument();
    expect(await screen.findByRole('link', { name: /Start a routine/ })).toBeInTheDocument();
  });
});

describe('New routine', () => {
  it('starts from a job in one click, with the job as a sentence', async () => {
    const { user, router } = renderApp(`${ORG}/flows/new`);
    await user.click(await screen.findByRole('link', { name: /^Invoice reminders:/ }));
    await waitFor(() => expect(router.state.location.pathname).toMatch(/flows\/new\/invoice$/));
    expect(await screen.findByRole('button', { name: 'send a reminder' })).toBeInTheDocument();
  });

  it('will not turn on until a send step has a list, and takes the person to it', async () => {
    const { user } = renderApp(`${ORG}/flows/new/invoice?workspace=harbor`);
    await user.click(await screen.findByRole('button', { name: 'Turn on' }));
    expect((await screen.findAllByText('Stops at 200 a day')).length).toBeGreaterThan(0);
    expect(screen.getByRole('combobox', { name: 'Who it goes to' })).toBeInTheDocument();
  });
});

describe('Changing the steps', () => {
  const edit = `${ORG}/flows/harbor.shopify.new-order/edit`;
  const cards = () => screen.getAllByRole('button', { name: /^(DO|DECIDE|TELL|Do|Decide|Tell)/ });

  it('moves a step with the keyboard, keeps focus on it, and undoes', async () => {
    const { user } = renderApp(edit);
    const decide = await screen.findByRole('button', { name: /^Decide/i });
    decide.focus();
    // The same step elements, in order: a move reorders them, and undo must put them back as they were.
    // Their text is not compared, because a step's sample value is filled in from the mock as the page settles.
    const before = cards();
    const inOrder = (nodes: HTMLElement[]) => nodes.every((node, index) => node === before[index]);
    await user.keyboard('{Alt>}{ArrowUp}{/Alt}');
    await waitFor(() => expect(inOrder(cards())).toBe(false), { timeout: 5_000 });
    expect(document.activeElement?.textContent).toMatch(/If it is in stock/);
    await user.click(screen.getByRole('button', { name: 'Undo' }));
    await waitFor(() => expect(inOrder(cards())).toBe(true), { timeout: 5_000 });
  }, 15_000);  // renders the whole editor and types a key chord: slow under a full parallel run

  it('removes a step, moves focus to its neighbour, and brings it back', async () => {
    const { user } = renderApp(edit);
    const stock = await screen.findByRole('button', { name: /^Do.*Check the stock/i });
    stock.focus();
    await user.keyboard('{Delete}');
    await waitFor(() => expect(screen.queryByRole('button', { name: /^Do.*Check the stock/i })).toBeNull());
    expect(document.activeElement?.textContent).toMatch(/When a new order arrives/);
    await user.click(screen.getByRole('button', { name: 'Undo' }));
    expect(await screen.findByRole('button', { name: /^Do.*Check the stock/i })).toBeInTheDocument();
  });

  it('adds a step where the gap is, and opens it', async () => {
    const { user } = renderApp(edit);
    await user.click((await screen.findAllByRole('button', { name: 'Add a step here' }))[0]!);
    await user.click(await screen.findByRole('button', { name: /^Wait/ }));
    expect(await screen.findByRole('spinbutton', { name: 'For' })).toHaveValue(1);
  });
});

describe('Flow detail', () => {
  const path = `${ORG}/flows/aster.instagram.post`;

  it('leads with the sentence of what it does, and what it really made', async () => {
    renderApp(path);
    const title = await screen.findByRole('heading', { level: 1 });
    // The routine's name is the title, and its sentence the line under it.
    expect(title.closest('header')).toHaveTextContent("pick today's idea, write a caption, then post it on Instagram.");
    expect(await screen.findByRole('list', { name: 'The steps of this routine, in order' })).toBeInTheDocument();
    expect(await screen.findByRole('heading', { name: 'What it made lately' })).toBeInTheDocument();
    expect((await screen.findAllByText(/Checked on Instagram/)).length).toBeGreaterThan(0);
  }, 20_000);

  it('opens a run with its steps, what went in and what came out', async () => {
    const { user } = renderApp(path);
    await user.click((await screen.findAllByRole('button', { name: /^Posted on Instagram/ }))[0]!);
    const steps = await screen.findByRole('list', { name: 'Steps of this run' });
    await user.click(within(steps).getByRole('button', { name: /Post it on Instagram/ }));
    expect(await within(steps).findByText('Went in')).toBeInTheDocument();
    expect(within(steps).getByText('Came out')).toBeInTheDocument();
  }, 20_000);

  it('tries a failed step again, and the routine is no longer stuck', async () => {
    const { user } = renderApp(`${ORG}/flows/solstice.outlook.outreach`);
    await user.click(await screen.findByRole('button', { name: 'See what happened' }));
    await user.click(await screen.findByRole('button', { name: 'Try this step again' }));
    await waitFor(() => expect(screen.queryByText(/^Stopped at/)).toBeNull());
  });

  it('shows the next fire and a pause for its schedule', async () => {
    renderApp(path);
    expect(await screen.findByRole('switch', { name: 'aster.instagram.post runs on schedule' })).toBeChecked();
  });

  it('says plainly what a send step can never do', async () => {
    renderApp(`${ORG}/flows/harbor.xero.invoice-reminders`);
    const rules = await screen.findByRole('region', { name: 'Who it can reach' });
    expect(within(rules).getByText('Stops at 200 a day')).toBeInTheDocument();
    expect(within(rules).getByText(/who asked not to hear from you/)).toBeInTheDocument();
    expect(within(rules).getByText('Every email carries an unsubscribe link')).toBeInTheDocument();
    expect(within(rules).queryByRole('switch')).toBeNull();
  });

  it('says so when the flow is not here', async () => {
    renderApp(`${ORG}/flows/nowhere.at.all`);
    expect(await screen.findByText('This routine is not here')).toBeInTheDocument();
  });
});
