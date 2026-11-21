import { screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { db, resetDb } from '@/mocks/handlers';
import { renderApp } from '@/test/render';
import { server } from '@/test/setup';

const org = () => db.orgs.get('northwind-group')!;
const steppedUp = () => (db.stepUpUntil = new Date(Date.now() + 10 * 60_000).toISOString());

describe('webhooks', () => {
  it('shows the origin, never the URL, with the last delivery', async () => {
    renderApp('/northwind-group/settings/webhooks');
    const table = await screen.findByRole('table', { name: 'Your webhooks' });
    expect(within(table).getByText('https://hooks.northwind.example')).toBeInTheDocument();
    expect(within(table).getByText('Delivered')).toBeInTheDocument();
  });

  it('asks to confirm it is you before adding one, then shows the secret exactly once', async () => {
    const { user } = renderApp('/northwind-group/settings/webhooks');
    await user.click(await screen.findByRole('button', { name: 'Add webhook' }));
    const dialog = within(await screen.findByRole('dialog', { name: 'Add a webhook' }));
    await user.type(dialog.getByLabelText('Address'), 'https://receiver.example.net/openajar');
    await user.click(dialog.getByRole('button', { name: 'Add webhook' }));
    // The mock asks for step-up like the API does.
    expect(await screen.findByRole('dialog', { name: /Confirm it is you/ })).toBeInTheDocument();
    steppedUp();
  });

  it('adds one, shows its secret once, and refuses a private or plain http address in words', async () => {
    steppedUp();
    const { user } = renderApp('/northwind-group/settings/webhooks');
    await user.click(await screen.findByRole('button', { name: 'Add webhook' }));
    const dialog = within(await screen.findByRole('dialog', { name: 'Add a webhook' }));
    const address = dialog.getByLabelText('Address');
    await user.type(address, 'http://receiver.example.net/openajar');
    await user.click(dialog.getByRole('button', { name: 'Add webhook' }));
    expect(await dialog.findByRole('alert')).toHaveTextContent('Use an https address');
    await user.clear(address);
    await user.type(address, 'https://localhost/openajar');
    await user.click(dialog.getByRole('button', { name: 'Add webhook' }));
    await waitFor(() => expect(dialog.getByRole('alert')).toHaveTextContent('That address is private'));
    await user.clear(address);
    await user.type(address, 'https://receiver.example.net/openajar');
    await user.click(dialog.getByRole('button', { name: 'Add webhook' }));
    const secret = within(await screen.findByRole('dialog', { name: 'Copy the signing secret' }));
    const value = secret.getByText(/^whsec_/);
    expect(secret.getByText(/You will not see this again/)).toBeInTheDocument();
    await user.click(secret.getByRole('button', { name: 'Done' }));
    expect(value).not.toBeInTheDocument();
    expect(screen.queryByText(/^whsec_/)).not.toBeInTheDocument();
    expect(await screen.findByText('https://receiver.example.net')).toBeInTheDocument();
  });

  it('turns one off at once, and back on if the server refuses', async () => {
    steppedUp();
    server.use(http.patch('/api/orgs/:org/webhooks/:id', () => HttpResponse.json({ error: { code: 'unavailable', message: 'Down.' } }, { status: 503 })));
    const { user } = renderApp('/northwind-group/settings/webhooks');
    const toggle = await screen.findByRole('switch', { name: 'Send to https://hooks.northwind.example' });
    await user.click(toggle);
    await waitFor(() => expect(toggle).toBeChecked());
    expect(await screen.findByRole('alert')).toHaveTextContent('It is back as it was.');
  });

  it('sends a test and shows it land in the delivery log', async () => {
    steppedUp();
    const { user } = renderApp('/northwind-group/settings/webhooks');
    await user.click(await screen.findByRole('button', { name: 'Actions for https://hooks.northwind.example' }));
    await user.click(await screen.findByRole('menuitem', { name: 'Send a test' }));
    const drawer = within(await screen.findByRole('dialog', { name: 'Recent deliveries' }));
    expect(await drawer.findAllByText('Test')).not.toHaveLength(0);
    expect(drawer.getByText('Failed')).toBeInTheDocument();
    expect(drawer.getByText('The receiver answered 502.')).toBeInTheDocument();
  });

  it('rotates the secret only after saying the old one stops at once', async () => {
    steppedUp();
    const { user } = renderApp('/northwind-group/settings/webhooks');
    await user.click(await screen.findByRole('button', { name: 'Actions for https://hooks.northwind.example' }));
    await user.click(await screen.findByRole('menuitem', { name: 'Rotate secret' }));
    const confirm = within(await screen.findByRole('dialog', { name: 'Rotate the signing secret?' }));
    expect(confirm.getByText(/The old secret stops working at once/)).toBeInTheDocument();
    await user.click(confirm.getByRole('button', { name: 'Rotate secret' }));
    const secret = within(await screen.findByRole('dialog', { name: 'Copy the new signing secret' }));
    expect(secret.getByText(/^whsec_/)).toBeInTheDocument();
  });

  it('offers a viewer nothing to add', async () => {
    org().role = 'viewer';
    org().webhooks = [];
    renderApp('/northwind-group/settings/webhooks');
    expect(await screen.findByText(/Webhooks are for members who operate lanes/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Add webhook' })).not.toBeInTheDocument();
  });
});

describe('standing approvals', () => {
  it('says beside a commit limit that it counts only while the account asks first', async () => {
    renderApp('/northwind-group/settings/standing');
    const note = await screen.findByText(/This limit only counts while an account asks first\./);
    expect(note).toHaveTextContent('Instagram and LinkedIn run on their own, so what goes out there does not use this limit and is not capped by it. Turn on Ask me first to keep the limit.');
    expect(screen.getAllByText(/This limit only counts/)).toHaveLength(1);
  });

  it('drops the note once every account it covers asks first', async () => {
    const at = new Date().toISOString();
    for (const id of ['aster/instagram', 'aster/linkedin']) org().askFirst.push({ scope: 'lane', id, ask_first: true, set_by: { kind: 'user', id: 'usr_DEMO' }, set_at: at });
    renderApp('/northwind-group/settings/standing');
    expect(await screen.findByText(/The weekly roastery post/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText('The weekly roastery post, same template each time.')).toBeInTheDocument());
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(screen.queryByText(/This limit only counts/)).not.toBeInTheDocument();
  });

  it('links straight to the account when there is only one to turn on', async () => {
    resetDb();
    org().standing[0]!.lanes = ['aster/instagram'];
    renderApp('/northwind-group/settings/standing');
    const link = await screen.findByRole('link', { name: 'Turn on Ask me first' });
    expect(link).toHaveAttribute('href', '/northwind-group/accounts/aster/instagram');
  });

  it('says it on the account too, where the switch is', async () => {
    resetDb();
    renderApp('/northwind-group/lanes/aster/instagram');
    expect(await screen.findByText(/This limit only counts while an account asks first\./)).toBeInTheDocument();
  });

  it('warns when making a limit for commits, before anyone relies on it', async () => {
    const { user } = renderApp('/northwind-group/settings/standing');
    await user.click(await screen.findByRole('button', { name: 'New' }));
    const dialog = within(await screen.findByRole('dialog', { name: 'New standing approval' }));
    expect(await dialog.findByText(/It only limits what asks first\./)).toBeInTheDocument();
  });

  it('says on every one, as written, that no node applies it', async () => {
    renderApp('/northwind-group/settings/standing');
    expect(await screen.findByText('No computer applies these yet.')).toBeInTheDocument();
    expect(screen.getAllByText('No computer applies standing approvals yet. Whatever asks first will still ask each time.').length).toBeGreaterThan(0);
    expect(screen.getByText('7 of 20 used')).toBeInTheDocument();
    expect(screen.queryByText(/automatically/i)).not.toBeInTheDocument();
  });

  it('keeps the history one switch away, in the address', async () => {
    const { user, router } = renderApp('/northwind-group/settings/standing');
    await screen.findByText('7 of 20 used');
    await user.click(screen.getByRole('radio', { name: 'All' }));
    await waitFor(() => expect(router.state.location.search).toEqual({ show: 'all' }));
    expect(await screen.findByText('Used up')).toBeInTheDocument();
    expect(screen.getByText('Expired')).toBeInTheDocument();
  });

  it('creates one with every limit, and never offers payments or irreversible actions', async () => {
    steppedUp();
    const { user } = renderApp('/northwind-group/settings/standing');
    await user.click(await screen.findByRole('button', { name: 'New' }));
    const dialog = within(await screen.findByRole('dialog', { name: 'New standing approval' }));
    expect(await dialog.findByText('No computer applies standing approvals yet. Whatever asks first will still ask each time.')).toBeInTheDocument();
    await user.click(dialog.getByRole('combobox', { name: 'Covers' }));
    const options = (await screen.findAllByRole('option')).map((item) => item.textContent);
    expect(options).not.toContain('Payment');
    expect(options.join(' ')).not.toMatch(/Irreversible|Base image/);
    await user.click(screen.getByRole('option', { name: 'Run command' }));
    await user.clear(dialog.getByLabelText('Times in total'));
    await user.type(dialog.getByLabelText('Times in total'), '5');
    expect(dialog.getByText(/Up to 5 times/)).toBeInTheDocument();
    await user.click(dialog.getByRole('button', { name: 'Create standing approval' }));
    await waitFor(() => expect(org().standing[0]).toMatchObject({ effect: 'exec', count: 5, lanes: null }));
  });

  it('revokes at once, and brings it back if the server refuses', async () => {
    server.use(http.delete('/api/orgs/:org/standing-approvals/:id', () => HttpResponse.json({ error: { code: 'unavailable', message: 'Down.' } }, { status: 503 })));
    const { user } = renderApp('/northwind-group/settings/standing');
    await user.click(await screen.findByRole('button', { name: 'Revoke' }));
    await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Revoke' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('still active');
    expect(screen.getByText('7 of 20 used')).toBeInTheDocument();
  });

  it('tells a lane on a node that ignores them that it still asks every time', async () => {
    renderApp('/northwind-group/lanes/aster/instagram');
    const section = within(await screen.findByRole('region', { name: 'Always allowed on this account' }));
    expect(section.getByText(/does not apply these rules yet, so whatever asks first here still asks a person each time/)).toBeInTheDocument();
  });
});

describe('retention and export', () => {
  it('shows the plan window honestly', async () => {
    renderApp('/northwind-group/settings/retention');
    expect(await screen.findByText('History is kept for 30 days on your Team plan.')).toBeInTheDocument();
    expect(screen.getByText(/The oldest history kept is from/)).toBeInTheDocument();
  });

  it('says how long a downgrade keeps older activity', async () => {
    renderApp('/juniper-labs/settings/retention');
    expect(await screen.findByText('History is kept for 7 days on your Free plan.')).toBeInTheDocument();
    expect(screen.getByText(/After your plan change, history stored before it is kept until/)).toBeInTheDocument();
    expect(screen.getByText(/Only the owner can export history/)).toBeInTheDocument();
  });

  it('downloads the export through step-up, with formulas made plain text', async () => {
    steppedUp();
    let saved: Blob | null = null;
    const create = vi.fn((blob: Blob) => {
      saved = blob;
      return 'blob:mock';
    });
    Object.assign(URL, { createObjectURL: create, revokeObjectURL: () => undefined });
    org().activity[0]!.summary = '=HYPERLINK("https://evil.example","x")';
    const { user } = renderApp('/northwind-group/settings/retention');
    await user.click(await screen.findByRole('button', { name: 'Export' }));
    await waitFor(() => expect(create).toHaveBeenCalled());
    const text = await (saved as unknown as Blob).text();
    expect(text.split('\n')[0]).toBe('id,at,kind,tool,lane,workspace,principal,effect,status,summary,run_id');
    expect(text).toContain(`"'=HYPERLINK(""https://evil.example"",""x"")"`);
  });
});
