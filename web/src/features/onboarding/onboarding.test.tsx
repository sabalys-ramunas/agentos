import { screen, waitFor, within } from '@testing-library/react';
import type { Approval } from '@/api/types';
import { db } from '@/mocks/handlers';
import { renderApp } from '@/test/render';
import { workOutItems } from './setup';
import { ceilingOf, setAsk } from './step-accounts';

describe('first run: the first job', () => {
  it('starts with purpose: Continue waits for a job and a name, then makes the organization and the business silently', async () => {
    const { user, router } = renderApp('/onboarding');
    expect(await screen.findByRole('heading', { name: 'What should your AI do first?' })).toBeInTheDocument();
    const go = screen.getByRole('button', { name: 'Continue' });
    expect(go).toBeDisabled();
    await user.type(screen.getByLabelText('Business name'), 'Kiln Bakery');
    expect(go).toBeDisabled();
    await user.click(screen.getByRole('radio', { name: /Collect invoices and receipts/ }));
    await user.click(go);
    await waitFor(() => expect(router.state.location.pathname).toBe('/onboarding/kiln-bakery/computer'));
    const org = db.orgs.get('kiln-bakery');
    expect(org?.org).toMatchObject({ name: 'Kiln Bakery', goal: 'invoices' });
    expect(org?.workspaces.map((item) => item.name)).toEqual(['Kiln Bakery']);
    // No address field anywhere: the cloud picks one, and Settings can change it.
    expect(screen.queryByLabelText(/Address/)).not.toBeInTheDocument();
  });

  it('keeps "Just me" as Personal', async () => {
    const { user, router } = renderApp('/onboarding');
    await user.click(await screen.findByRole('radio', { name: 'Just me' }));
    await user.click(screen.getByRole('radio', { name: /Handle email/ }));
    await user.click(screen.getByRole('button', { name: 'Continue' }));
    await waitFor(() => expect(router.state.location.pathname).toMatch(/^\/onboarding\/personal[\w-]*\/computer$/));
  });

  it('offers six jobs, and only one of them is about posting', async () => {
    renderApp('/onboarding');
    const jobs = await screen.findAllByRole('radio', { name: /./ });
    const titles = jobs.map((item) => item.textContent ?? '').filter((text) => /"|own words/.test(text));
    expect(titles).toHaveLength(6);
    expect(titles.filter((text) => /post/i.test(text))).toHaveLength(1);
  });
});

describe('first run: the steps', { timeout: 20_000 }, () => {
  it('sends an old step name to its new step', async () => {
    const { router } = renderApp('/onboarding/northwind-group/node');
    await waitFor(() => expect(router.state.location.pathname).toBe('/onboarding/northwind-group/computer'));
    expect(await screen.findByRole('heading', { name: 'Where should it work?' })).toBeInTheDocument();
  });

  it('marks the Mac app and the hosted computer as coming soon, and keeps the command behind "I\'m technical"', async () => {
    const { user } = renderApp('/onboarding/northwind-group/computer');
    await screen.findByRole('heading', { name: 'Where should it work?' });
    expect(screen.getAllByText('Coming soon')).toHaveLength(2);
    expect(screen.queryByRole('button', { name: 'Copy install command' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /I'm technical/ }));
    expect(await screen.findByRole('button', { name: 'Copy install command' }, { timeout: 10_000 })).toBeInTheDocument();
  }, 20_000);

  it('connects Claude Code with the ceiling the switches said', async () => {
    const { user } = renderApp('/onboarding/northwind-group/ai');
    await screen.findByRole('heading', { name: 'Connect your AI' });
    // The apps are honest about today.
    await user.click(screen.getByRole('radio', { name: /^Claude\s*Coming soon/ }));
    expect(screen.getByText(/one link and an Allow button/)).toBeInTheDocument();
    await user.click(screen.getByRole('radio', { name: /Claude Code/ }));
    const before = db.orgs.get('northwind-group')!.grants.length;
    const make = screen.getByRole('button', { name: 'Make a connection for Claude Code' });
    await waitFor(() => expect(make).toBeEnabled());
    await user.click(make);
    expect(await screen.findByRole('button', { name: 'Copy command' })).toBeInTheDocument();
    expect(screen.getByText(/Connection made for Claude Code/)).toBeInTheDocument();
    const grants = db.orgs.get('northwind-group')!.grants;
    expect(grants).toHaveLength(before + 1);
    expect(grants[0]).toMatchObject({ subject: 'claude-code', ceiling: 'commit', workspaces: ['aster'] });
  });

  it('shows the first request that arrives on the try step, and decides it for real', async () => {
    const data = db.orgs.get('northwind-group')!;
    const template = data.approvals.find((item) => item.lane === 'aster/instagram') ?? data.approvals[0]!;
    const fresh: Approval = { ...template, id: 'ap_first_try', created_at: new Date().toISOString() };
    data.approvals = [fresh];
    const { user } = renderApp('/onboarding/northwind-group/try');
    expect(await screen.findByRole('heading', { name: 'Give it its first job' })).toBeInTheDocument();
    expect(await screen.findByText(/Using agentos, write an Instagram post for Aster Coffee/)).toBeInTheDocument();
    const [card] = await screen.findAllByRole('article', { name: /Instagram/ });
    await user.click(within(card!).getAllByRole('button').at(-1)!);
    expect(await screen.findByText(/Approved\. It goes ahead now\./)).toBeInTheDocument();
    expect(data.history.some((item) => item.id === 'ap_first_try')).toBe(true);
  });

  it('invites someone who can approve one business, in a sentence instead of a role name', async () => {
    const { user } = renderApp('/onboarding/northwind-group/people');
    await screen.findByRole('heading', { name: 'Stay in the loop, from anywhere' });
    await user.type(screen.getByLabelText('Their email'), 'priya@example.com');
    expect(await screen.findByRole('radio', { name: /Can approve for Aster Coffee/ })).toHaveAttribute('aria-checked', 'true');
    await user.click(screen.getByRole('button', { name: 'Send invite' }));
    expect(await screen.findByText('Invite sent to priya@example.com')).toBeInTheDocument();
    expect(db.orgs.get('northwind-group')!.invites[0]).toMatchObject({ email: 'priya@example.com', role: 'approver', workspaces: ['aster'] });
  });
});

describe('ask me first', () => {
  it('maps the switches to the grant ceiling, as a ladder', () => {
    expect(ceilingOf({ post: false, pay: false, delete: false })).toBe('draft');
    expect(ceilingOf({ post: true, pay: false, delete: false })).toBe('commit');
    expect(ceilingOf({ post: true, pay: true, delete: false })).toBe('pay');
    expect(ceilingOf({ post: true, pay: true, delete: true })).toBe('irreversible');
    expect(setAsk({ post: false, pay: false, delete: false }, 'delete', true)).toEqual({ post: true, pay: true, delete: true });
    expect(setAsk({ post: true, pay: true, delete: true }, 'post', false)).toEqual({ post: false, pay: false, delete: false });
    expect(setAsk({ post: true, pay: true, delete: true }, 'pay', false)).toEqual({ post: true, pay: false, delete: false });
  });
});

describe('getting started, worked out', () => {
  it('is done only when it is true, in the cloud\'s seven lines', () => {
    const lanes = [
      { kind: 'browser', name: 'instagram', logged_in_hint: 'needs_login' },
      { kind: 'browser', name: 'facebook', logged_in_hint: 'ok' },
    ];
    const items = workOutItems({
      goal: 'post',
      workspaces: [{ lanes } as never],
      nodes: [{ id: 'home-mini', state: 'up' }],
      grants: [{ revoked: false }],
      decided: 0,
      phones: 1,
      invited: 0,
      people: 1,
    });
    expect(items.map((item) => [item.key, item.done])).toEqual([
      ['goal', true],
      ['computer', true],
      ['account', true],
      ['ai', true],
      ['approval', false],
      ['phone', true],
      ['invite', false],
    ]);
    expect(items.find((item) => item.key === 'computer')?.hint).toBe('Mac mini (home) is connected.');
    expect(workOutItems({ goal: null, workspaces: [], nodes: [], grants: [], decided: 0, phones: 0, invited: 0, people: 1 }).every((item) => !item.done)).toBe(true);
  });
});
