import { screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { db } from '@/mocks/handlers';
import { renderApp } from '@/test/render';
import { server } from '@/test/setup';
import { menuFor, NAV, placeOf, tabOf } from './nav';

/** A rail link's words, without its count or its keys. */
const linkWords = (link: HTMLElement) => link.querySelector('[data-rail-label]')?.textContent ?? link.textContent ?? '';

/** An organisation that has never used a developer place: no run, no check, no Site API. */
function nothingForDevelopers() {
  server.use(
    http.get('/api/orgs/:org/jobs', () => HttpResponse.json({ available: true, jobs: [] })),
    http.get('/api/orgs/:org/studio/runs', () => HttpResponse.json({ available: true, runs: [] })),
    http.get('/api/orgs/:org/site-apis', () => HttpResponse.json([])),
  );
}

describe('the menu', () => {
  it('is the simpler navigation the owner chose: four first, Your business, For developers, then Settings', () => {
    expect(menuFor('owner').sections.map((section) => [section.label, section.items.map((item) => item.label)])).toEqual([
      ['', ['Home', 'Needs you', 'Computers', 'What happened']],
      ['Your business', ['Accounts', 'Jobs that repeat', 'Brand']],
      ['For developers', ['Runs and checks', 'Site APIs']],
      ['', ['Settings']],
    ]);
    expect(NAV).toHaveLength(10);
  });

  it('keeps the developer places out until they are used, and the command bar keeps them all', () => {
    const menu = menuFor('owner', new Set());
    expect(menu.sections.map((section) => section.label)).toEqual(['', 'Your business', '']);
    expect(menu.more.map((item) => item.label)).toEqual(['Accounts', 'Jobs that repeat', 'Brand', 'Settings']);
    expect(menu.all).toHaveLength(10);
    expect(menuFor('owner', new Set(['/$org/site-apis'])).sections[2]?.items.map((item) => item.label)).toEqual(['Site APIs']);
  });

  it('gives every place its own key after g', () => {
    const keys = NAV.map((item) => item.key);
    expect(new Set(keys).size).toBe(keys.length);
    // Needs you acts on these keys alone. The one after g is swallowed, but none should look like one.
    for (const key of ['j', 'k', 'd', 'x', 't']) expect(keys).not.toContain(key);
  });

  it('puts the four everyday places at the thumb and the rest under More', () => {
    const menu = menuFor('owner');
    expect(menu.tabs.map((item) => item.label)).toEqual(['Home', 'Needs you', 'Computers', 'What happened']);
    expect(menu.more.map((item) => item.label)).toEqual(['Accounts', 'Jobs that repeat', 'Brand', 'Runs and checks', 'Site APIs', 'Settings']);
  });

  it('knows which place and which list a page belongs to', () => {
    expect(placeOf('/acme')?.label).toBe('Home');
    expect(placeOf('/acme/runs/job-1')?.label).toBe('Runs and checks');
    expect(placeOf('/acme/checks/c1')?.label).toBe('Runs and checks');
    expect(placeOf('/acme/environments/e7k2')?.label).toBe('Computers');
    expect(placeOf('/acme/nodes/home-mini')?.label).toBe('Computers');
    expect(placeOf('/acme/email/leads')?.label).toBe('Jobs that repeat');
    expect(placeOf('/acme/flows')?.label).toBe('Jobs that repeat');
    expect(placeOf('/acme/activity')?.label).toBe('What happened');
    expect(placeOf('/acme/lanes/aster/instagram')?.label).toBe('Accounts');
    expect(placeOf('/acme/review/a1')?.label).toBe('Needs you');
    expect(placeOf('/acme/design')).toBeNull();
    expect(tabOf('/acme/nodes/home-mini')?.label).toBe('Machines');
    expect(tabOf('/acme/email')?.label).toBe('Email');
    expect(tabOf('/acme/brand')).toBeNull();
  });
});

describe('the menu by role', () => {
  it('gives an approver Needs you, What happened and Settings, and nothing else', () => {
    const menu = menuFor('approver');
    expect(menu.all.map((item) => item.label)).toEqual(['Needs you', 'What happened', 'Settings']);
    expect(menu.tabs).toHaveLength(3);
    expect(menu.more).toEqual([]);
  });

  it('opens Needs you for an approver who goes Home, and shows only their three places', async () => {
    db.orgs.get('northwind-group')!.role = 'approver';
    const { router } = renderApp('/northwind-group');
    await waitFor(() => expect(router.state.location.pathname).toBe('/northwind-group/needs-you'));
    const [rail] = await screen.findAllByRole('navigation', { name: 'Main' });
    await waitFor(() => expect(within(rail!).getAllByRole('link').map(linkWords)).toEqual(['Needs you', 'What happened', 'Settings']));
    // No status line: an approver has no machines to watch.
    expect(screen.queryByRole('button', { name: /^System:/ })).not.toBeInTheDocument();
  });
});

describe('the rail and the tab bar', () => {
  it('opens every place from the rail', async () => {
    const { user, router } = renderApp('/northwind-group');
    const [rail] = await screen.findAllByRole('navigation', { name: 'Main' });
    // The mock organisation has runs and Site APIs, so the developer places show.
    await within(rail!).findByRole('link', { name: /Site APIs/ });
    await within(rail!).findByRole('link', { name: /Runs and checks/ });
    const expected: Record<string, string> = {
      Home: '/northwind-group',
      'Needs you': '/northwind-group/needs-you',
      Computers: '/northwind-group/environments',
      'What happened': '/northwind-group/activity',
      Accounts: '/northwind-group/accounts',
      'Jobs that repeat': '/northwind-group/flows',
      Brand: '/northwind-group/brand',
      'Runs and checks': '/northwind-group/runs',
      'Site APIs': '/northwind-group/site-apis',
      Settings: '/northwind-group/settings',
    };
    for (const [label, path] of Object.entries(expected)) {
      const link = within(rail!).getAllByRole('link').find((item) => linkWords(item) === label)!;
      await user.click(link);
      await waitFor(() => expect(router.state.location.pathname).toBe(path));
      expect(link).toHaveAttribute('data-status', 'active');
    }
  }, 15_000);

  it('shows Computers as two lists, with Computers lit on both', async () => {
    const { user, router } = renderApp('/northwind-group/environments');
    const tabs = await screen.findByRole('navigation', { name: 'Computers' });
    expect(within(tabs).getAllByRole('link').map((link) => link.textContent)).toEqual(['Environments', 'Machines']);
    await user.click(within(tabs).getByRole('link', { name: 'Machines' }));
    await waitFor(() => expect(router.state.location.pathname).toBe('/northwind-group/nodes'));
    expect(await screen.findByRole('heading', { level: 1, name: 'Machines' })).toBeInTheDocument();
    const [rail] = screen.getAllByRole('navigation', { name: 'Main' });
    expect(within(rail!).getAllByRole('link').find((item) => linkWords(item) === 'Computers')).toHaveAttribute('data-status', 'active');
  });

  it('shows Jobs that repeat as Routines and Email, and Runs and checks as Runs and Checks', async () => {
    renderApp('/northwind-group/email');
    const jobs = await screen.findByRole('navigation', { name: 'Jobs that repeat' });
    expect(within(jobs).getAllByRole('link').map((link) => link.textContent)).toEqual(['Routines', 'Email']);
    expect(within(jobs).getByRole('link', { name: 'Email' })).toHaveAttribute('aria-current', 'page');
  });

  it('shows each place its g key, and the key goes there; the old keys of the lists still land', async () => {
    const { user, router } = renderApp('/northwind-group');
    const [rail] = await screen.findAllByRole('navigation', { name: 'Main' });
    await within(rail!).findByRole('link', { name: /Site APIs/ });
    await within(rail!).findByRole('link', { name: /Runs and checks/ });
    expect(within(rail!).getAllByRole('link').map((link) => link.getAttribute('aria-keyshortcuts'))).toEqual(NAV.map((item) => `G ${item.key.toUpperCase()}`));
    await user.keyboard('gc');
    await waitFor(() => expect(router.state.location.pathname).toBe('/northwind-group/environments'));
    await user.keyboard('gm');
    await waitFor(() => expect(router.state.location.pathname).toBe('/northwind-group/nodes'));
    await user.keyboard('gr');
    await waitFor(() => expect(router.state.location.pathname).toBe('/northwind-group/runs'));
    expect(await screen.findByRole('heading', { level: 1, name: 'Runs' })).toBeInTheDocument();
  });

  it('says where you are in the top bar, with the list as a link back', async () => {
    renderApp('/northwind-group/runs/job-7f3a2c91');
    const crumbs = await screen.findByRole('navigation', { name: 'Breadcrumb' });
    expect(within(crumbs).getByRole('link', { name: 'Runs and checks' })).toHaveAttribute('href', '/northwind-group/runs');
    expect(within(crumbs).getByText('job-7f3a2c91')).toHaveAttribute('aria-current', 'page');
  });

  it('links one machine back to Machines, in Computers', async () => {
    renderApp('/northwind-group/machines/home-mini');
    const crumbs = await screen.findByRole('navigation', { name: 'Breadcrumb' });
    expect(within(crumbs).getByRole('link', { name: 'Computers' })).toHaveAttribute('href', '/northwind-group/machines');
  });

  it('opens More on the phone tab bar with the places that do not fit', async () => {
    const { user } = renderApp('/northwind-group');
    const bars = await screen.findAllByRole('navigation', { name: 'Main' });
    const tabs = within(bars[1]!);
    expect(tabs.getAllByRole('link').map((item) => item.textContent?.replace(/, \d+ (waiting|open)$/, ''))).toEqual(['Home', 'Needs you', 'Computers', 'What happened']);
    await waitFor(() => expect(tabs.getByRole('button', { name: /^More/ }).getAttribute('aria-label')).toMatch(/Runs and checks, Site APIs/));
    await user.click(tabs.getByRole('button', { name: /^More/ }));
    const sheet = within(await screen.findByRole('dialog', { name: 'More' }));
    const more = within(sheet.getByRole('navigation', { name: 'More' }));
    expect(more.getAllByRole('link').map((item) => item.textContent)).toEqual(['Accounts', 'Jobs that repeat', 'Brand', 'Runs and checks', 'Site APIs', 'Settings']);
    // in the rail's groups, with the sidebar's foot under them: the organisation, with its words
    expect(more.getByRole('group', { name: 'Your business' })).toBeInTheDocument();
    expect(more.getByRole('group', { name: 'For developers' })).toBeInTheDocument();
    expect(sheet.getByRole('button', { name: /^Switch org/ })).toBeInTheDocument();
  });
});

describe('the developer places, hidden until first used', () => {
  it('leaves them out of the rail and More for an organisation that has none', async () => {
    nothingForDevelopers();
    const { user } = renderApp('/northwind-group');
    const [rail, bar] = await screen.findAllByRole('navigation', { name: 'Main' });
    await within(rail!).findByRole('link', { name: /Brand/ });
    expect(within(rail!).queryByText('For developers')).toBeNull();
    expect(within(rail!).getAllByRole('link').map(linkWords)).toEqual(['Home', 'Needs you', 'Computers', 'What happened', 'Accounts', 'Jobs that repeat', 'Brand', 'Settings']);
    await user.click(within(bar!).getByRole('button', { name: /^More/ }));
    const more = within(within(await screen.findByRole('dialog', { name: 'More' })).getByRole('navigation', { name: 'More' }));
    expect(more.getAllByRole('link').map((item) => item.textContent)).toEqual(['Accounts', 'Jobs that repeat', 'Brand', 'Settings']);
    expect(more.queryByRole('group', { name: 'For developers' })).toBeNull();
  });

  it('shows one once the person opens it, by the command bar, and keeps it', async () => {
    nothingForDevelopers();
    const { user, router, unmount } = renderApp('/northwind-group');
    await screen.findAllByRole('navigation', { name: 'Main' });
    await user.click(screen.getAllByRole('button', { name: 'Search or run a command' })[0]!);
    const dialog = await screen.findByRole('dialog', { name: 'Search or run a command' });
    await user.type(within(dialog).getByRole('combobox'), 'Site APIs');
    await user.click(await within(dialog).findByRole('option', { name: /^Site APIsGI$/i }));
    await waitFor(() => expect(router.state.location.pathname).toBe('/northwind-group/site-apis'));
    const [rail] = screen.getAllByRole('navigation', { name: 'Main' });
    expect(await within(rail!).findByText('For developers')).toBeInTheDocument();
    expect(within(rail!).getAllByRole('link').map(linkWords)).toContain('Site APIs');
    expect(within(rail!).getAllByRole('link').map(linkWords)).not.toContain('Runs and checks');
    unmount();
    // Remembered on this device: back Home, it is still there.
    renderApp('/northwind-group');
    const [again] = await screen.findAllByRole('navigation', { name: 'Main' });
    expect(await within(again!).findByRole('link', { name: /Site APIs/ })).toBeInTheDocument();
  });

  it('shows Runs and checks once the organisation has a run', async () => {
    server.use(http.get('/api/orgs/:org/site-apis', () => HttpResponse.json([])));
    renderApp('/northwind-group');
    const [rail] = await screen.findAllByRole('navigation', { name: 'Main' });
    expect(await within(rail!).findByRole('link', { name: /Runs and checks/ })).toBeInTheDocument();
    expect(within(rail!).getAllByRole('link').map(linkWords)).not.toContain('Site APIs');
  });
});

describe('old addresses', () => {
  it.each([
    ['/northwind-group/history', '/northwind-group/activity', 'What happened'],
    ['/northwind-group/activity', '/northwind-group/activity', 'What happened'],
    ['/northwind-group/routines', '/northwind-group/flows', 'Jobs that repeat'],
    ['/northwind-group/flows', '/northwind-group/flows', 'Jobs that repeat'],
    ['/northwind-group/machines', '/northwind-group/nodes', 'Computers'],
    ['/northwind-group/nodes', '/northwind-group/nodes', 'Computers'],
    ['/northwind-group/computers', '/northwind-group/nodes', 'Computers'],
    ['/northwind-group/environments', '/northwind-group/environments', 'Computers'],
    ['/northwind-group/email', '/northwind-group/email', 'Jobs that repeat'],
    ['/northwind-group/runs', '/northwind-group/runs', 'Runs and checks'],
    ['/northwind-group/checks', '/northwind-group/checks', 'Runs and checks'],
  ])('%s opens %s, under %s', async (address, route, place) => {
    const { router } = renderApp(address);
    await waitFor(() => expect(router.state.location.pathname).toBe(route));
    const crumbs = await screen.findByRole('navigation', { name: 'Breadcrumb' });
    expect(within(crumbs).getByText(place)).toBeInTheDocument();
  });
});

describe('the command bar', () => {
  it('offers the six most used places, and every place with its keys once you type', async () => {
    const { user } = renderApp('/northwind-group');
    await screen.findAllByRole('navigation', { name: 'Main' });
    await user.click(screen.getAllByRole('button', { name: 'Search or run a command' })[0]!);
    const dialog = await screen.findByRole('dialog', { name: 'Search or run a command' });
    const first = ['h', 'n', 'c', 'y', 'a', 'o'];
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
    // The lists inside a place, by their own names.
    await user.clear(input);
    await user.type(input, 'machines');
    expect(await within(dialog).findByRole('option', { name: /^Machines\s*In Computers$/ })).toBeInTheDocument();
    await user.clear(input);
    await user.type(input, 'cloud');
    expect(await within(dialog).findByRole('option', { name: /^Cloud server/ })).toBeInTheDocument();
  }, 15_000);
});
