import { screen, waitFor, within } from '@testing-library/react';
import { fleet } from '@/api/client';
import { db } from '@/mocks/handlers';
import { renderApp } from '@/test/render';
import { asComputer } from '@/test/computer';

const ORG = 'northwind-group';
const ASTER = '/northwind-group/lanes/aster/instagram';
const data = () => db.orgs.get(ORG)!;
const lane = (id: string) => data().lanes.find((item) => item.id === id)!;

/** A member of the org: sees the account and what an agent did, and changes none of its settings. */
function asMember() {
  data().role = 'member';
}

/** The owner has signed in recently, so a step-up that asks for it is not needed. */
function recentSignIn() {
  db.stepUpUntil = new Date(Date.now() + 10 * 60_000).toISOString();
}

describe('posting pace', () => {
  it('answers first with what the account may do, and lets the owner change it', async () => {
    const { user } = renderApp(ASTER);
    expect(await screen.findByRole('heading', { name: 'Posting pace' })).toBeInTheDocument();
    expect(screen.getByText('Posts go out between 08:00 and 22:00, at least 45 minutes apart, 8 posts a day.')).toBeInTheDocument();
    expect(screen.getByText(/Posted \d+ of 8 today/)).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Change pace' }));
    const dialog = within(await screen.findByRole('dialog'));
    const perDay = dialog.getByLabelText('Posts a day');
    await user.clear(perDay);
    await user.type(perDay, '2');
    await user.click(dialog.getByRole('button', { name: 'Save the pace' }));

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(await screen.findByText('Posts go out between 08:00 and 22:00, at least 45 minutes apart, 2 posts a day.')).toBeInTheDocument();
  });

  it('shows a member the pace but no control to change it', async () => {
    asMember();
    renderApp(ASTER);
    expect(await screen.findByText('Posts go out between 08:00 and 22:00, at least 45 minutes apart, 8 posts a day.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Change pace' })).not.toBeInTheDocument();
  });

  it('is refused for anyone but the owner, whatever the page shows', async () => {
    asMember();
    await expect(fleet.setLanePace(ORG, 'aster', 'instagram', { pace: { commits_per_day: 9 } })).rejects.toMatchObject({ status: 403 });
    expect(data().account.pace['aster/instagram']?.commits_per_day).toBe(8);
  });
});

describe('limits', () => {
  it('shows the posts a day beside a bar that turns amber near its limit', async () => {
    renderApp(ASTER);
    const bar = await screen.findByRole('meter', { name: 'Posts a day, 5 of 6 posts today' });
    expect(bar.firstElementChild).toHaveClass('bg-warn-dot');
    expect(screen.getByText('5 of 6 posts today')).toBeInTheDocument();
    expect(screen.getByText('Near its limit')).toBeInTheDocument();
  });

  it('turns red once a limit is at 95 percent', async () => {
    data().account.limits['aster/instagram'] = { commits_per_day: 5 };
    renderApp(ASTER);
    const bar = await screen.findByRole('meter', { name: 'Posts a day, 5 of 5 posts today' });
    expect(bar.firstElementChild).toHaveClass('bg-bad-dot');
    expect(screen.getByText('Paused until it resets')).toBeInTheDocument();
  });

  it('is refused for anyone but the owner', async () => {
    asMember();
    await expect(fleet.setLaneLimits(ORG, 'aster', 'instagram', { commits_per_day: 2 })).rejects.toMatchObject({ status: 403 });
    expect(data().account.limits['aster/instagram']).toEqual({ commits_per_day: 6 });
  });
});

describe('taint', () => {
  it('says an agent read a page, so its next post asks first, and the owner can clear it', async () => {
    recentSignIn();
    const { user } = renderApp(ASTER);
    expect(await screen.findByText('An agent read a page at coffee-reviews.example, so its next post asks you first.')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Clear it' }));
    const dialog = within(await screen.findByRole('dialog'));
    await user.type(dialog.getByRole('textbox'), 'clear');
    await user.click(dialog.getByRole('button', { name: 'Clear it' }));
    await waitFor(() => expect(screen.queryByText(/An agent read a page/)).not.toBeInTheDocument());
    expect(data().account.taint['aster/instagram']).toBeUndefined();
  });

  it('tells a member it asks a person first, with nothing to clear', async () => {
    asMember();
    renderApp(ASTER);
    expect(await screen.findByText('An agent read a page at coffee-reviews.example, so its next post asks a person first.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Clear it' })).not.toBeInTheDocument();
  });

  it('is refused for anyone but the owner, and the node holds the taint', async () => {
    recentSignIn();
    const grant = data().account.taint['aster/instagram']!.grant;
    asMember();
    await expect(fleet.clearTaint(ORG, grant)).rejects.toMatchObject({ status: 403 });
    expect(data().account.taint['aster/instagram']).toBeDefined();
  });
});

describe('who is using the account', () => {
  it('says who holds it, and who waits behind', async () => {
    renderApp(ASTER);
    expect(await screen.findByText('Claude Code holds the account, for post the morning photo.')).toBeInTheDocument();
    expect(screen.getByText('One thing waits: the weekly posts routine.')).toBeInTheDocument();
  });

  it('is not shown for an account nobody holds or waits for', async () => {
    renderApp(`/northwind-group/lanes/${lane('aster/linkedin').workspace}/${lane('aster/linkedin').name}`);
    await screen.findByRole('heading', { name: 'Sites' });
    expect(screen.queryByRole('heading', { name: 'Who is using it' })).not.toBeInTheDocument();
  });
});

describe('the account page, read calmly', () => {
  it('keeps the less used settings behind one closed disclosure, and every one of them reachable', async () => {
    const { user } = renderApp(ASTER);
    await waitFor(() => expect(document.querySelector('details summary')).not.toBeNull());
    const disclosure = document.querySelector('details');
    expect(disclosure).not.toHaveAttribute('open');
    const summary = disclosure!.querySelector('summary')!;
    expect(summary).toHaveTextContent('Settings');
    await user.click(summary);
    expect(disclosure).toHaveAttribute('open');
    // The watch link only shows for an awake account, so this one (asleep) has the rest.
    for (const heading of ['Posting pace', 'Limits', 'Sites']) {
      expect(await screen.findByRole('heading', { name: heading })).toBeInTheDocument();
    }
  });

  it('says what the check found once, not again in its summary', async () => {
    renderApp(ASTER);
    const detail = 'A page can make WebRTC send UDP from the node\'s own address.';
    expect(await screen.findByText(detail)).toBeInTheDocument();
    expect(screen.getAllByText(detail)).toHaveLength(1);
  });
});

describe('the safe-to-use line', () => {
  it('says safe with a warning, in words, and what the check found', async () => {
    renderApp(ASTER);
    expect(await screen.findByText('Safe to use, with a warning')).toBeInTheDocument();
    expect(await screen.findByText('WebRTC can send this node\'s own IP through a blank frame. Normal pages stay behind the proxy.')).toBeInTheDocument();
  });

  it('is not shown for an account that is safe', async () => {
    renderApp(`/northwind-group/lanes/${lane('aster/linkedin').workspace}/${lane('aster/linkedin').name}`);
    await screen.findByRole('heading', { name: 'Sites' });
    expect(screen.queryByText(/Safe to use/)).not.toBeInTheDocument();
  });
});

describe('sites', () => {
  it('lets the owner add a site, and says a typed thing that is not one', async () => {
    recentSignIn();
    const { user } = renderApp(ASTER);
    expect(await screen.findByText('instagram.com')).toBeInTheDocument();
    const input = screen.getByRole('textbox', { name: 'Add a site' });
    await user.type(input, 'not a site');
    await user.click(screen.getByRole('button', { name: 'Add the site' }));
    expect(screen.getByRole('alert')).toHaveTextContent('Type a site as its address, such as instagram.com.');

    await user.clear(input);
    await user.type(input, 'example.org');
    await user.click(screen.getByRole('button', { name: 'Add the site' }));
    expect(await screen.findByText('example.org')).toBeInTheDocument();
    expect(data().account.sites['aster/instagram']?.sites).toContain('example.org');

    await user.click(screen.getByRole('button', { name: 'Remove example.org' }));
    await waitFor(() => expect(screen.queryByText('example.org')).not.toBeInTheDocument());
  });

  it('shows a member the list and nothing to change it', async () => {
    asMember();
    renderApp(ASTER);
    expect(await screen.findByText('instagram.com')).toBeInTheDocument();
    expect(screen.queryByRole('textbox', { name: 'Add a site' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Remove instagram.com' })).not.toBeInTheDocument();
  });

  it('is refused for anyone but the owner', async () => {
    asMember();
    await expect(fleet.setLaneSites(ORG, 'aster', 'instagram', { op: 'add', sites: ['evil.example'] })).rejects.toMatchObject({ status: 403 });
    expect(data().account.sites['aster/instagram']?.sites).toEqual(['instagram.com', 'frame:www.google.com']);
  });
});

describe('the sign-in upkeep', () => {
  it('says the sign-in is kept warm, and when it last ran', async () => {
    renderApp(ASTER);
    expect(await screen.findByText('Keeping it warm')).toBeInTheDocument();
    expect(screen.getByText(/Last visit 2h ago, and it stayed signed in\./)).toBeInTheDocument();
  });
});

describe('a watch link', () => {
  it('is made once for an awake account, shown with a copy button, and never kept', async () => {
    asComputer();
    const { user } = renderApp(ASTER);
    await user.click(await screen.findByRole('button', { name: 'Make a watch link' }));
    const link = await screen.findByRole('textbox', { name: 'Watch link' });
    expect(link).toHaveValue(`${window.location.origin}/view/aster/instagram?t=mock-view-link`);
    expect(screen.getByRole('button', { name: 'Copy the watch link' })).toBeInTheDocument();
    expect(screen.getByText(/It is shown once, so copy it now\./)).toBeInTheDocument();
  });

  it('is not offered for an account that is resting, since there is no screen to watch', async () => {
    const cold = data().lanes.find((item) => item.state === 'cold' && item.kind === 'browser')!;
    renderApp(`/northwind-group/lanes/${cold.workspace}/${cold.name}`);
    await screen.findByRole('heading', { name: 'Sites' });
    expect(screen.queryByRole('button', { name: 'Make a watch link' })).not.toBeInTheDocument();
  });
});
