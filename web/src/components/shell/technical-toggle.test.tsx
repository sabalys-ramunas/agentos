import { screen, waitFor, within } from '@testing-library/react';
import { db } from '@/mocks/handlers';
import { renderApp } from '@/test/render';
import { readTechnical } from '@/lib/technical';

describe('the Technical details switch', () => {
  beforeEach(() => localStorage.clear());

  it('sits at the foot of the rail, off by default, and brings back ids when on', async () => {
    const { user } = renderApp('/northwind-group');
    const toggle = await screen.findByRole('switch', { name: 'Technical details' });
    expect(toggle).not.toBeChecked();
    expect(toggle).toHaveAccessibleDescription('Shows ids, logs, costs per run and the terminal.');

    await user.click((await screen.findAllByRole('button', { name: /^System: / }))[0]!);
    expect(within(await screen.findByRole('dialog')).queryByText('home-mini')).not.toBeInTheDocument();
    await user.keyboard('{Escape}');

    await user.click(toggle);
    expect(toggle).toBeChecked();
    expect(readTechnical(db.user.id)).toBe(true);
    await user.click(screen.getAllByRole('button', { name: /^System: / })[0]!);
    await waitFor(() => expect(within(screen.getByRole('dialog')).getByText('home-mini')).toBeInTheDocument());
  });

  it('is in Profile too, for a phone that has no rail', async () => {
    renderApp('/northwind-group/settings/account');
    const switches = await screen.findAllByRole('switch', { name: 'Technical details' });
    expect(switches.length).toBeGreaterThanOrEqual(1);
  });
});
