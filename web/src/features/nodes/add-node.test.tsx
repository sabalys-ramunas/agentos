import { screen, waitFor, within } from '@testing-library/react';
import { renderApp } from '@/test/render';

describe('Add a machine', () => {
  it('keeps the join token out of the install command, and shows it on its own (security review 10)', async () => {
    const { user } = renderApp('/northwind-group/nodes?add=true');
    // Named for the everyday word: the button and this dialog say "a machine", never "a node".
    const dialog = within(await screen.findByRole('dialog', { name: 'Add a machine' }, { timeout: 15_000 }));
    const get = dialog.getByRole('button', { name: 'Get install command' });
    await waitFor(() => expect(get).toBeEnabled(), { timeout: 10_000 });
    await user.click(get);
    expect(await dialog.findByText('Paste this when the installer asks for it', {}, { timeout: 10_000 })).toBeInTheDocument();
    for (const command of dialog.getAllByText(/install\.sh/)) expect(command.textContent).not.toMatch(/--join|jt_/);
    expect(dialog.getByRole('button', { name: 'Copy join token' })).toBeInTheDocument();
  }, 20_000);
});
