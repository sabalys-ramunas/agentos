import { Link, useParams } from '@tanstack/react-router';
import { Check, MoreHorizontal, Smartphone, Trash2, UserPlus } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';
import {
  useCreateInvite,
  useAccess,
  useInvites,
  useMembers,
  useProviders,
  useRemoveMember,
  useRevokeInvite,
  useSession,
  useUpdateMember,
  useWorkspaces,
} from '@/api/hooks';
import type { Invite, Member, Role, Workspace } from '@/api/types';
import { Button, IconButton } from '@/components/ui/button';
import { Card, FormCard } from '@/components/ui/card';
import { ConfirmDialog } from '@/components/ui/dialog';
import { CopyField, Field, Input } from '@/components/ui/field';
import { Menu, MenuContent, MenuItem, MenuTrigger } from '@/components/ui/menu';
import { Avatar } from '@/components/ui/misc';
import { Select } from '@/components/ui/select';
import { Status } from '@/components/ui/status';
import { EmptyState, NoAccess, QueryView, Skeleton } from '@/components/ui/states';
import { CellTitle, mainCell, NoValue, Table, TableScroll, Td, Th, Tr } from '@/components/ui/table';
import { PersonMark } from '@/components/brand/identity';
import { PlanNotice, usePlanRoom } from '@/components/plan-notice';
import { useRunsAlone } from '@/features/nodes/add-node';
import { describeError, isPlanLimit } from '@/lib/errors';
import { plural, timeAgo } from '@/lib/format';
import type { StatusLabel } from '@/lib/labels';
import { roleHint, roleLabel } from '@/lib/labels';
import { atLeast, isOrgWide, ROLES } from '@/lib/permissions';
import { SettingsHeader } from './page';
import { MemberDevices } from './member-devices';

/** Only the roles this person may hand out: never above their own. */
export const roleOptions = (actor: Role) => ROLES.filter((value) => atLeast(actor, value)).map((value) => ({ value, label: roleLabel[value] }));

/** A person whose role differs between businesses: each business has its own, shown beside it. */
export function differsByWorkspace(member: Member): boolean {
  return new Set((member.roles_by_workspace ?? []).map((row) => row.role)).size > 1;
}

/** The role a person holds in one business, or their role where no business is named. */
export function roleIn(member: Member, workspace: string): Role {
  return member.roles_by_workspace?.find((row) => row.workspace === workspace)?.role ?? member.role;
}

function scopeLabel(scope: string[] | null, workspaces: Workspace[]) {
  if (scope === null) return 'All workspaces';
  return scope.map((id) => workspaces.find((item) => item.id === id)?.name ?? id).join(', ') || 'No workspaces';
}

export function TableSkeleton() {
  return (
    <div role="status" aria-label="Loading">
      {[0, 1, 2, 3].map((index) => (
        <div key={index} className="flex h-15 items-center gap-3 border-b border-line px-5 last:border-b-0">
          <Skeleton className="size-8 rounded-full" />
          <div className="grid gap-2">
            <Skeleton className="h-3 w-36" />
            <Skeleton className="h-3 w-48" />
          </div>
          <Skeleton className="ml-auto h-3 w-20" />
        </div>
      ))}
    </div>
  );
}

/** A person: their mark (violet to blue for you, their own hue for anyone else), name and second line. */
export function Person({ member, me, second }: { member: Member; me: boolean; second: string }) {
  return (
    <div className="flex min-w-0 items-center gap-3">
      {me ? (
        <Avatar name={member.user.name} me className="size-8 text-caption" />
      ) : (
        <PersonMark id={member.user.id} name={member.user.name} size={32} />
      )}
      <CellTitle
        title={
          <>
            {member.user.name}
            {me ? <span className="font-normal text-faint"> (you)</span> : null}
          </>
        }
        second={second}
      />
    </div>
  );
}

export function MembersSettings() {
  const { org } = useParams({ from: '/$org/settings/members' });
  const members = useMembers(org);
  const workspaces = useWorkspaces(org).data ?? [];
  const me = useSession().data?.user.id;
  const update = useUpdateMember(org);
  const remove = useRemoveMember(org);
  const [removing, setRemoving] = useState<Member | null>(null);
  const [devicesOf, setDevicesOf] = useState<Member | null>(null);
  // On a computer there is one org and no one to join it: its words say so.
  const alone = useRunsAlone(org);

  const access = useAccess(org);
  const canChange = (member: Member) =>
    access.can('members.update') && member.user.id !== me && atLeast(access.role, member.role);
  const canRemove = (member: Member) =>
    access.can('members.remove') && member.user.id !== me && atLeast(access.role, member.role);
  // An admin sees where someone is signed in, and ends one of those sessions. Their own devices live on their profile.
  const canSeeDevices = (member: Member) => access.can('members.update') && member.user.id !== me;

  // Plain text unless this person can really change it. Their own role stays out of reach too,
  // so nobody can demote themselves by accident.
  const roleSelect = (member: Member) =>
    differsByWorkspace(member) ? (
      // Each business's role is changed in Workspace access, beside the business.
      <span className="text-secondary text-muted">Differs by workspace</span>
    ) : canChange(member) ? (
      <Select
        size="sm"
        className="w-32"
        aria-label={`Role for ${member.user.name}`}
        value={member.role}
        options={roleOptions(access.role)}
        onValueChange={(role) => update.mutate({ id: member.id, role: role as Role }, { onSuccess: () => toast('Role updated') })}
      />
    ) : (
      <span className="text-body">{roleLabel[member.role]}</span>
    );

  return (
    <>
      <SettingsHeader
        title="Members"
        description={alone ? 'People who can use this computer, and what they can do.' : 'People in this org and what they can do.'}
        action={
          access.can('invites.read') ? (
            <Button asChild variant="primary">
              <Link to="/$org/settings/invites" params={{ org }}>
                <UserPlus className="size-4" />
                Invite someone
              </Link>
            </Button>
          ) : undefined
        }
      />
      <Card className="overflow-hidden">
        <QueryView query={members} skeleton={<TableSkeleton />}>
          {(items) => (
            <TableScroll>
              <Table density="relaxed">
                <thead className="hidden md:table-header-group">
                  <tr>
                    <Th>Member</Th>
                    <Th className="w-36">Role</Th>
                    <Th className="hidden w-44 lg:table-cell">Workspaces</Th>
                    <Th className="w-28">Last active</Th>
                    <Th className="w-14">
                      <span className="sr-only">Actions</span>
                    </Th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((member) => {
                    const mine = member.user.id === me;
                    const where = scopeLabel(member.workspaces, workspaces);
                    return (
                      <Tr key={member.id}>
                        <Td className={mainCell}>
                          <Person member={member} me={mine} second={member.user.email} />
                          {/* A phone has no room for this as a column, so it sits under the name. */}
                          <div className="mt-2 pl-11 md:hidden">{roleSelect(member)}</div>
                        </Td>
                        <Td className="hidden md:table-cell">{roleSelect(member)}</Td>
                        <Td className="hidden w-44 max-w-44 text-secondary whitespace-normal text-muted lg:table-cell">
                          <span className="line-clamp-2" title={where}>
                            {where}
                          </span>
                        </Td>
                        <Td className="tabular hidden text-secondary text-muted md:table-cell">
                          {mine ? 'Now' : member.last_active_at ? timeAgo(member.last_active_at) : <NoValue />}
                        </Td>
                        <Td className="pr-4 pl-0 text-right md:pr-5">
                          {!canRemove(member) && !canSeeDevices(member) ? null : (
                            <Menu>
                              <MenuTrigger asChild>
                                <IconButton label={`Actions for ${member.user.name}`} size="sm">
                                  <MoreHorizontal className="size-4" />
                                </IconButton>
                              </MenuTrigger>
                              <MenuContent align="end">
                                {canSeeDevices(member) ? (
                                  <MenuItem onSelect={() => setDevicesOf(member)}>
                                    <Smartphone className="size-4" />
                                    Signed-in devices
                                  </MenuItem>
                                ) : null}
                                {canRemove(member) ? (
                                  <MenuItem className="text-bad" onSelect={() => setRemoving(member)}>
                                    <Trash2 className="size-4" />
                                    Remove access
                                  </MenuItem>
                                ) : null}
                              </MenuContent>
                            </Menu>
                          )}
                        </Td>
                      </Tr>
                    );
                  })}
                </tbody>
              </Table>
            </TableScroll>
          )}
        </QueryView>
      </Card>
      <p className="mt-3 max-w-[46em] text-secondary text-muted">
        {alone
          ? 'An Owner or Admin runs the whole computer. A Member works in the workspaces you give them. An Approver only says yes or no. A Viewer only looks.'
          : 'Owner and Admin see every workspace. A Member works in the workspaces you give them. An Approver only says yes or no. A Viewer only looks.'}
      </p>
      {devicesOf ? <MemberDevices key={devicesOf.id} org={org} member={devicesOf} onClose={() => setDevicesOf(null)} /> : null}
      <ConfirmDialog
        open={removing !== null}
        onOpenChange={(open) => !open && setRemoving(null)}
        title={`Remove ${removing?.user.name}?`}
        description={alone ? 'They lose access to this computer on their next click. Their own account is not touched, and the audit log keeps what they did.' : 'They lose access to this org on their next click. Their account and other orgs are not touched, and the audit log keeps what they did.'}
        confirmLabel="Remove member"
        danger
        loading={remove.isPending}
        onConfirm={() =>
          removing &&
          remove.mutate(removing.id, {
            onSuccess: () => {
              setRemoving(null);
              toast('Member removed');
            },
          })
        }
      />
    </>
  );
}

/** How long an invite has left, in words a person reads at a glance: "6 days", "5 hours". */
function left(iso: string, now = Date.now()): string {
  const ms = new Date(iso).getTime() - now;
  if (ms <= 0) return 'Expired';
  const hours = ms / 3_600_000;
  if (hours < 1) return 'Under an hour';
  if (hours < 24) return plural(Math.round(hours), 'hour');
  return plural(Math.round(hours / 24), 'day');
}

const inviteStatus: Record<Invite['state'], StatusLabel> = {
  pending: { label: 'Waiting', tone: 'info' },
  accepted: { label: 'Accepted', tone: 'ok' },
  revoked: { label: 'Revoked', tone: 'idle' },
  expired: { label: 'Expired', tone: 'idle' },
};

export function InvitesSettings() {
  const { org } = useParams({ from: '/$org/settings/invites' });
  const access = useAccess(org);
  const invites = useInvites(org);
  const workspaces = useWorkspaces(org).data ?? [];
  const create = useCreateInvite(org);
  const resend = useCreateInvite(org);
  const revoke = useRevokeInvite(org);
  const [email, setEmail] = useState('');
  const [blurred, setBlurred] = useState(false);
  const [role, setRole] = useState<Role>('member');
  const [scope, setScope] = useState('all');
  // A computer sends no email, so its invite comes back with a link, shown here once.
  const [made, setMade] = useState<{ email: string; link: string } | null>(null);
  const pending = (invites.data ?? []).filter((item) => item.state === 'pending').length;
  const room = usePlanRoom(org, 'members', pending);
  const computer = useProviders().data?.code === true;
  const valid = /^\S+@\S+\.\S+$/.test(email.trim());
  // Judged once they leave the field or press Send, never while they are still typing.
  const problem = blurred && !valid ? (email.trim() ? 'Enter a full email address.' : 'Enter the email address of the person to invite.') : null;
  const error = problem ?? (create.isError && !isPlanLimit(create.error) ? describeError(create.error) : null);
  const orgWide = isOrgWide(role);

  if (!access.can('invites.read')) {
    return <NoAccess />;
  }

  const done = (invite: Invite) => {
    if (invite.token) {
      setMade({ email: invite.email, link: `${window.location.origin}/invite/${invite.token}` });
      toast('Invite made. Copy the link to send it.');
    } else {
      setMade(null);
      toast(`Invite sent to ${invite.email}`);
    }
  };

  const submit = () => {
    setBlurred(true);
    if (!valid || room.full) return;
    create.mutate(
      { email: email.trim(), role, workspaces: orgWide || scope === 'all' ? null : [scope] },
      {
        onSuccess: (invite) => {
          setEmail('');
          setBlurred(false);
          done(invite);
        },
      },
    );
  };

  return (
    <>
      <SettingsHeader
        title="Invites"
        // A computer's invites last a day, set by its own owner, so the screen says only what is true there.
        description={computer ? 'Links work once and expire. The time each one has left is shown below.' : 'Links work once and expire after 7 days.'}
      />
      <div className="grid gap-4">
        <PlanNotice org={org} resource="members" reserved={pending} />
        <FormCard
          title="Invite someone"
          description={computer ? 'You get a link to send them. It opens the invite once.' : 'They get an email with a link to join.'}
          onSubmit={submit}
        >
          <div className="grid gap-4 sm:grid-cols-[minmax(0,1fr)_minmax(0,9rem)_minmax(0,11rem)] md:grid-cols-[minmax(0,1fr)_minmax(0,9rem)_minmax(0,11rem)_auto] md:items-start">
            <Field label="Email" error={error} required className="sm:col-span-3 md:col-span-1">
              <Input
                type="email"
                inputMode="email"
                autoComplete="off"
                spellCheck={false}
                placeholder="name@company.com"
                value={email}
                onChange={(event) => {
                  setEmail(event.target.value);
                  create.reset();
                  if (blurred && problem) setBlurred(false);
                }}
                onBlur={() => setBlurred(Boolean(email.trim()))}
              />
            </Field>
            <Field label="Role">
              <Select value={role} onValueChange={(value) => setRole(value as Role)} options={roleOptions(access.role)} />
            </Field>
            <Field label="Access">
              <Select
                // Owners and admins run the whole org, so there is nothing to choose.
                disabled={orgWide}
                value={orgWide ? 'all' : scope}
                onValueChange={setScope}
                options={[{ value: 'all', label: 'All workspaces' }, ...workspaces.map((item) => ({ value: item.id, label: item.name }))]}
              />
            </Field>
            <Button
              type="submit"
              variant="primary"
              icon={<UserPlus className="size-4" />}
              disabled={room.full}
              loading={create.isPending}
              className="justify-self-start sm:col-span-3 md:col-span-1 md:mt-7"
            >
              Send invite
            </Button>
          </div>
          <p className="text-secondary text-faint">
            {roleLabel[role]}: {roleHint[role].charAt(0).toLowerCase() + roleHint[role].slice(1)}.
          </p>
        </FormCard>
        {made ? (
          <Card className="grid gap-3 px-6 py-5">
            <div className="flex items-start gap-3">
              <Check aria-hidden className="mt-1 size-4 shrink-0 text-ok" />
              <p className="text-body">
                Send this link to <span className="font-medium">{made.email}</span>. It opens the invite once, and it is not shown again once you
                leave this page.
              </p>
            </div>
            <CopyField label="Invite link" value={made.link} />
          </Card>
        ) : null}
        <Card className="overflow-hidden">
          <QueryView query={invites} skeleton={<TableSkeleton />}>
            {(items) =>
              items.length ? (
                <TableScroll>
                  <Table>
                    <thead className="hidden md:table-header-group">
                      <tr>
                        <Th>Email</Th>
                        <Th className="w-28">Role</Th>
                        <Th className="w-32">State</Th>
                        <Th className="w-28" align="end">
                          Expires in
                        </Th>
                        <Th className="w-32">
                          <span className="sr-only">Actions</span>
                        </Th>
                      </tr>
                    </thead>
                    <tbody>
                      {items.map((invite) => {
                        const where = scopeLabel(invite.workspaces, workspaces);
                        return (
                          <Tr key={invite.id}>
                            <Td className={mainCell}>
                              <CellTitle
                                title={invite.email}
                                second={
                                  <>
                                    <span className="md:hidden">{roleLabel[invite.role]} · </span>
                                    {where}
                                  </>
                                }
                              />
                            </Td>
                            <Td className="hidden md:table-cell">{roleLabel[invite.role]}</Td>
                            <Td className="hidden md:table-cell">
                              <Status status={inviteStatus[invite.state]} />
                            </Td>
                            <Td className="tabular hidden text-right md:table-cell">
                              {invite.state === 'pending' ? left(invite.expires_at) : <NoValue />}
                            </Td>
                            <Td className="pr-4 text-right md:pr-5">
                              {invite.state === 'pending' ? (
                                <Button
                                  size="sm"
                                  variant="ghost"
                                  loading={revoke.isPending && revoke.variables === invite.id}
                                  onClick={() => revoke.mutate(invite.id, { onSuccess: () => toast(`Invite to ${invite.email} revoked`) })}
                                >
                                  Revoke
                                </Button>
                              ) : invite.state === 'expired' || invite.state === 'revoked' ? (
                                <Button
                                  size="sm"
                                  variant="ghost"
                                  disabled={room.full}
                                  loading={resend.isPending && resend.variables?.email === invite.email}
                                  onClick={() => resend.mutate({ email: invite.email, role: invite.role, workspaces: invite.workspaces }, { onSuccess: done })}
                                >
                                  Send again
                                </Button>
                              ) : (
                                <Status status={inviteStatus[invite.state]} className="md:hidden" />
                              )}
                            </Td>
                          </Tr>
                        );
                      })}
                    </tbody>
                  </Table>
                </TableScroll>
              ) : (
                <EmptyState
                  icon={<UserPlus className="size-5" />}
                  title="No open invites"
                  description="Invites you send show here until they are accepted."
                  className="py-8"
                />
              )
            }
          </QueryView>
        </Card>
      </div>
    </>
  );
}
