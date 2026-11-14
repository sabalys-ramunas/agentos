import { screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { mockAuth } from '@/mocks/scenario';
import { renderApp } from '@/test/render';
import { server } from '@/test/setup';

// No wait in tests. What matters here is what the resend sends, not the countdown.
vi.mock('./cooldown', () => ({ COOLDOWN_SECONDS: 0 }));

afterEach(() => {
  mockAuth.signedIn = true;
});

describe('auth', () => {
  it('redirects to sign in without a session', async () => {
    mockAuth.signedIn = false;
    const { router } = renderApp('/northwind-group');
    expect(await screen.findByRole('heading', { name: 'Sign in to agentos' })).toBeInTheDocument();
    expect(router.state.location.pathname).toBe('/sign-in');
  });

  it('announces a failed send, and does not leave the page', async () => {
    mockAuth.signedIn = false;
    server.use(http.post('/api/auth/magic-link', () => new HttpResponse('Bad gateway', { status: 502 })));
    const { user, router } = renderApp('/sign-in');
    await user.type(await screen.findByLabelText('Email'), 'a@b.example');
    await user.click(screen.getByRole('button', { name: 'Continue with email' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('The service did not answer');
    expect(router.state.location.pathname).toBe('/sign-in');
  });

  it('drops a next that points at another site', async () => {
    mockAuth.signedIn = false;
    const { router } = renderApp('/sign-in?next=//evil.example');
    await screen.findByRole('heading', { name: 'Sign in to agentos' });
    expect(router.state.location.search).toEqual({});
  });

  it('never asks for a third-party password', async () => {
    mockAuth.signedIn = false;
    renderApp('/sign-in');
    await screen.findByRole('heading', { name: 'Sign in to agentos' });
    expect(document.querySelector('input[type="password"]')).toBeNull();
  });

  it('asks for an email, not a work email, and no checkbox: the policy line says continuing is agreeing', async () => {
    mockAuth.signedIn = false;
    renderApp('/sign-up');
    expect(await screen.findByRole('heading', { name: 'Give your AI a computer of its own' })).toBeInTheDocument();
    expect(screen.getByLabelText('Email')).toBeInTheDocument();
    expect(screen.queryByLabelText(/Work email/)).not.toBeInTheDocument();
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
    expect(screen.getByText(/By continuing you agree to the/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Continue with email' })).toBeEnabled();
  });

  it('sends aup_accepted with the sign-up', async () => {
    mockAuth.signedIn = false;
    let body: unknown;
    server.use(
      http.post('/api/auth/magic-link', async ({ request }) => {
        body = await request.json();
        return new HttpResponse(null, { status: 202 });
      }),
    );
    const { user } = renderApp('/sign-up');
    await user.type(await screen.findByLabelText('Email'), 'a@b.example');
    await user.click(screen.getByRole('button', { name: 'Continue with email' }));
    expect(await screen.findByRole('heading', { name: 'Check your email' })).toBeInTheDocument();
    expect(body).toEqual({ email: 'a@b.example', aup_accepted: true });
  });

  it('posts the token once, so a double render cannot burn it', async () => {
    mockAuth.signedIn = false;
    let calls = 0;
    server.use(
      http.post('/api/auth/magic-link/verify', () => {
        calls += 1;
        return HttpResponse.json({ error: { code: 'expired', message: 'Expired.' } }, { status: 410 });
      }),
    );
    renderApp('/auth/verify?token=abc');
    expect(await screen.findByRole('heading', { name: 'This link is no longer valid' })).toBeInTheDocument();
    expect(calls).toBe(1);
  });

  it('gives every bad link one answer, because the API does', async () => {
    mockAuth.signedIn = false;
    renderApp('/auth/verify?token=expired');
    expect(await screen.findByRole('heading', { name: 'This link is no longer valid' })).toBeInTheDocument();
  });

  it('confirms a step-up link instead of treating it as a sign-in', async () => {
    let body: unknown;
    server.use(
      http.post('/api/auth/step-up/verify', async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ user: {}, csrf: 'x', step_up_until: null, expires_at: '', orgs: [] });
      }),
    );
    const { router } = renderApp('/auth/verify?token=abc&step_up=1');
    expect(await screen.findByRole('heading', { name: 'Confirmed' })).toBeInTheDocument();
    expect(body).toEqual({ token: 'abc' });
    // The token leaves the address bar (security review 15).
    expect(router.state.location.href).toBe('/auth/verify?step_up=1');
  });

  it('never follows a next from the server that leaves the panel', async () => {
    mockAuth.signedIn = false;
    server.use(
      http.post('/api/auth/magic-link/verify', () =>
        HttpResponse.json({ user: { id: 'u' }, csrf: 'x', step_up_until: null, expires_at: '', orgs: [], next: '/\\evil.example' }),
      ),
    );
    const { router } = renderApp('/auth/verify?token=abc&next=/northwind-group/needs-you');
    await waitFor(() => expect(router.state.location.pathname).not.toBe('/auth/verify'));
    expect(router.state.location.href).not.toContain('evil');
  });

  it('says so when a step-up link is opened in another browser', async () => {
    mockAuth.signedIn = false;
    renderApp('/auth/verify?token=abc&step_up=1');
    expect(await screen.findByRole('heading', { name: 'Open this in the browser you started in' })).toBeInTheDocument();
  });

  it('explains every Google failure the API can send', async () => {
    mockAuth.signedIn = false;
    renderApp('/sign-in?error=email_unverified');
    expect(await screen.findByRole('alert')).toHaveTextContent('Google has not verified');
  });

  it('sends a new Google user to accept the policy first', async () => {
    mockAuth.signedIn = false;
    renderApp('/sign-in?error=aup_required');
    expect(await screen.findByRole('link', { name: 'Create one' })).toBeInTheDocument();
  });

  it('sends Google sign-up with the policy accepted, as the line under it says', async () => {
    mockAuth.signedIn = false;
    renderApp('/sign-up?next=/invite/abc');
    const link = await screen.findByRole('link', { name: 'Continue with Google' });
    expect(link).toHaveAttribute('href', '/api/auth/google/start?aup_accepted=true&next=%2Finvite%2Fabc');
  });

  it('hides Google when the API says it is off', async () => {
    mockAuth.signedIn = false;
    server.use(http.get('/api/auth/providers', () => HttpResponse.json({ magic_link: true, google: false, passkey: true })));
    renderApp('/sign-in');
    await screen.findByRole('heading', { name: 'Sign in to agentos' });
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByRole('link', { name: 'Continue with Google' })).not.toBeInTheDocument();
  });

  it('keeps the policy acceptance on a resend', async () => {
    mockAuth.signedIn = false;
    let body: unknown;
    server.use(
      http.post('/api/auth/magic-link', async ({ request }) => {
        body = await request.json();
        return HttpResponse.json({ message: 'ok' }, { status: 202 });
      }),
    );
    const { user } = renderApp('/check-email?email=a%40b.example&aup=true');
    await user.click(await screen.findByRole('button', { name: 'Resend the link' }));
    await waitFor(() => expect(body).toEqual({ email: 'a@b.example', aup_accepted: true }));
  });

  it('explains an expired invite', async () => {
    renderApp('/invite/expired');
    expect(await screen.findByRole('heading', { name: 'This invite has expired' })).toBeInTheDocument();
  });

  it("takes a computer's invite with a passkey, never by email", async () => {
    mockAuth.signedIn = false;
    let asked = false;
    server.use(
      http.get('/api/auth/providers', () => HttpResponse.json({ magic_link: false, google: false, passkey: true, code: true })),
      http.get('/api/invites/:token', () =>
        HttpResponse.json({ org_name: 'Aster Coffee', role: 'member', inviter: 'Mara', business: 'Aster Coffee', workspaces: null, join_by: 'passkey' }),
      ),
      http.post('/api/invites/:token/passkey/options', () => {
        asked = true;
        return HttpResponse.json({ error: { code: 'unauthorized', message: 'Not in this test.' } }, { status: 401 });
      }),
    );
    const { user } = renderApp('/invite/computer-token');
    expect(await screen.findByRole('button', { name: 'Join Aster Coffee' })).toBeInTheDocument();
    expect(screen.queryByLabelText(/email/i)).not.toBeInTheDocument();
    expect(screen.getByText(/Your device makes a passkey/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Join Aster Coffee' }));
    await waitFor(() => expect(asked).toBe(true));
  });

  it('accepts an invite while signed in, and opens what an approver will be asked', async () => {
    const { user, router } = renderApp('/invite/good-token');
    expect(await screen.findByRole('heading', { name: 'Mara asked you to help with Aster Coffee' })).toBeInTheDocument();
    await user.click(await screen.findByRole('button', { name: 'Join Aster Coffee' }));
    await waitFor(() => expect(router.state.location.pathname).toBe('/northwind-group/welcome'));
  });

  it('joins in one click for a new person: the link is the sign-in', async () => {
    mockAuth.signedIn = false;
    let body: unknown;
    server.use(
      http.post('/api/invites/:token/join', async ({ request }) => {
        body = await request.json();
        mockAuth.signedIn = true;
        return HttpResponse.json({ user: { id: 'u', email: 'p@x.example', name: 'Priya Shah', email_verified: true, has_passkey: false }, csrf: 'x', step_up_until: null, expires_at: '', orgs: [{ id: 'o', slug: 'northwind-group', name: 'Northwind Group', role: 'approver', plan: 'free', needs_you: 2 }], org: 'northwind-group' });
      }),
    );
    const { user, router } = renderApp('/invite/good-token');
    expect(await screen.findByText(/No password\. This button signs you in\./)).toBeInTheDocument();
    expect(screen.queryByLabelText(/email/i)).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Join Aster Coffee' }));
    await waitFor(() => expect(router.state.location.pathname).toBe('/northwind-group/welcome'));
    expect(body).toEqual({ aup_accepted: true });
    expect(await screen.findByRole('heading', { name: /Hi Priya\. You're helping Mara with/ })).toBeInTheDocument();
    expect(screen.getByText('Can this go out?')).toBeInTheDocument();
    expect(screen.getByText('Can it spend money?')).toBeInTheDocument();
    expect(screen.getByText('A quick question')).toBeInTheDocument();
  });

  it('sends someone who already has an account to sign in, with the address the invite went to', async () => {
    mockAuth.signedIn = false;
    server.use(
      http.get('/api/invites/:token', () =>
        HttpResponse.json({ org_name: 'Northwind Group', role: 'approver', inviter: 'Mara Lindqvist', inviter_first_name: 'Mara', business: 'Aster Coffee', workspaces: ['Aster Coffee'], email_hint: 'pr***@example.com', one_click: false, expires_at: '' }),
      ),
    );
    let body: unknown;
    server.use(
      http.post('/api/auth/magic-link', async ({ request }) => {
        body = await request.json();
        return new HttpResponse(null, { status: 202 });
      }),
    );
    const { user } = renderApp('/invite/good-token');
    expect(await screen.findByText(/pr\*\*\*@example\.com/)).toBeInTheDocument();
    await user.type(screen.getByLabelText('Your email'), 'priya@example.com');
    await user.click(screen.getByRole('button', { name: 'Email me a link to join' }));
    expect(await screen.findByRole('heading', { name: 'Check your email' })).toBeInTheDocument();
    expect(body).toEqual({ email: 'priya@example.com', aup_accepted: true, next: '/invite/good-token' });
  });

  it('shows why an invite lapsed when its sender lost the right to send it', async () => {
    mockAuth.signedIn = false;
    const message = 'This invite no longer works, because the person who sent it can no longer invite at that role. Ask for a new one.';
    server.use(http.get('/api/invites/:token', () => HttpResponse.json({ error: { code: 'expired', message } }, { status: 410 })));
    renderApp('/invite/lapsed-token');
    expect(await screen.findByRole('heading', { name: 'This invite no longer works' })).toBeInTheDocument();
    expect(screen.getByText(message)).toBeInTheDocument();
  });
});

describe('Google linking', () => {
  it('explains a Google sign-in that needs linking first', async () => {
    mockAuth.signedIn = false;
    renderApp('/sign-in?error=google_link_required');
    expect(await screen.findByRole('alert')).toHaveTextContent('This email already has an account');
  });
});
