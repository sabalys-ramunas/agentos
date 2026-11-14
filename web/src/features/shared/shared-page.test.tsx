import { render, screen, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { proof } from '@/api/client';
import { db } from '@/mocks/handlers';
import { SharedPage, tokenFromHash } from './shared-page';

const ORG = 'northwind-group';

/** Opened with no sign-in: the token in the fragment is the only credential. */
function open(hash: string) {
  window.location.hash = hash;
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <SharedPage />
    </QueryClientProvider>,
  );
}

async function tokenOfNewLink(): Promise<string> {
  const lane = db.orgs.get(ORG)!.activity[0]!.lane;
  const day = db.orgs.get(ORG)!.activity[0]!.at.slice(0, 10);
  const made = await proof.createShare(ORG, { lane, day, ttl: '7d' });
  return made.token;
}

describe('a read-only link from Proof', () => {
  it('reads the token from the fragment only', () => {
    expect(tokenFromHash('#token=shr_abc')).toBe('shr_abc');
    expect(tokenFromHash('#x=1&token=shr_abc')).toBe('shr_abc');
    expect(tokenFromHash('')).toBe('');
    expect(tokenFromHash('#token=%E0%A4%A')).toBe('');
  });

  it('shows one day of an account to someone with no sign-in, and each step as text', async () => {
    const token = await tokenOfNewLink();
    open(`#token=${token}`);
    expect(await screen.findByRole('heading', { name: /^What .+ did on / })).toBeInTheDocument();
    expect(screen.getByText(/Read-only\. The owner shared this day with you\./)).toBeInTheDocument();
    const steps = await screen.findAllByRole('listitem');
    expect(steps.length).toBeGreaterThan(0);
    expect(within(steps[0]!).getByText(/Signed|Not signed yet|Before the log began|Changed/)).toBeInTheDocument();
    // Nothing a signed-in person sees is here: no controls, no arguments, no ids.
    expect(screen.queryByRole('button', { name: /Share|Stop/ })).toBeNull();
    expect(screen.queryByText(/gr_\d+|tr_/)).toBeNull();
  });

  it('says plainly when a link is not valid, and does not offer to try again', async () => {
    open('#token=shr_nobody_made_this');
    expect(await screen.findByRole('heading', { name: 'This link is not valid' })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull();
  });

  it('stops working when the owner stops the link', async () => {
    const token = await tokenOfNewLink();
    const share = (await proof.shares(ORG)).find((item) => item.state === 'active')!;
    await proof.revokeShare(ORG, share.id);
    open(`#token=${token}`);
    expect(await screen.findByRole('heading', { name: 'This link is not valid' })).toBeInTheDocument();
  });

  it('says nothing when there is no token at all', () => {
    open('');
    expect(screen.getByRole('heading', { name: 'This link is not valid' })).toBeInTheDocument();
  });
});
