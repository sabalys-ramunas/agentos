import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { db } from '@/mocks/handlers';
import { renderApp } from '@/test/render';

const data = () => db.orgs.get('northwind-group')!;
const open = (status: string) => data().runs.find((run) => run.status === status && run.proof !== undefined)!;

/** A run read on its own, as from a routine's page or an old link. */
async function show(id: string) {
  const view = renderApp(`/northwind-group/activity?run=${id}`);
  const drawer = await screen.findByRole('dialog', { name: 'What happened' });
  await within(drawer).findByRole('article');
  return { ...view, drawer };
}

describe('a run as a story', () => {
  it('says who did what, where, and how we know, with the steps in words', async () => {
    const { drawer } = await show(open('succeeded_verified').id);
    expect(within(within(drawer).getByRole('article')).getByRole('heading', { level: 2 }).textContent).toMatch(/^(Claude Code|Claude|Codex|The weekly posts routine) \w+.* on .+'s /);
    expect(within(drawer).getByText('It is live, and we checked.')).toBeInTheDocument();
    const steps = within(drawer).getByRole('list', { name: 'What the agent did' });
    expect(within(steps).getByText(/^Opened \w+/)).toBeInTheDocument();
    expect(within(steps).getByText('Looked it up from outside and found it')).toBeInTheDocument();
    expect(within(steps).queryByText(/Verified|lane\.open/)).toBeNull();
    expect(within(drawer).getByRole('img', { name: /showed afterwards/ })).toBeInTheDocument();
  });

  it('keeps the machinery folded under Technical details: the run id, the waterfall and the tool names', async () => {
    const run = open('succeeded_verified');
    const { drawer, user } = await show(run.id);
    expect(within(drawer).queryByRole('list', { name: 'Steps, in order' })).toBeNull();
    await user.click(within(drawer).getByText('Technical details'));
    const steps = await within(drawer).findByRole('list', { name: 'Steps, in order' });
    expect(within(steps).getAllByRole('button')).toHaveLength(run.steps!.length);
    await user.click(within(steps).getAllByRole('button')[0]!);
    expect(await within(steps).findByText('lane.open')).toBeInTheDocument();
    expect(within(drawer).getAllByText(run.id).length).toBeGreaterThan(0);
    // Clicks in a drawer over a full History page take their time.
  }, 15_000);

  it('says what did not happen when it failed, and offers the account', async () => {
    const run = data().runs.find((item) => item.status === 'failed')!;
    const { drawer } = await show(run.id);
    expect(within(drawer).getByText('It did not work.')).toBeInTheDocument();
    expect(within(drawer).getByText(/^Nothing was (posted|published)\./)).toBeInTheDocument();
    expect(within(drawer).getByRole('link', { name: /the account/ })).toBeInTheDocument();
  });

  it('says so when the computer could not send the picture', async () => {
    const { drawer } = await show(open('succeeded_verified').id);
    fireEvent.error(within(drawer).getByRole('img', { name: /showed afterwards/ }));
    expect(await within(drawer).findByText(/could not send it just now/)).toBeInTheDocument();
  });

  it('stops a live run and records it as stopped, never as done', async () => {
    const run = data().runs.find((item) => item.status === 'running')!;
    const { drawer, user } = await show(run.id);
    expect(within(drawer).getByText('So far')).toBeInTheDocument();
    await user.click(await within(drawer).findByRole('button', { name: 'Stop it' }));
    const title = await screen.findByText('Stop this now?');
    const confirm = title.closest<HTMLElement>('[role="alertdialog"], [role="dialog"]')!;
    await user.click(within(confirm).getByRole('button', { name: 'Stop it' }));
    await waitFor(() => expect(data().runs.find((item) => item.id === run.id)?.status).toBe('cancelled'));
    expect(await within(drawer).findByText('It did not happen.')).toBeInTheDocument();
    expect(within(drawer).queryByRole('button', { name: 'Stop it' })).toBeNull();
  });

  it('offers no stop to someone who cannot operate accounts', async () => {
    data().role = 'viewer';
    const { drawer } = await show(data().runs.find((item) => item.status === 'running')!.id);
    expect(within(drawer).getByText('Working on it now.')).toBeInTheDocument();
    expect(within(drawer).queryByRole('button', { name: 'Stop it' })).toBeNull();
  });
});
