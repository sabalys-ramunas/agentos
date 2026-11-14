import { http as mock, HttpResponse } from 'msw';
import { server } from '@/test/setup';
import { fleet, invites, path } from './client';

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
