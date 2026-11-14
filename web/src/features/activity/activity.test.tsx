import { screen, waitFor, within } from '@testing-library/react';
import { db } from '@/mocks/handlers';
import { writeTechnical } from '@/lib/technical';
import { renderApp } from '@/test/render';

const PLACE = /(your personal |(Aster Coffee|Harbor Supply|Meridian Travel|Solstice Studio)'s |the Mac mini)[\w .]+$/;

async function today() {
  return screen.findByRole('region', { name: 'Today' });
}

describe('History', () => {
  it('tells each thing done as a sentence on a business and a site, with its result', async () => {
    renderApp('/northwind-group/activity');
    const day = await today();
    const sentences = within(day).getAllByRole('button', { name: /^What happened: / });
    expect(sentences.length).toBeGreaterThan(3);
    for (const sentence of sentences.slice(0, 10)) expect(sentence.textContent).toMatch(PLACE);
    expect(within(day).getAllByText('Live, checked').length).toBeGreaterThan(0);
    // No ids, no engine words, no cost per run while Technical details is off.
    expect(within(day).queryByText(/aster\/|claude-code|\$\d/)).toBeNull();
    expect(within(day).queryByText(/Verified|Unverified/)).toBeNull();
  });

  it('says who did it in words, with the agent or routine mark', async () => {
    renderApp('/northwind-group/activity');
    const day = await today();
    expect(within(day).getAllByText(/^(Claude Code|Claude|Codex|Weekly posts routine)/).length).toBeGreaterThan(0);
  });

  it('says what did not happen when something did not work, and what to do', async () => {
    renderApp('/northwind-group/activity');
    await today();
    const notes = await screen.findAllByText(/^Nothing was (posted|published)\./);
    expect(notes.length).toBeGreaterThan(0);
    const row = notes[0]!.closest('li')!;
    expect(within(row).getAllByText('Did not work').length).toBeGreaterThan(0);
    expect(within(row).getByRole('link', { name: /the account/ })).toHaveAttribute('href', expect.stringMatching(/\/(lanes|accounts)\/\w+\/\w+$/));
  });

  it('folds looking-only into one line a day, with a line per account behind Show', async () => {
    const { user } = renderApp('/northwind-group/activity');
    const day = await today();
    const reads = within(day).getByRole('region', { name: 'Looked only, Today' });
    expect(within(reads).getByText(/^Also checked \d+ accounts? \d+ times?, and changed nothing\.$/)).toBeInTheDocument();
    await user.click(within(reads).getByRole('button', { name: 'Show' }));
    const lines = within(reads).getByRole('list', { name: 'Each account, Today' });
    expect(within(lines).getAllByText(/^Checked .+ (once|twice|\d+ times)$/).length).toBeGreaterThan(0);
  });

  it('shows what is working right now on top, in pink, with a way to watch', async () => {
    renderApp('/northwind-group/activity');
    const working = await screen.findByRole('region', { name: 'Working now' });
    expect(within(working).getByText(/^Claude Code is publishing the weekly post on Aster Coffee's Instagram$/)).toBeInTheDocument();
    expect(within(working).getByRole('link', { name: 'Watch' })).toHaveAttribute('href', expect.stringMatching(/^\/northwind-group\/(lanes|accounts)\/aster\/instagram$/));
  });

  it('filters calmly: Problems shows only what did not work', async () => {
    const { user } = renderApp('/northwind-group/activity');
    await today();
    await user.click(screen.getByRole('radio', { name: /^Problems/ }));
    await waitFor(() => expect(screen.queryAllByText('Live, checked')).toHaveLength(0));
    const day = await today();
    expect(within(day).getAllByText(/^(Did not work|Partly done)$/).length).toBeGreaterThan(0);
    expect(screen.queryByRole('combobox', { name: 'Started by' })).toBeNull();
  });

  it('offers a way back when nothing matches', async () => {
    const { user } = renderApp('/northwind-group/activity?kind=shop&workspace=solstice');
    expect(await screen.findByText('Nothing like that lately')).toBeInTheDocument();
    await user.click(screen.getAllByRole('button', { name: 'Show everything' })[0]!);
    expect(await today()).toBeInTheDocument();
  });

  it('pages back to older days', async () => {
    const { user } = renderApp('/northwind-group/activity');
    await today();
    const days = () => screen.getAllByRole('heading', { level: 2 }).length;
    const first = days();
    await user.click(screen.getByRole('button', { name: 'Show older days' }));
    await waitFor(() => expect(days()).toBeGreaterThan(first));
  });

  it('opens a story with its verdict, its proof and the steps in words', async () => {
    const { user } = renderApp('/northwind-group/activity');
    const day = await today();
    const checked = within(day).getAllByText('Live, checked')[0]!.closest('li')!;
    await user.click(within(checked).getByRole('button', { name: /^What happened: / }));
    const drawer = await screen.findByRole('dialog', { name: 'What happened' });
    expect(within(drawer).getByText('It is live, and we checked.')).toBeInTheDocument();
    expect(within(drawer).getByRole('img', { name: /showed afterwards/ })).toBeInTheDocument();
    expect(within(drawer).getByRole('link', { name: /^Open on / })).toHaveAttribute('rel', 'noreferrer noopener');
    expect(within(drawer).getByRole('list', { name: 'What the agent did' })).toBeInTheDocument();
  });

  it('says calmly that nothing has gone out yet in an empty organisation', async () => {
    renderApp('/juniper-labs/activity');
    expect(await screen.findByText('Nothing has gone out yet')).toBeInTheDocument();
  });

  describe('with Technical details on', () => {
    beforeEach(() => writeTechnical(db.user.id, true));
    afterEach(() => writeTechnical(db.user.id, false));

    it('puts ids and costs on each story, and keeps the raw log, its chart and every filter one switch away', async () => {
      const { user } = renderApp('/northwind-group/activity');
      const day = await today();
      expect(within(day).getAllByText(/\$\d+\.\d{2} · (run|trc)_/).length).toBeGreaterThan(0);
      await user.click(screen.getByRole('radio', { name: 'Raw log' }));
      const table = await screen.findByRole('table', { name: 'Activity log' });
      expect(within(table).getAllByText(/^\$\d+\.\d{2}$/).length).toBeGreaterThan(0);
      expect(within(table).getAllByText('Claude Code').length).toBeGreaterThan(0);
      expect(within(table).queryByText('claude-code')).toBeNull();
      expect(screen.getByRole('combobox', { name: 'Started by' })).toBeInTheDocument();
    });

    it('filters the raw log by who started it, by id, and shows the name', async () => {
      const { user } = renderApp('/northwind-group/activity?principal=codex');
      await today();
      await user.click(screen.getByRole('radio', { name: 'Raw log' }));
      const table = await screen.findByRole('table', { name: 'Activity log' });
      expect(within(table).getAllByText('Codex').length).toBeGreaterThan(0);
      expect(within(table).queryByText('Claude Code')).toBeNull();
    });
  });
});

describe('a busy day', () => {
  it('shows the first 25 stories, then 25 more at a time, and says how many are left', async () => {
    const { server } = await import('@/test/setup');
    const { http, HttpResponse } = await import('msw');
    const at = (minute: number) => new Date(Date.now() - minute * 60_000).toISOString();
    const stories = Array.from({ length: 60 }, (_, index) => ({
      id: `run_${index}`,
      source: 'run',
      at: at(index + 1),
      started_at: at(index + 1),
      lane: 'aster/instagram',
      workspace: 'aster',
      workspace_name: 'Aster Coffee',
      place: "Aster Coffee's Instagram",
      site: { key: 'instagram', label: 'Instagram' },
      action: 'post',
      sentence: `Posted photo number ${index} on Aster Coffee's Instagram`,
      verdict: 'checked',
      verdict_text: 'It is live, and we checked.',
      reason: null,
      detail: null,
      status: 'succeeded_verified',
      proof: null,
      who: { kind: 'flow', id: 'aster.instagram.post', name: 'Instagram post', brand: null },
      why: { trigger: 'schedule', text: 'On its schedule', approval: null },
      steps: [],
      technical: { run_id: `run_${index}`, flow: 'aster.instagram.post', effect: 'commit', cost: { tokens: 0, usd: 0 } },
    }));
    const day = { date: '2026-11-04', label: 'Today', summary: '60 things done · 60 checked', done: 60, checked: 60, problems: 0, payments: 0, stories, reads: null };
    server.use(
      http.get('/api/orgs/:org/history', () =>
        HttpResponse.json({ timezone: 'UTC', counts: { everything: 60, posts: 60, problems: 0, spending: 0 }, working_now: [], days: [day], next_before: null, retention_days: 30 }),
      ),
    );
    const { user } = renderApp('/northwind-group/activity');
    const today = await screen.findByRole('region', { name: 'Today' });
    expect(within(today).getAllByRole('button', { name: /^What happened: / })).toHaveLength(25);
    expect(within(today).getByText('35 more things this day')).toBeInTheDocument();
    await user.click(within(today).getByRole('button', { name: 'Show 25 more' }));
    expect(within(today).getAllByRole('button', { name: /^What happened: / })).toHaveLength(50);
    await user.click(within(today).getByRole('button', { name: 'Show 10 more' }));
    expect(within(today).getAllByRole('button', { name: /^What happened: / })).toHaveLength(60);
    expect(within(today).queryByText(/more things? this day/)).toBeNull();
  }, 15_000);
});

describe('more than posting', () => {
  it('offers only the kinds of work that happened, and keeps to one', async () => {
    const { user } = renderApp('/northwind-group/activity');
    await today();
    const show = screen.getByRole('radiogroup', { name: 'Show' });
    for (const kind of ['Sent and published', 'Paperwork and money', 'Forms and bookings', 'Research', 'Orders', 'Files and commands']) {
      expect(within(show).getByRole('radio', { name: kind })).toBeInTheDocument();
    }
    await user.click(within(show).getByRole('radio', { name: 'Paperwork and money' }));
    const day = await today();
    await waitFor(() => expect(within(day).queryByText('Live, checked')).toBeNull());
    expect(within(day).getByText(/^Downloaded 6 supplier invoices from Harbor Supply's Xero/)).toBeInTheDocument();
    expect(within(day).getByText(/^Paid €412\.50 to Bean & Co/)).toBeInTheDocument();
  });

  it('shows an email as it went out, with its attachment, as the proof', async () => {
    renderApp('/northwind-group/activity?run=run_EX01EMAIL');
    const drawer = await screen.findByRole('dialog', { name: 'What happened' });
    expect(await within(drawer).findByText('It was sent, and we checked.')).toBeInTheDocument();
    const email = within(drawer).getByRole('figure', { name: 'The email as it was sent' });
    expect(within(email).getByText('Invoice 2026-0412 for October')).toBeInTheDocument();
    expect(within(email).getByText('Invoice-2026-0412.pdf')).toBeInTheDocument();
    expect(within(email).getByText('84 KB')).toBeInTheDocument();
  });

  it('shows a form as sent in, with what changed marked and what it was before', async () => {
    renderApp('/northwind-group/activity?run=run_EX04FORM');
    const drawer = await screen.findByRole('dialog', { name: 'What happened' });
    const form = await within(drawer).findByRole('figure', { name: 'The form as it was sent in' });
    expect(within(form).getByText('2 changes')).toBeInTheDocument();
    expect(within(form).getByText('€104')).toHaveClass('line-through');
    expect(within(form).getByText('€118')).toBeInTheDocument();
  });

  it('says plainly what did not happen when a form could not go in', async () => {
    renderApp('/northwind-group/activity?run=run_EX08FAILFORM');
    const drawer = await screen.findByRole('dialog', { name: 'What happened' });
    expect(await within(drawer).findByText('It did not work.')).toBeInTheDocument();
    expect(within(drawer).getByText(/^Nothing was sent in\. The portal asked for a code from your phone/)).toBeInTheDocument();
  });
});

