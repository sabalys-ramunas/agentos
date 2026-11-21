import { screen, waitFor, within } from '@testing-library/react';
import { db } from '@/mocks/handlers';
import { renderApp } from '@/test/render';
import { menuFor, NAV, placeOf } from './nav';

/** A rail link's words, without its count or its keys. */
const linkWords = (link: HTMLElement) => link.querySelector('[data-rail-label]')?.textContent ?? link.textContent ?? '';

describe('the menu', () => {
  it('is the structure the owner approved, in five groups', () => {
    expect(menuFor('owner').sections.map((section) => [section.label, section.items.map((item) => item.label)])).toEqual([
      ['Work', ['Home', 'Environments', 'Runs', 'Checks']],
      ['Where it runs', ['Machines']],
      ['Who it is for', ['Brand', 'Accounts']],
      ['Automations', ['Routines', 'Email', 'Site APIs']],
      ['You', ['Needs you', 'History', 'Settings']],
    ]);
  });

  it('gives every place its own key after g', () => {
    const keys = NAV.map((item) => item.key);
    expect(new Set(keys).size).toBe(keys.length);
    // Needs you acts on these keys alone. The one after g is swallowed, but none should look like one.
    for (const key of ['j', 'k', 'd', 'x', 't']) expect(keys).not.toContain(key);
  });

  it('puts five places at the thumb and the rest under More', () => {
    const menu = menuFor('owner');
    expect(menu.tabs.map((item) => item.label)).toEqual(['Home', 'Environments', 'Runs', 'Checks', 'Needs you']);
    expect(menu.more.map((item) => item.label)).toEqual(['Machines', 'Brand', 'Accounts', 'Routines', 'Email', 'Site APIs', 'History', 'Settings']);
  });

  it('knows which place a page belongs to', () => {
    expect(placeOf('/acme')?.label).toBe('Home');
    expect(placeOf('/acme/runs/job-1')?.label).toBe('Runs');
    expect(placeOf('/acme/environments/e7k2')?.label).toBe('Environments');
    expect(placeOf('/acme/nodes/home-mini')?.label).toBe('Machines');
    expect(placeOf('/acme/lanes/aster/instagram')?.label).toBe('Accounts');
    expect(placeOf('/acme/review/a1')?.label).toBe('Needs you');
    expect(placeOf('/acme/design')).toBeNull();
  });
});

describe('the menu by role', () => {
  it('gives an approver Needs you, History and Settings, and nothing else', () => {
    const menu = menuFor('approver');
    expect(menu.all.map((item) => item.label)).toEqual(['Needs you', 'History', 'Settings']);
    expect(menu.tabs).toHaveLength(3);
    expect(menu.more).toEqual([]);
  });

  it('opens Needs you for an approver who goes Home, and shows only their three places', async () => {
    db.orgs.get('northwind-group')!.role = 'approver';
    const { router } = renderApp('/northwind-group');
    await waitFor(() => expect(router.state.location.pathname).toBe('/northwind-group/needs-you'));
    const [rail] = await screen.findAllByRole('navigation', { name: 'Main' });
    await waitFor(() => expect(within(rail!).getAllByRole('link').map(linkWords)).toEqual(['Needs you', 'History', 'Settings']));
    // No status line: an approver has no machines to watch.
    expect(screen.queryByRole('button', { name: /^System:/ })).not.toBeInTheDocument();
  });
});

describe('the rail and the tab bar', () => {
  it('opens every place from the rail', async () => {
    const { user, router } = renderApp('/northwind-group');
    const [rail] = await screen.findAllByRole('navigation', { name: 'Main' });
    const expected: Record<string, string> = {
      Home: '/northwind-group',
      Environments: '/northwind-group/environments',
      Runs: '/northwind-group/runs',
      Checks: '/northwind-group/checks',
      Machines: '/northwind-group/nodes',
      Brand: '/northwind-group/brand',
      Accounts: '/northwind-group/accounts',
      Routines: '/northwind-group/flows',
      Email: '/northwind-group/email',
      'Site APIs': '/northwind-group/site-apis',
      'Needs you': '/northwind-group/needs-you',
      History: '/northwind-group/activity',
      Settings: '/northwind-group/settings',
    };
    for (const [label, path] of Object.entries(expected)) {
      const link = within(rail!).getAllByRole('link').find((item) => linkWords(item) === label)!;
      await user.click(link);
      await waitFor(() => expect(router.state.location.pathname).toBe(path));
      expect(link).toHaveAttribute('data-status', 'active');
    }
  });

  it('shows each place its g key, and the key goes there', async () => {
    const { user, router } = renderApp('/northwind-group');
    const [rail] = await screen.findAllByRole('navigation', { name: 'Main' });
    expect(within(rail!).getAllByRole('link').map((link) => link.getAttribute('aria-keyshortcuts'))).toEqual(NAV.map((item) => `G ${item.key.toUpperCase()}`));
    await user.keyboard('gm');
    await waitFor(() => expect(router.state.location.pathname).toBe('/northwind-group/nodes'));
    await user.keyboard('gr');
    await waitFor(() => expect(router.state.location.pathname).toBe('/northwind-group/runs'));
    expect(await screen.findByRole('heading', { level: 1, name: 'Runs' })).toBeInTheDocument();
  });

  it('says where you are in the top bar, with the place as a link back', async () => {
    renderApp('/northwind-group/runs/job-7f3a2c91');
    const crumbs = await screen.findByRole('navigation', { name: 'Breadcrumb' });
    expect(within(crumbs).getByRole('link', { name: 'Runs' })).toHaveAttribute('href', '/northwind-group/runs');
    expect(within(crumbs).getByText('job-7f3a2c91')).toHaveAttribute('aria-current', 'page');
  });

  it('opens More on the phone tab bar with the places that do not fit', async () => {
    const { user } = renderApp('/northwind-group');
    const bars = await screen.findAllByRole('navigation', { name: 'Main' });
    const tabs = within(bars[1]!);
    expect(tabs.getAllByRole('link').map((item) => item.textContent?.replace(/, \d+ waiting$/, ''))).toEqual(['Home', 'Environments', 'Runs', 'Checks', 'Needs you']);
    await user.click(tabs.getByRole('button', { name: /^More/ }));
    expect((await screen.findAllByRole('menuitem')).map((item) => item.textContent)).toEqual(['Machines', 'Brand', 'Accounts', 'Routines', 'Email', 'Site APIs', 'History', 'Settings']);
  });

  it('offers the six most used places in the command bar, and every place with its keys once you type', async () => {
    const { user } = renderApp('/northwind-group');
    await screen.findAllByRole('navigation', { name: 'Main' });
    await user.click(screen.getAllByRole('button', { name: 'Search or run a command' })[0]!);
    const dialog = await screen.findByRole('dialog', { name: 'Search or run a command' });
    const first = ['h', 'v', 'n', 'r', 'c', 'm'];
    for (const item of NAV) {
      const option = within(dialog).queryByRole('option', { name: new RegExp(`^${item.label}G${item.key.toUpperCase()}$`, 'i') });
      if (first.includes(item.key)) expect(option).toBeInTheDocument();
      else expect(option).toBeNull();
    }
    const input = within(dialog).getByRole('combobox');
    for (const item of NAV.filter((place) => !first.includes(place.key))) {
      await user.clear(input);
      await user.type(input, item.label);
      expect(await within(dialog).findByRole('option', { name: new RegExp(`^${item.label}G${item.key.toUpperCase()}$`, 'i') })).toBeInTheDocument();
    }
    await user.clear(input);
    await user.type(input, 'cloud');
    expect(await within(dialog).findByRole('option', { name: /^Cloud server/ })).toBeInTheDocument();
  }, 15_000);
});
