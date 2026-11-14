/**
 * The mock of a member's devices (docs/API.md, Members): the passkeys a person signs in with and the
 * devices they are signed in on. An admin reads them and can end one; a member is refused, as the
 * node refuses them. Ending a device is kept for the rest of the session.
 */
import { http, HttpResponse } from 'msw';
import type { MemberDetail, MemberSession, Passkey } from '@/api/types';
import type { Helpers } from './s3';
import type { OrgData } from './seed';

const HOUR = 3_600_000;
const DAY = 86_400_000;
const endedBy = new WeakMap<OrgData, Set<string>>();

/** Two devices and a passkey for each person, so the list is never a single sad row. */
function devicesOf(member: { id: string }, index: number, ended: Set<string>): { passkeys: Passkey[]; sessions: MemberSession[] } {
  const now = Date.now();
  const sessions: MemberSession[] = [
    {
      id: `ses-${index}-phone`,
      label: 'iPhone, Safari',
      via: 'passkey',
      created_at: new Date(now - (index + 2) * DAY).toISOString(),
      last_seen_at: new Date(now - (index + 1) * HOUR).toISOString(),
      expires_at: new Date(now + 7 * DAY).toISOString(),
      current: false,
    },
    {
      id: `ses-${index}-laptop`,
      label: 'Laptop, Chrome',
      via: 'code',
      created_at: new Date(now - (index + 5) * DAY).toISOString(),
      last_seen_at: new Date(now - (index + 3) * HOUR).toISOString(),
      expires_at: new Date(now + 3 * DAY).toISOString(),
      current: false,
    },
  ].filter((session) => !ended.has(session.id));
  return {
    passkeys: [
      {
        id: `pk-${member.id}`,
        name: 'Phone passkey',
        created_at: new Date(now - 30 * DAY).toISOString(),
        last_used_at: new Date(now - (index + 1) * HOUR).toISOString(),
      },
    ],
    sessions,
  };
}

export function memberDeviceHandlers({ org, failure }: Pick<Helpers, 'org' | 'failure'>) {
  const endedOf = (record: OrgData) => {
    let set = endedBy.get(record);
    if (!set) endedBy.set(record, (set = new Set()));
    return set;
  };
  const adminOnly = () => failure(403, 'denied', 'Only an admin sees where a member is signed in.');

  return [
    http.get(
      '/api/orgs/:org/members/:id',
      org((record, { params }) => {
        if (record.role !== 'owner' && record.role !== 'admin') return adminOnly();
        const index = record.members.findIndex((item) => item.id === params.id);
        if (index < 0) return failure(404, 'not_found', 'No such member.');
        const member = record.members[index]!;
        const detail: MemberDetail = { ...member, devices: devicesOf(member, index, endedOf(record)) };
        return HttpResponse.json(detail);
      }),
    ),
    http.delete(
      '/api/orgs/:org/members/:id/devices/:device',
      org((record, { params }) => {
        if (record.role !== 'owner' && record.role !== 'admin') return adminOnly();
        const index = record.members.findIndex((item) => item.id === params.id);
        if (index < 0) return failure(404, 'not_found', 'No such member.');
        const device = String(params.device);
        const member = record.members[index]!;
        const known = devicesOf(member, index, new Set()).sessions.some((session) => session.id === device);
        if (!known) return failure(404, 'not_found', 'No such device.');
        endedOf(record).add(device);
        return HttpResponse.json({ ok: true, sessions_ended: 1 });
      }),
    ),
  ];
}
