import { screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import type { SiteApi, SiteDiscovery, SiteField } from '@/api/types';
import { db, resetDb } from '@/mocks/handlers';
import { renderApp } from '@/test/render';
import { server } from '@/test/setup';
import { definitionOf, draftFields, draftRows, firstDraft } from './draft';
import { callUrl, HEALTH_WORD, openApiOf, problemWords, snippet, toCsv } from './words';

const ORG = '/northwind-group';

// The pages render whole in jsdom, which a loaded machine takes its time over.
vi.setConfig({ testTimeout: 20_000 });

const api = (rest: Partial<SiteApi>) => ({ site: { name: 'Fernway', domain: 'track.fernway.example' }, account: 'Harbor Supply Ltd', problem: null, health: 'good', ...rest }) as SiteApi;

describe('problemWords', () => {
  it('has words for every state, and none for a good one', () => {
    expect(problemWords(api({ health: 'good' }))).toBeNull();
    for (const health of Object.keys(HEALTH_WORD).filter((key) => key !== 'good')) {
      expect(problemWords(api({ health: health as SiteApi['health'], problem: { since: '2026-11-05T09:00:00Z' } }))?.title).toBeTruthy();
    }
  });

  it('says what changed, and that nothing was passed on', () => {
    const words = problemWords(api({ health: 'changed', problem: { since: '2026-11-05T09:00:00Z', now_label: 'Latest scan' } }), { key: 'status', label: 'Status', kind: 'status' });
    expect(words?.title).toContain('"Status"');
    expect(words?.body).toContain('"Latest scan"');
    expect(words?.body).toContain('nothing was passed on');
  });

  it('never uses a word from the engine on a surface a person reads', () => {
    const banned = /\b(lanes?|grants?|surfaces?|scrap\w+)\b/i;
    for (const health of Object.keys(HEALTH_WORD)) {
      const words = problemWords(api({ health: health as SiteApi['health'], problem: { since: '2026-11-05T09:00:00Z', resumes_at: '2026-11-05T12:00:00Z' } }));
      expect(`${words?.title} ${words?.body} ${words?.callers}`).not.toMatch(banned);
    }
  });
});

describe('the line a Site API stays inside', () => {
  it('offers nothing about spreading calls, rotating accounts or getting past a limit', () => {
    const dir = __dirname;
    const source = readdirSync(dir)
      .filter((name) => /\.tsx?$/.test(name) && !name.includes('.test.'))
      .map((name) => readFileSync(join(dir, name), 'utf8'))
      .join('\n');
    expect(source).not.toMatch(/rotate|another account to|more accounts|bypass|proxy/i);
  });
});

describe('the shapes a call can take', () => {
  it('builds the address from what was filled in and leaves out what was not', () => {
    expect(callUrl('nordic-beans', 'open_orders', { status: 'open', limit: '' })).toBe('https://relay.openajar.example/v1/sites/nordic-beans/open_orders?status=open');
  });

  it('never puts a key in a snippet', () => {
    for (const kind of ['curl', 'javascript', 'python', 'n8n'] as const) expect(snippet(kind, 'https://x.example/v1')).not.toMatch(/aos_/);
    expect(snippet('curl', 'https://x.example/v1')).toContain('$OPENAJAR_KEY');
  });

  it('holds no site data in the OpenAPI document', () => {
    const field: SiteField = { key: 'total', label: 'Total', kind: 'money' };
    const endpoint = { key: 'open_orders', title: 'Open orders', description: '', effect: 'read', input: [], fields: [field], rows: [{ total: 'A-SECRET-VALUE' }], total: 1, cache_s: 300 } as never;
    const text = JSON.stringify(openApiOf({ key: 'nordic-beans', title: 'x', version: '1.0.0', endpoints: [endpoint] }));
    expect(text).toContain('"openapi":"3.1.0"');
    expect(text).toContain('nordic_beans_open_orders');
    expect(text).not.toContain('A-SECRET-VALUE');
  });

  it('quotes a CSV, and defuses a cell that a spreadsheet would run', () => {
    const fields: SiteField[] = [{ key: 'a', label: 'Note', kind: 'text' }];
    expect(toCsv(fields, [{ a: 'one, "two"' }])).toBe('Note\n"one, ""two"""');
    expect(toCsv(fields, [{ a: '=HYPERLINK("x")' }])).toContain(`'=HYPERLINK`);
  });
});

describe('a draft', () => {
  const found = {
    lane: 'aster/portal',
    api: 'nordic-beans',
    site: { name: 'Nordic Beans', domain: 'orders.nordicbeans.example' },
    rows: [{ number: 'A-1' }],
    total: 1,
    regions: [],
    suggestions: [],
    page: {
      url: 'https://orders.nordicbeans.example/orders',
      wordmark: 'Nordic Beans',
      nav: [],
      heading: 'Your orders',
      count_words: '1',
      layout: 'table',
      side: [],
      seen_at: '',
      columns: [
        { label: 'Order', field: 'number' },
        { label: 'Buyer', field: null, values: ['L. Kairys'] },
      ],
    },
  } satisfies SiteDiscovery;

  it('starts from what the computer would keep, and takes a person\'s names', () => {
    const draft = { ...firstDraft(found), names: { Order: 'Number' } };
    expect(draft.kept).toEqual(['Order']);
    expect(draftFields(found, draft)[0]?.label).toBe('Number');
  });

  it('reads a column a person added back, from what the page said', () => {
    const draft = { ...firstDraft(found), kept: ['Order', 'Buyer'] };
    expect(draftRows(found, draft)[0]).toEqual({ order: 'A-1', buyer: 'L. Kairys' });
  });

  it('defines only the kept columns, on the one account, and on the site\'s own domain', () => {
    const definition = definitionOf(found, firstDraft(found));
    const text = JSON.stringify(definition);
    expect(definition.surfaces).toEqual(['aster/portal']);
    expect(text).not.toContain('buyer');
    expect(text).not.toMatch(/cookie|session|password/i);
  });
});

describe('the pages on the mock', () => {
  it('leads the list with what needs a person, and an agent\'s proposal to decide on', async () => {
    renderApp(`${ORG}/site-apis`);
    expect(await screen.findByRole('heading', { name: /4 websites are answering/ })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /Turn a website you use into a live table/ })).toBeInTheDocument();
    const proposal = screen.getByRole('region', { name: 'Proposed by an agent' });
    expect(within(proposal).getByText(/Nothing runs until a person turns it on/)).toBeInTheDocument();
    expect(within(proposal).getByRole('link', { name: 'Look and decide' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: /Fernway Couriers changed its page/ })).toHaveTextContent('Show me');
  });

  it('asks a person to look first, then offers the fix, when a site changed shape', async () => {
    const { user } = renderApp(`${ORG}/site-apis/fernway-tracking?mock=still`);
    await user.click(await screen.findByRole('button', { name: 'Show me' }));
    await user.click(await screen.findByRole('button', { name: 'Use "Latest scan" for Status' }));
    await waitFor(() => expect(screen.queryByRole('region', { name: 'What needs attention' })).not.toBeInTheDocument());
    expect(screen.getAllByText('Answering').length).toBeGreaterThan(0);
  });

  it('shows a new key once, and only its first letters after that', async () => {
    const { user } = renderApp(`${ORG}/site-apis/nordic-beans`);
    await user.click(await screen.findByRole('button', { name: 'New key' }));
    await user.type(await screen.findByLabelText('What is it for?'), 'Roastery sheet');
    await user.click(screen.getByRole('button', { name: 'Make the key' }));
    expect(await screen.findByText(/only time it is shown/)).toBeInTheDocument();
    const secret = screen.getByRole('dialog').querySelector('code')?.textContent ?? '';
    expect(secret).toMatch(/^aos_[\w-]{43}$/);
    await user.click(screen.getByRole('button', { name: 'I have saved it' }));
    const row = await screen.findByRole('row', { name: /Roastery sheet/ });
    expect(row).toHaveTextContent(`${secret.slice(0, 8)}...`);
    expect(row).not.toHaveTextContent(secret);
    expect(row).toHaveTextContent('Reads');
  });

  it('asks before a shown key is lost, and a person can go back to it or revoke it', async () => {
    const { user } = renderApp(`${ORG}/site-apis/nordic-beans`);
    await user.click(await screen.findByRole('button', { name: 'New key' }));
    await user.type(await screen.findByLabelText('What is it for?'), 'Lost sheet');
    await user.click(screen.getByRole('button', { name: 'Make the key' }));
    expect(await screen.findByText(/only time it is shown/)).toBeInTheDocument();
    await user.keyboard('{Escape}');
    expect(await screen.findByRole('heading', { name: 'Close without saving the key?' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Go back to the key' }));
    expect(await screen.findByText(/only time it is shown/)).toBeInTheDocument();
    await user.click(document.querySelector('[data-dialog-overlay]') as HTMLElement);
    expect(await screen.findByRole('heading', { name: 'Close without saving the key?' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Revoke it and close' }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await waitFor(() => expect(screen.queryByRole('row', { name: /Lost sheet/ })).not.toBeInTheDocument());
  });

  it('only turns a proposal on when a person says so', async () => {
    const { user } = renderApp(`${ORG}/site-apis/nordic-prices`);
    expect(await screen.findByRole('heading', { name: /only a person can turn it on/ })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'API keys' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Turn it on' }));
    expect(await screen.findByRole('heading', { name: 'API keys' })).toBeInTheDocument();
  });

  it('says so honestly when a page has nothing to read', async () => {
    renderApp(`${ORG}/site-apis/new?step=found&lane=personal/mail`);
    expect(await screen.findByRole('heading', { name: 'I could not find a list here' })).toBeInTheDocument();
  });

  it('keeps a draft a person saved on the list, under Saved, not on yet', async () => {
    const draft = {
      key: 'harbor-price-sheet', title: 'Harbor price sheet', site: { name: 'Harbor Supply', domain: 'orders.harbor.example' },
      account: 'Harbor Supply Ltd', workspace: 'harbor', lane: 'harbor/portal', words: 'Every price on the sheet.', stage: 'proposed',
      health: 'unread', problem: null, proposal: null, sample: { kind: 'rows', columns: [], rows: [] }, spark: [], calls_7d: 0,
      cached_pct: null, read_at: null, version: '0.1.0',
    };
    server.use(http.get('/api/orgs/:org/site-apis', () => HttpResponse.json([draft])));
    renderApp(`${ORG}/site-apis`);
    const saved = await screen.findByRole('region', { name: 'Saved, not on yet' });
    expect(within(saved).getByText('Harbor price sheet')).toBeInTheDocument();
    expect(within(saved).getByText(/nothing runs until a person turns it on/i)).toBeInTheDocument();
    expect(within(saved).getByRole('link', { name: 'Open it' })).toHaveAttribute('href', expect.stringContaining('harbor-price-sheet'));
  });
});

describe('what a computer that does not serve a part says', () => {
  it('shows the keys as not here, never as a skeleton that stays', async () => {
    server.use(http.get('/api/orgs/:org/site-keys', () => new HttpResponse('Not Found', { status: 404 })));
    renderApp(`${ORG}/site-apis/nordic-beans`);
    expect(await screen.findByText('Shown on your computer', undefined, { timeout: 5_000 })).toBeInTheDocument();
  });

  it('says a page could not be looked at as an error, not as a page with nothing to repeat', async () => {
    server.use(http.get('/api/orgs/:org/site-apis/discover', () => HttpResponse.json({ error: { code: 'unsupported', message: 'The page could not be read right now.' } }, { status: 503 })));
    renderApp(`${ORG}/site-apis/new?step=found&lane=personal/mail`);
    expect(await screen.findByRole('heading', { name: 'Could not load this' })).toBeInTheDocument();
    expect(screen.queryByText('Nothing repeats on this page')).toBeNull();
  });

  it('says a page is not served here, and never that nothing repeats on it', async () => {
    server.use(http.get('/api/orgs/:org/site-apis/discover', () => new HttpResponse('Not Found', { status: 404 })));
    renderApp(`${ORG}/site-apis/new?step=found&lane=personal/mail`);
    expect(await screen.findByText('Shown on your computer', undefined, { timeout: 5_000 })).toBeInTheDocument();
    expect(screen.queryByText('Nothing repeats on this page')).toBeNull();
  });

  it('says none of the accounts is ready, and that an account not ready links to its own page to sign in', async () => {
    // Every browser account on the demo is not signed in yet: the page must say so, and each links to its own page.
    for (const lane of db.orgs.get('northwind-group')!.lanes) if (lane.kind === 'browser') lane.readiness = 'needs_login';
    try {
      renderApp(`${ORG}/site-apis/new`);
      expect(await screen.findByText(/None of your accounts is ready yet/)).toBeInTheDocument();
      const signIn = (await screen.findAllByText('Not signed in yet'))[0]!;
      // The link goes to the account's own page, where it is signed in.
      expect(signIn.closest('a')).toHaveAttribute('href', expect.stringMatching(/\/(accounts|lanes)\/[^/]+\/[^/]+$/));
      expect(screen.queryByRole('button', { name: /Not signed in yet/ })).toBeNull();
    } finally {
      resetDb();
    }
  });

  it('writes the step sentence with no question mark running into a comma', async () => {
    const { container } = renderApp(`${ORG}/site-apis/new`);
    await waitFor(() => expect(container.textContent).toContain('call it a name.'));
    expect(container.textContent).not.toMatch(/\?,/);
  });
});
