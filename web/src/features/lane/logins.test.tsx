import { screen, waitFor, within } from '@testing-library/react';
import { db } from '@/mocks/handlers';
import { renderApp } from '@/test/render';

const org = () => db.orgs.get('northwind-group')!;
const ready = () => org().lanes.filter((lane) => lane.kind === 'browser' && lane.readiness === 'ready').length;

describe('login queue', () => {
  it('shows progress and starts with what needs a look', async () => {
    renderApp('/northwind-group/logins');
    expect(await screen.findByText('accounts ready', { exact: false })).toBeInTheDocument();
    const heading = await screen.findByRole('heading', { level: 2 });
    expect(heading).toHaveTextContent(/Reddit|LinkedIn/);
  });

  it('marks an account ready and moves to the next, by key', async () => {
    const { user } = renderApp('/northwind-group/logins');
    const first = (await screen.findByRole('heading', { level: 2 })).textContent;
    const before = ready();
    await user.keyboard('r');
    await waitFor(() => expect(ready()).toBe(before + 1));
    await waitFor(() => expect(screen.getByRole('heading', { level: 2 }).textContent).not.toBe(first));
  });

  it('skips without changing anything', async () => {
    const { user } = renderApp('/northwind-group/logins');
    const first = (await screen.findByRole('heading', { level: 2 })).textContent;
    const before = ready();
    await user.click(screen.getByRole('button', { name: /Skip for now/ }));
    await waitFor(() => expect(screen.getByRole('heading', { level: 2 }).textContent).not.toBe(first));
    expect(ready()).toBe(before);
  });

  it('takes a note for an account that is stuck', async () => {
    const { user } = renderApp('/northwind-group/logins');
    await screen.findByRole('heading', { level: 2 });
    await user.click(screen.getByRole('button', { name: 'Something is wrong' }));
    await user.type(screen.getByLabelText('What is wrong'), 'Phone code asked');
    await user.click(screen.getByRole('button', { name: 'Save and move on' }));
    await waitFor(() => expect(org().lanes.some((lane) => lane.readiness_note === 'Phone code asked')).toBe(true));
  });

  it('never has a password field', async () => {
    renderApp('/northwind-group/logins');
    await screen.findByRole('heading', { level: 2 });
    expect(document.querySelector('input[type="password"]')).toBeNull();
    expect(screen.getByText(/openajar never sees it/)).toBeInTheDocument();
  });

  it('gives a viewer the queue without the controls', async () => {
    org().role = 'viewer';
    renderApp('/northwind-group/logins');
    expect(await screen.findByText('Your role can see this queue but not work through it.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Logged in, next/ })).not.toBeInTheDocument();
  });

  it('says when every account is ready', async () => {
    for (const lane of org().lanes) lane.readiness = 'ready';
    renderApp('/northwind-group/logins');
    expect(await within(await screen.findByRole('main')).findByText(/accounts are ready/)).toBeInTheDocument();
  });
});
