import { screen, waitFor, within } from '@testing-library/react';
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { renderApp } from '@/test/render';
import { planDays } from './plan';
import { addressParts, agoWords, endingWords, hintFor, inWords, looksLikeAddress } from './words';
import type { Ending } from './words';
import { asComputer } from '@/test/computer';

const ORG = '/northwind-group';

// The pages render whole in jsdom, which a loaded machine takes its time over.
vi.setConfig({ testTimeout: 20_000 });

const here = __dirname;
const sources = readdirSync(here).filter((name) => /\.(ts|tsx)$/.test(name) && !name.includes('.test.'));
const read = (name: string) => readFileSync(join(here, name), 'utf8');
const mock = readFileSync(join(here, '../../mocks/email.ts'), 'utf8');
const people = (count: number) => Array.from({ length: count }, (_, index) => ({ id: `p${index}`, name: `Person ${index}` }));
const MONDAY = new Date('2026-11-02T09:00:00').getTime();
const DAY = 86_400_000;

describe('what the hosted panel must never do', () => {
  it('has no password field on any page, the computer\'s form included', () => {
    for (const name of sources) expect(read(name)).not.toMatch(/type=["']password["']/);
    expect(read('computer-form.tsx')).not.toMatch(/<input|<Input|<textarea|<Textarea/);
  });

  it('takes no password in any type or handler', () => {
    const types = readFileSync(join(here, '../../api/types.ts'), 'utf8');
    const email = types.slice(types.indexOf('Email, at concept stage'));
    expect(email).not.toMatch(/password\??:\s*string/);
    expect(mock).not.toMatch(/body\.password|\{ password/);
  });

  it('offers nothing that gets around a limit or a filter', () => {
    const banned = /rotat(ing|ion)\b|rotate (your |the )?(sender|mailbox|address)|warm.?up|deliverab|spam|guess(ed|ing)? (an )?address|address pattern|verify the address/i;
    for (const name of sources) expect(`${name}: ${read(name)}`).not.toMatch(banned);
    expect(mock).not.toMatch(banned);
  });

  it('never shows a real person\'s address in the demo', () => {
    const addresses = mock.match(/[\w.+-]+@[\w.-]+\.\w+/g) ?? [];
    expect(addresses.length).toBeGreaterThan(5);
    for (const address of addresses) expect(address).toMatch(/\.example$/);
  });
});

describe('the words of a failure', () => {
  const endings: Ending[] = ['wrong-password', 'two-step-off', 'imap-blocked', 'certificate'];

  it('has plain words for each, with no word from the engine', () => {
    for (const ending of endings) {
      const words = endingWords(ending, 'Gmail', 'gmail.example', 'home-mini');
      expect(words.title).toBeTruthy();
      expect(`${words.title} ${words.body} ${words.caption}`).not.toMatch(/\b(imap|smtp|tls|ssl|port|lane|node|vault)\b/i);
    }
  });

  it('says which of the two checks failed', () => {
    expect(endingWords('imap-blocked', 'Gmail', 'a.example', 'home-mini')).toMatchObject({ read: 'no', send: 'yes' });
    expect(endingWords('wrong-password', 'Gmail', 'a.example', 'home-mini')).toMatchObject({ read: 'no', send: 'no' });
  });
});

describe('words', () => {
  it('knows a whole address from a part of one', () => {
    expect(looksLikeAddress('mia@gmail.example')).toBe(true);
    expect(looksLikeAddress('mia@gmail')).toBe(false);
    expect(looksLikeAddress('mia')).toBe(false);
  });

  it('names what the address gives away, and nothing it does not', () => {
    expect(hintFor('mia@gmail.example')?.word).toBe('Looks like Gmail');
    expect(hintFor('mia@harbor-supply.example')).toBeNull();
  });

  it('splits an address at the @ and spells time out', () => {
    expect(addressParts('a@b.example')).toEqual({ local: 'a', domain: '@b.example' });
    expect(inWords(['Estonia', 'Latvia', 'Sweden'])).toBe('Estonia, Latvia and Sweden');
    expect(agoWords(new Date(MONDAY - 2 * DAY).toISOString(), MONDAY)).toBe('2 days ago');
    expect(agoWords(new Date(MONDAY - 60_000).toISOString(), MONDAY)).toBe('1 minute ago');
  });
});

describe('the send plan', () => {
  const base = { limit: 60, other: 12, start: MONDAY, follow: true };

  it('writes to each person once, and never on a weekend', () => {
    const days = planDays({ ...base, people: people(46), pace: 15 });
    const firsts = days.flatMap((day) => day.first.map((person) => person.id));
    expect(firsts).toHaveLength(46);
    expect(new Set(firsts).size).toBe(46);
    for (const day of days) expect([0, 6]).not.toContain(new Date(day.at).getDay());
  });

  it('never goes over the mailbox\'s limit, whatever the pace', () => {
    for (const pace of [5, 15, 30, 48]) {
      for (const day of planDays({ ...base, people: people(200), pace })) {
        expect(day.other + day.first.length + day.follow.length).toBeLessThanOrEqual(60);
      }
    }
  });

  it('holds a follow-up for its full three days, and sends it once', () => {
    const days = planDays({ ...base, people: people(30), pace: 10 });
    const firstAt = new Map(days.flatMap((day) => day.first.map((person) => [person.id, day.at] as const)));
    const follows = days.flatMap((day) => day.follow.map((person) => ({ id: person.id, at: day.at })));
    expect(follows).toHaveLength(30);
    expect(new Set(follows.map((item) => item.id)).size).toBe(30);
    for (const follow of follows) expect(follow.at - (firstAt.get(follow.id) ?? 0)).toBeGreaterThanOrEqual(3 * DAY - 3_600_000);
  });

  it('sends no follow-up when that is switched off', () => {
    expect(planDays({ ...base, people: people(30), pace: 10, follow: false }).flatMap((day) => day.follow)).toEqual([]);
  });
});

describe('the pages', () => {
  it('sees the mailbox in trouble first, with its one next step', async () => {
    renderApp(`${ORG}/email?mock=still`);
    expect(await screen.findByRole('heading', { name: /2 mailboxes are fine/ })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Connect it again' })).toHaveAttribute('href', expect.stringContaining('studio%40solstice-studio.example'));
    expect(screen.getAllByRole('meter', { name: 'Sent today' })).toHaveLength(2);
  });

  it('asks for an address, never a password, and names the provider while typing', async () => {
    asComputer();
    const { user, container } = renderApp(`${ORG}/email/connect?mock=still`);
    await user.type(await screen.findByRole('textbox', { name: 'What is the email address?' }), 'mia@gmail.example');
    expect(screen.getByText('Looks like Gmail')).toBeInTheDocument();
    expect(container.querySelector('input[type="password"]')).toBeNull();
  });

  it('holds a low-trust result for a person, and marks the name that differs', async () => {
    asComputer();
    renderApp(`${ORG}/email/connect?address=ola%40nordlys-post.example&stage=found&mock=still`);
    expect(await screen.findByRole('heading', { name: 'Please look at these two names' })).toBeInTheDocument();
    expect(screen.getAllByText('nordlys-mailer.example')).toHaveLength(2);
    const wrong = screen.getByRole('button', { name: 'These look wrong' });
    const right = screen.getByRole('button', { name: 'Yes, these are right' });
    expect(wrong.compareDocumentPosition(right) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('offers no way past a certificate that did not check out', async () => {
    asComputer();
    renderApp(`${ORG}/email/connect?address=post%40old-mill-post.example&stage=failed&ending=certificate&mock=still`);
    expect(await screen.findByRole('heading', { name: 'This mail server could not prove who it is' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Copy a note for your mail company' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /anyway|continue|ignore/i })).toBeNull();
  });

  it('shows mail as text, marked, with nothing fetched', async () => {
    renderApp(`${ORG}/email/new-mail?message=m-fair&mock=still`);
    const article = await screen.findByRole('article', { name: 'Last stands for the spring fair' });
    expect(within(article).getByText('Written by someone else')).toBeInTheDocument();
    expect(within(article).getByText(/img\.balticfoodfair\.example/)).toBeInTheDocument();
    expect(article.querySelector('img')).toBeNull();
    expect(article.querySelector('a[href*="track.balticfoodfair"]')).toBeNull();
  });

  it('counts a person leaving the batch, and undoes it', async () => {
    const { user } = renderApp(`${ORG}/email/leads/marinas?mock=still`);
    expect(await screen.findByRole('button', { name: 'Take all 46' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /^Liis Tamm/ }));
    expect(await screen.findByRole('button', { name: 'Take these 45' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Undo' }));
    expect(await screen.findByRole('button', { name: 'Take all 46' })).toBeInTheDocument();
  });

  it('never lets a suppressed person be pressed', async () => {
    renderApp(`${ORG}/email/leads/marinas?mock=still`);
    await screen.findByRole('button', { name: 'Take all 46' });
    expect(screen.getByText('Paul Rebane')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Paul Rebane/ })).toBeNull();
  });

  it('says the sender\'s note once, and keeps it gone', async () => {
    localStorage.removeItem('agentos.email.first-list-note');
    const { user, unmount } = renderApp(`${ORG}/email/lists/marinas?mock=still`);
    expect(await screen.findByRole('heading', { name: 'A word before your first list' })).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Understood' }));
    await waitFor(() => expect(screen.queryByRole('heading', { name: 'A word before your first list' })).toBeNull());
    unmount();
    renderApp(`${ORG}/email/lists/marinas?mock=still`);
    await screen.findByRole('heading', { name: 'What each person gets' });
    expect(screen.queryByRole('heading', { name: 'A word before your first list' })).toBeNull();
  });

  it('tells the routine in the same sentence language as Routines', async () => {
    renderApp(`${ORG}/email/routines/follow-up?mock=still`);
    const heading = await screen.findByRole('heading', { level: 1 });
    expect(heading).toHaveTextContent('When someone gets your first email, wait 3 days, then if they replied, stop, and tell you, otherwise send one follow-up.');
  });
});
