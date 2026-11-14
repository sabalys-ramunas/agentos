import { screen, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { renderApp } from '@/test/render';
import { asComputer } from '@/test/computer';
import { server } from '@/test/setup';

const ORG = '/northwind-group';
const SOON = () => new Date(Date.now() + 15 * 60_000).toISOString();

/** A form link as the node answers it, for the token given. */
const link = (token: string) => ({ url: `http://localhost:7400/api/mail/form/${token}`, expires_at: SOON(), computer: 'home-mini', token });

describe('connecting a mailbox from the computer', () => {
  it('has no concept shortcuts on a live computer', async () => {
    asComputer();
    renderApp(`${ORG}/email/connect?address=ola%40nordlys-post.example&stage=found&mock=still`);
    await screen.findByRole('heading', { name: 'Please look at these two names' });
    expect(screen.queryByRole('complementary', { name: 'Concept shortcuts' })).toBeNull();
    expect(screen.queryByText('Concept only: see another outcome')).toBeNull();
  });

  it('sends the server names a person typed to the node\'s form, with the usual ports when nothing was found', async () => {
    asComputer();
    const sent: unknown[] = [];
    server.use(
      http.post('/api/orgs/:org/email/form-links', async ({ request }) => {
        sent.push(await request.json());
        return HttpResponse.json(link('tok-typed'));
      }),
    );
    const { user } = renderApp(`${ORG}/email/connect?address=hello%40lindqvist-yachts.example&stage=found&mock=still`);
    await user.click(await screen.findByRole('button', { name: 'Type the server names' }));
    await user.type(screen.getByPlaceholderText('imap.yourmailcompany.com'), 'imap.lindqvist.example');
    await user.type(screen.getByPlaceholderText('smtp.yourmailcompany.com'), 'smtp.lindqvist.example');
    await user.click(screen.getByRole('button', { name: 'Use these' }));
    await user.click(await screen.findByRole('button', { name: /Open the form on/ }));
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(sent[0]).toMatchObject({
      address: 'hello@lindqvist-yachts.example',
      imap: { host: 'imap.lindqvist.example', port: 993, security: 'tls' },
      smtp: { host: 'smtp.lindqvist.example', port: 465, security: 'tls' },
    });
  });

  it('says the link has ended when the node\'s form closes with no mailbox, and offers a new one', async () => {
    asComputer();
    server.use(
      http.post('/api/orgs/:org/email/form-links', () => HttpResponse.json(link('tok-gone'))),
      http.get('/api/orgs/:org/email/form-links/:token/state', () => HttpResponse.json({ state: 'ended' })),
    );
    const { user } = renderApp(`${ORG}/email/connect?address=ola%40nordlys-post.example&stage=hand-over&mock=still`);
    await user.click(await screen.findByRole('button', { name: /Open the form on/ }));
    expect(await screen.findByRole('heading', { name: 'This link has ended' }, { timeout: 5_000 })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Start again' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Back' })).toBeInTheDocument();
    expect(screen.queryByText(/Now on its way/)).toBeNull();
  });

  it('moves on to the two checks once the mailbox shows up, while the form is still waiting', async () => {
    asComputer();
    server.use(
      http.post('/api/orgs/:org/email/form-links', () => HttpResponse.json(link('tok-open'))),
      http.get('/api/orgs/:org/email/form-links/:token/state', () => HttpResponse.json({ state: 'open', expires_at: SOON() })),
      http.get('/api/orgs/:org/email/mailboxes', () =>
        HttpResponse.json([{ id: 'northwind/nordlys', address: 'ola@nordlys-post.example', site: 'nordlys-post.example', provider: null, workspace: 'northwind' }]),
      ),
    );
    const { user } = renderApp(`${ORG}/email/connect?address=ola%40nordlys-post.example&stage=hand-over&mock=still`);
    await user.click(await screen.findByRole('button', { name: /Open the form on/ }));
    expect(await screen.findByText('Can it read new mail?', undefined, { timeout: 5_000 })).toBeInTheDocument();
  });

  it('cancels the node\'s form when the person stops waiting for it', async () => {
    asComputer();
    const cancelled: string[] = [];
    server.use(
      http.post('/api/orgs/:org/email/form-links', () => HttpResponse.json(link('tok-cancel'))),
      http.get('/api/orgs/:org/email/form-links/:token/state', () => HttpResponse.json({ state: 'open', expires_at: SOON() })),
      http.delete('/api/orgs/:org/email/form-links/:token', ({ params }) => {
        cancelled.push(String(params.token));
        return HttpResponse.json({ state: 'ended' });
      }),
    );
    const { user } = renderApp(`${ORG}/email/connect?address=ola%40nordlys-post.example&stage=hand-over&mock=still`);
    await user.click(await screen.findByRole('button', { name: /Open the form on/ }));
    await user.click(await screen.findByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(cancelled).toEqual(['tok-cancel']));
    expect(await screen.findByRole('button', { name: /Open the form on/ })).toBeInTheDocument();
  });
});
