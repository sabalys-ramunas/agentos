import { screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { db } from '@/mocks/handlers';
import { server } from '@/test/setup';
import { renderApp } from '@/test/render';

describe('settings', () => {
  it('names the organization fields in plain words, and keeps the id for Technical details', async () => {
    renderApp('/northwind-group/settings/general');
    expect(await screen.findByText('Every link you share from here starts with this.')).toBeInTheDocument();
    expect(screen.queryByText(/^Org (name|address|id)$/)).toBeNull();
    expect(screen.queryByText('Quote this when you contact support.')).toBeNull();
  });

  it('shows a new grant token exactly once', async () => {
    const { user } = renderApp('/northwind-group/settings/grants');
    await user.click(await screen.findByRole('button', { name: 'New grant' }));
    await user.type(await screen.findByLabelText('Subject'), 'my agent');
    await user.click(screen.getByRole('button', { name: 'Create grant' }));
    const token = await screen.findByText(/^agt_/);
    expect(screen.getByText(/only time it is shown/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Done' }));
    expect(await screen.findByText('my-agent')).toBeInTheDocument();
    expect(token).not.toBeInTheDocument();
    expect(screen.queryByText(/^agt_/)).not.toBeInTheDocument();
  });

  it('shows vault names and offers no way to read a secret', async () => {
    renderApp('/northwind-group/settings/vault');
    const table = await screen.findByRole('table');
    expect(within(table).getByText('aster-instagram')).toBeInTheDocument();
    expect(screen.getByText(/Names only/)).toBeInTheDocument();
    // The only control in a row removes the login. Nothing reads or copies a secret.
    const controls = within(table).queryAllByRole('button');
    expect(controls.every((item) => /^Remove /.test(item.getAttribute('aria-label') ?? ''))).toBe(true);
    expect(document.querySelector('input[type="password"]')).toBeNull();
  });

  it('refuses to demote the last owner', async () => {
    const org = db.orgs.get('northwind-group')!;
    const owner = org.members.find((item) => item.role === 'owner')!;
    const response = await fetch(`/api/orgs/northwind-group/members/${owner.id}`, {
      method: 'PATCH',
      body: JSON.stringify({ role: 'viewer' }),
    });
    expect(response.status).toBe(409);
    expect(owner.role).toBe('owner');
  });

  it("shows a computer's invite link once, right after it is made", async () => {
    server.use(
      http.post('/api/orgs/:org/invites', async ({ request }) => {
        const body = (await request.json()) as { email: string; role: string };
        return HttpResponse.json({
          id: 'inv-link',
          email: body.email,
          role: body.role,
          workspaces: null,
          invited_by: 'owner',
          created_at: new Date().toISOString(),
          expires_at: new Date(Date.now() + 86_400_000).toISOString(),
          state: 'pending',
          token: 'code-shown-once',
        });
      }),
    );
    const { user } = renderApp('/northwind-group/settings/invites');
    await user.type(await screen.findByLabelText('Email'), 'ana@example.com');
    await user.click(screen.getByRole('button', { name: 'Send invite' }));
    const link = await screen.findByRole('textbox', { name: 'Invite link' });
    expect(link).toHaveValue(`${window.location.origin}/invite/code-shown-once`);
    expect(screen.getByText(/is not shown again/)).toBeInTheDocument();
  });

  it('marks an expired invite honestly', async () => {
    renderApp('/northwind-group/settings/invites');
    expect((await screen.findAllByText('Expired')).length).toBeGreaterThan(0);
    expect(screen.getByText('Pending')).toBeInTheDocument();
  });

  it('opens the billing portal only at an https address (security review 19)', async () => {
    server.use(http.post('/api/orgs/:org/billing/portal', () => HttpResponse.json({ url: 'javascript:alert(document.cookie)' })));
    const { user } = renderApp('/northwind-group/settings/billing');
    await user.click(await screen.findByRole('button', { name: 'Manage billing' }));
    expect(await screen.findByText(/did not look right, so it was not opened/)).toBeInTheDocument();
    expect(location.protocol).not.toBe('javascript:');
  });
});
