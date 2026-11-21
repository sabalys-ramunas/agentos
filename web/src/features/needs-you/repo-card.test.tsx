import { screen, waitFor, within } from '@testing-library/react';
import type { Principal, Question } from '@/api/types';
import { db } from '@/mocks/handlers';
import { renderApp } from '@/test/render';

const PATH = '/northwind-group/needs-you';
const ORG = 'northwind-group';
const org = () => db.orgs.get(ORG)!;
const REPO = 'github.com/acme/private-app';
vi.setConfig({ testTimeout: 20_000 });

function addCard(reason: 'not_connected' | 'refused' = 'not_connected'): Question {
  const now = Date.now();
  const card: Question = {
    id: 'ap_repo_card',
    state: 'open',
    lane: null,
    question: `An agent asks you to connect the private repository ${REPO}, so it can work on it in an environment.`,
    options: null,
    answer: null,
    asked_by: { kind: 'agent', id: 'claude-code' } as Principal,
    created_at: new Date(now - 60_000).toISOString(),
    expires_at: new Date(now + 20 * 3_600_000).toISOString(),
    answered_at: null,
    answered_by: null,
    repo_connect: {
      repo: REPO,
      host: 'github.com',
      reason,
      token_url: 'https://github.com/settings/personal-access-tokens/new',
      classic_token_url: 'https://github.com/settings/tokens/new?scopes=repo&description=openajar',
      deploy_keys_url: 'https://github.com/acme/private-app/settings/keys/new',
    },
  };
  org().questions = [...org().questions, card];
  return card;
}

const steppedUp = () => {
  db.stepUpUntil = new Date(Date.now() + 600_000).toISOString();
};
const panel = () => within(screen.getByRole('region', { name: 'Selected item' }));

async function openCard() {
  const view = renderApp(PATH);
  await screen.findByRole('heading', { name: 'Needs you', level: 1 });
  await view.user.click(await screen.findByRole('button', { name: new RegExp(`Connect ${REPO}`) }));
  await screen.findByRole('heading', { level: 2, name: `Connect ${REPO}` });
  return view;
}

/** The person pastes the token on the computer's page, and GitHub takes it: the mock's trip settles on its next read. */
function pasteOnTheComputersPage() {
  for (const trip of Object.values(org().trips)) if (trip.kind === 'repo_token') trip.opened = 0;
}

describe('a repository card', () => {
  afterEach(() => vi.restoreAllMocks());

  it('says what a token needs and links to GitHub, and has no field for one', async () => {
    addCard();
    await openCard();
    expect(panel().getByText(/classic token needs the repo scope/)).toBeInTheDocument();
    expect(panel().getByText(/Contents: Read \(Read and write to push\)/)).toBeInTheDocument();
    expect(panel().getByText(/you do not need to be an admin of the repository/)).toBeInTheDocument();
    expect(panel().getByRole('link', { name: /Make a fine-grained token/ })).toHaveAttribute('href', 'https://github.com/settings/personal-access-tokens/new');
    expect(panel().getByRole('link', { name: /or a classic one/ })).toHaveAttribute('href', 'https://github.com/settings/tokens/new?scopes=repo&description=openajar');
    expect(panel().getByRole('button', { name: 'Paste a token' })).toBeInTheDocument();
    expect(panel().queryByLabelText('Token')).not.toBeInTheDocument();
    expect(document.querySelector('input[type="password"]')).toBeNull();
    expect(panel().getByText(/Nothing is rented while it waits/)).toBeInTheDocument();
    expect(document.body.textContent).not.toContain(String.fromCharCode(0x2014));
  });

  it('asks the owner to confirm it is them first, the usual way', async () => {
    const open = vi.spyOn(window, 'open').mockReturnValue(null);
    addCard();
    const { user } = await openCard();
    await user.click(panel().getByRole('button', { name: 'Paste a token' }));
    const dialog = await screen.findByRole('dialog', { name: /Confirm it is you/ });
    expect(within(dialog).getByText(/A token opens your code to this computer/)).toBeInTheDocument();
    expect(open).not.toHaveBeenCalled();
    expect(org().questions.some((item) => item.id === 'ap_repo_card')).toBe(true);
  });

  it('opens the computer\'s own page for the token, waits on it, and shows connected with the expiry', async () => {
    const open = vi.spyOn(window, 'open').mockReturnValue(null);
    steppedUp();
    addCard();
    const { user } = await openCard();
    await user.click(panel().getByRole('button', { name: 'Paste a token' }));
    expect(await panel().findByRole('status')).toHaveTextContent('Paste the token on this computer\'s own page, in a new tab. This card moves on by itself.');
    const [url] = open.mock.calls[0] ?? [];
    expect(String(url)).toMatch(/^\/api\/forms\/[a-z0-9]+$/);
    expect(panel().getByRole('link', { name: /Open the token page/ })).toHaveAttribute('href', String(url));
    expect(panel().queryByRole('button', { name: 'Paste a token' })).not.toBeInTheDocument();
    expect(org().questions.some((item) => item.id === 'ap_repo_card')).toBe(true);
    pasteOnTheComputersPage();
    expect(await panel().findByText(`Connected. The agent can open ${REPO} now.`, undefined, { timeout: 5_000 })).toBeInTheDocument();
    expect(panel().getByText(/GitHub says this token expires on/)).toBeInTheDocument();
    expect(org().questions.some((item) => item.id === 'ap_repo_card')).toBe(false);
    await user.click(panel().getByRole('button', { name: 'Done' }));
    await waitFor(() => expect(screen.queryByRole('button', { name: new RegExp(`Connect ${REPO}`) })).not.toBeInTheDocument());
  });

  it('says so when the page closed before a token was connected, and opens a new one', async () => {
    const open = vi.spyOn(window, 'open').mockReturnValue(null);
    steppedUp();
    addCard();
    const { user } = await openCard();
    await user.click(panel().getByRole('button', { name: 'Paste a token' }));
    await panel().findByRole('status');
    for (const trip of Object.values(org().trips)) {
      trip.stage = 'ended';
      trip.form_url = null;
    }
    expect(await panel().findByRole('alert', undefined, { timeout: 5_000 })).toHaveTextContent('That page closed before a token was connected. Nothing was kept. Open a new one.');
    await user.click(panel().getByRole('button', { name: 'Paste a token' }));
    await waitFor(() => expect(open).toHaveBeenCalledTimes(2));
    expect(org().questions.some((item) => item.id === 'ap_repo_card')).toBe(true);
  });

  it('offers the deploy key to a repository admin, and checks when they added it', async () => {
    addCard();
    const { user } = await openCard();
    await user.click(panel().getByRole('button', { name: 'Use a deploy key instead (repo admins)' }));
    expect(await panel().findByText('ssh-ed25519')).toBeInTheDocument();
    expect(panel().getByRole('button', { name: 'Copy key' })).toBeInTheDocument();
    expect(panel().getByRole('link', { name: /deploy keys/ })).toHaveAttribute('href', 'https://github.com/acme/private-app/settings/keys/new');
    await user.click(panel().getByRole('button', { name: 'I added it, check' }));
    expect(await panel().findByRole('alert')).toHaveTextContent(/^Not yet:/);
    await user.click(panel().getByRole('button', { name: 'I added it, check' }));
    expect(await panel().findByText(`Connected. The agent can open ${REPO} now.`)).toBeInTheDocument();
    expect(panel().queryByText(/expires on/)).not.toBeInTheDocument();
  });

  it('says a new token is needed when the old one stopped working, and can wait for later', async () => {
    addCard('refused');
    const view = renderApp(PATH);
    await view.user.click(await screen.findByRole('button', { name: new RegExp(`Connect ${REPO}`) }));
    expect(await screen.findByRole('heading', { level: 2, name: `Give a new token for ${REPO}` })).toBeInTheDocument();
    expect(panel().getByText(/The token this computer had for it stopped working/)).toBeInTheDocument();
    await view.user.click(panel().getByRole('button', { name: 'Not now' }));
    await waitFor(() => expect(org().questions.some((item) => item.id === 'ap_repo_card')).toBe(false));
  });

  it('is the owner\'s alone: anyone else is told so and cannot open the page', async () => {
    org().role = 'admin';
    addCard();
    await openCard();
    expect(panel().getByText('Only the owner of this computer can connect a repository.')).toBeInTheDocument();
    expect(panel().queryByRole('button', { name: 'Paste a token' })).not.toBeInTheDocument();
  });
});
