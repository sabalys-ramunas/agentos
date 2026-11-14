import { screen, waitFor, within } from '@testing-library/react';
import { brainTasks } from '@/api/client';
import { db } from '@/mocks/handlers';
import { writeTechnical } from '@/lib/technical';
import { renderApp } from '@/test/render';
import { canResume, canStop, TASK_WORDS } from './handed-words';

const ORG = 'northwind-group';
const PAGE = `/${ORG}/settings/brains`;

async function row(text: RegExp | string) {
  const found = await screen.findByText(text);
  return found.closest('li') as HTMLElement;
}

describe('what was handed to an AI model', () => {
  it('lists each task with its state in words, and only offers what can be done to it', async () => {
    renderApp(PAGE);
    const running = await row(/Write three captions for the spring menu/);
    expect(within(running).getAllByText('Working now').length).toBeGreaterThan(0);
    expect(within(running).getByRole('button', { name: 'Stop' })).toBeInTheDocument();
    expect(within(running).queryByRole('button', { name: 'Carry on' })).toBeNull();

    const failed = await row(/Pull the week/);
    expect(within(failed).getAllByText('Did not finish').length).toBeGreaterThan(0);
    expect(within(failed).getByRole('button', { name: 'Carry on' })).toBeInTheDocument();
    expect(within(failed).queryByRole('button', { name: 'Stop' })).toBeNull();

    const done = await row(/Sort this week/);
    expect(within(done).getAllByText('Finished, not checked').length).toBeGreaterThan(0);
    expect(within(done).queryByRole('button', { name: 'Stop' })).toBeNull();
    expect(within(done).queryByRole('button', { name: 'Carry on' })).toBeNull();
  });

  it('stops a task that is still going only after a confirm, and says what stays done', async () => {
    const { user } = renderApp(PAGE);
    const running = await row(/Write three captions for the spring menu/);
    await user.click(within(running).getByRole('button', { name: 'Stop' }));
    const dialog = await screen.findByRole('dialog', { name: 'Stop this task?' });
    expect(within(dialog).getByText(/What it has already done stays done/)).toBeInTheDocument();
    await user.click(within(dialog).getByRole('button', { name: 'Stop task' }));
    await waitFor(() => expect(within(running).getAllByText('Stopped').length).toBeGreaterThan(0));
    expect(within(running).getByRole('button', { name: 'Carry on' })).toBeInTheDocument();
  });

  it('carries on from a task that stopped, and it is working again', async () => {
    const { user } = renderApp(PAGE);
    const failed = await row(/Pull the week/);
    await user.click(within(failed).getByRole('button', { name: 'Carry on' }));
    await waitFor(() => expect(within(failed).getAllByText('Working now').length).toBeGreaterThan(0));
  });

  it('shows what the model said only with Technical details on, and never to anyone else', async () => {
    writeTechnical(db.user.id, true);
    const { user } = renderApp(PAGE);
    const running = await row(/Write three captions for the spring menu/);
    await user.click(within(running).getByRole('button', { name: 'Show what it said' }));
    expect(await within(running).findByText(/Caption 1 of 3 is written\./)).toBeInTheDocument();
  });

  it('is not shown to anyone who is not the owner, and the node refuses them the same', async () => {
    db.orgs.get(ORG)!.role = 'admin';
    renderApp(PAGE);
    expect(await screen.findByRole('heading', { name: 'AI models' })).toBeInTheDocument();
    expect(screen.queryByText('Handed to an AI model')).toBeNull();
    await expect(brainTasks.list(ORG)).rejects.toMatchObject({ status: 403 });
    await expect(brainTasks.cancel(ORG, 'bt_run')).rejects.toMatchObject({ status: 403 });
  });

  it('refuses to stop a task that has already ended, and to carry on one that cannot', async () => {
    await expect(brainTasks.cancel(ORG, 'bt_done')).rejects.toMatchObject({ status: 409 });
    await expect(brainTasks.resume(ORG, 'bt_run')).rejects.toMatchObject({ status: 409 });
  });
});

describe('the words of a task', () => {
  it('offers Stop only while a task has not ended, and Carry on only for one the node can resume', () => {
    expect(canStop({ status: 'waiting_approval' })).toBe(true);
    expect(canStop({ status: 'succeeded_unverified' })).toBe(false);
    expect(canResume({ status: 'interrupted', resumable: true })).toBe(true);
    expect(canResume({ status: 'running', resumable: true })).toBe(false);
    expect(canResume({ status: 'failed', resumable: false })).toBe(false);
  });

  it('never says a finished task is done, until something has checked it', () => {
    expect(TASK_WORDS.succeeded_unverified.label).toBe('Finished, not checked');
    for (const word of Object.values(TASK_WORDS)) expect(word.label).not.toMatch(/^Done$/);
  });
});
