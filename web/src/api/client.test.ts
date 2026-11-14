import { http as mock, HttpResponse } from 'msw';
import { server } from '@/test/setup';
import { connectors, engage, fleet, invites, path, surfaces } from './client';

describe('path segments (security review 13)', () => {
  it('encodes every value as one segment', () => {
    expect(path`/api/invites/${'../../x'}`).toBe('/api/invites/..%2F..%2Fx');
    expect(path`/api/orgs/${'a?b=c#d'}/lanes/${'ws'}/${'n/../m'}`).toBe('/api/orgs/a%3Fb%3Dc%23d/lanes/ws/n%2F..%2Fm');
  });

  it('sends a token from the address bar as one segment, never a climb', async () => {
    const seen: string[] = [];
    server.use(
      mock.get('*', ({ request }) => {
        seen.push(new URL(request.url).pathname);
        return HttpResponse.json({ org_name: 'x', role: 'viewer', inviter: 'y' });
      }),
    );
    await invites.preview('../../orgs/northwind-group');
    expect(seen).toEqual(['/api/invites/..%2F..%2Forgs%2Fnorthwind-group']);
  });

  it.each(['..', '.', '%2e%2E', ''])('refuses %j as a segment, because a URL would climb with it', (value) => {
    expect(() => path`/api/invites/${value}`).toThrow('That address is not valid.');
    expect(() => fleet.lane('northwind-group', value, 'approvals')).toThrow(expect.objectContaining({ code: 'invalid' }));
  });
});

describe('notification settings on the wire', () => {
  it("never sends the org's zone back as the person's own choice", async () => {
    const { notificationBody } = await import('./client');
    const read = {
      timezone: 'Europe/Vilnius',
      timezone_source: 'org' as const,
      digest: { time: '08:00', timezone: 'Europe/Vilnius', label: '08:00, Vilnius time' },
      push: true,
      email_digest: 'daily' as const,
      email_urgent: true,
      quiet_hours: { enabled: false, from: '22:00', to: '07:00', timezone: 'Europe/Vilnius', urgent_breaks_through: true },
      batch_minutes: 5,
    };
    expect(notificationBody(read)).not.toHaveProperty('timezone');
    expect(notificationBody(read)).not.toHaveProperty('digest');
    expect(notificationBody({ ...read, timezone_source: 'person' })).toHaveProperty('timezone', 'Europe/Vilnius');
  });
});

describe('engagement, surfaces and connectors on the wire', () => {
  type Sent = { method: string; path: string; search: string; body: unknown };

  /** Every call the screens make, recorded as the node would read it: one route and one body each. */
  async function recorded(run: () => Promise<unknown>): Promise<Sent[]> {
    const seen: Sent[] = [];
    server.use(
      mock.all('*', async ({ request }) => {
        const url = new URL(request.url);
        const body = request.method === 'GET' || request.method === 'DELETE' ? null : await request.clone().json();
        seen.push({ method: request.method, path: url.pathname, search: url.search, body });
        return HttpResponse.json({});
      }),
    );
    await run();
    return seen;
  }

  it('sends each program, limit, hold and item write to its own route, with the body the node reads', async () => {
    const sent = await recorded(async () => {
      await engage.approveProgram('local', 'biz-b-ig-bakery', 2);
      await engage.approveProgram('local', 'biz-b-ig-bakery');
      await engage.setCap('local', 'biz-a', 'social', 'like', 3);
      await engage.setCap('local', 'biz-a', 'social', 'like', null);
      await engage.clearHolds('local', 'biz-a', 'social', { id: 4, kind: null });
      await engage.withdrawItem('local', 'it_4a10d9');
    });
    expect(sent).toEqual([
      { method: 'POST', path: '/api/orgs/local/engage/programs/biz-b-ig-bakery/approve', search: '', body: { version: 2 } },
      { method: 'POST', path: '/api/orgs/local/engage/programs/biz-b-ig-bakery/approve', search: '', body: {} },
      { method: 'PUT', path: '/api/orgs/local/engage/caps/biz-a/social/like', search: '', body: { value: 3 } },
      { method: 'PUT', path: '/api/orgs/local/engage/caps/biz-a/social/like', search: '', body: { value: null } },
      { method: 'POST', path: '/api/orgs/local/engage/holds/biz-a/social/clear', search: '', body: { id: 4, kind: null } },
      { method: 'POST', path: '/api/orgs/local/engage/items/it_4a10d9/withdraw', search: '', body: {} },
    ]);
  });

  it('reads the day, the account, the program and its items, and passes the day on as it was asked', async () => {
    const sent = await recorded(async () => {
      await engage.digest('local', '2026-11-07');
      await engage.account('local', 'biz-a', 'social', '2026-11-06');
      await engage.program('local', 'biz-a', 'social');
      await engage.items('local', 'biz-b', 'forum', 'queued');
    });
    expect(sent.map(({ method, path, search }) => [method, path, search])).toEqual([
      ['GET', '/api/orgs/local/engage/digest', '?day=2026-11-07'],
      ['GET', '/api/orgs/local/engage/digest/biz-a/social', '?day=2026-11-06'],
      ['GET', '/api/orgs/local/engage/programs/biz-a/social', ''],
      ['GET', '/api/orgs/local/engage/items/biz-b/forum', '?status=queued'],
    ]);
  });

  it('adds, reads and removes a surface, and runs a connector on one', async () => {
    const sent = await recorded(async () => {
      await surfaces.list('local');
      await surfaces.get('local', 'biz-a', 'social');
      await surfaces.add('local', { id: 'biz-a/shop', site: 'instagram', lane: 'biz-a/shop', acts_as: { kind: 'ig_business', id: '17841400000002', name: '@biza.shop' } });
      await surfaces.remove('local', 'biz-a', 'shop');
      await connectors.list('local');
      await connectors.run('local', 'reddit.post_text', { on: 'harbor/reddit', params: { title: 'Bench' }, idempotency_key: 'k1' });
    });
    expect(sent).toEqual([
      { method: 'GET', path: '/api/orgs/local/surfaces', search: '', body: null },
      { method: 'GET', path: '/api/orgs/local/surfaces/biz-a/social', search: '', body: null },
      {
        method: 'POST', path: '/api/orgs/local/surfaces', search: '',
        body: { id: 'biz-a/shop', site: 'instagram', lane: 'biz-a/shop', acts_as: { kind: 'ig_business', id: '17841400000002', name: '@biza.shop' } },
      },
      { method: 'DELETE', path: '/api/orgs/local/surfaces/biz-a/shop', search: '', body: null },
      { method: 'GET', path: '/api/orgs/local/connectors', search: '', body: null },
      {
        method: 'POST', path: '/api/orgs/local/connectors/reddit.post_text/run', search: '',
        body: { on: 'harbor/reddit', params: { title: 'Bench' }, idempotency_key: 'k1' },
      },
    ]);
  });
});
