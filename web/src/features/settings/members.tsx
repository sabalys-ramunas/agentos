import { useParams } from '@tanstack/react-router';
import { Copy, MoreHorizontal, Smartphone, Trash2, UserPlus } from 'lucide-react';
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
import { Field, Input } from '@/components/ui/field';
import { Menu, MenuContent, MenuItem, MenuTrigger } from '@/components/ui/menu';
import { Avatar } from '@/components/ui/misc';
import { Select } from '@/components/ui/select';
import { Status } from '@/components/ui/status';
import { EmptyState, NoAccess, QueryView, Skeleton } from '@/components/ui/states';
import { mainCell, Table, Td, Th, Tr } from '@/components/ui/table';
import { Switch } from '@/components/ui/toggle';
import { PlanNotice, usePlanRoom } from '@/components/plan-notice';
import { cn } from '@/lib/cn';
import { useRunsAlone } from '@/features/nodes/add-node';
import { describeError, isPlanLimit } from '@/lib/errors';
import { timeAgo, timeUntil } from '@/lib/format';
import type { StatusLabel } from '@/lib/labels';
import { roleHint, roleLabel } from '@/lib/labels';
import { atLeast, isOrgWide, ROLES } from '@/lib/permissions';
import { SettingsHeader } from './page';
import { MemberDevices } from './member-devices';

/** Only the roles this person may hand out: never above their own. */
const roleOptions = (actor: Role) => ROLES.filter((value) => atLeast(actor, value)).map((value) => ({ value, label: roleLabel[value] }));

/** A person whose role differs between businesses: each business has its own, shown beside it. */
function differsByWorkspace(member: Member): boolean {
  return new Set((member.roles_by_workspace ?? []).map((row) => row.role)).size > 1;
}

/** The role a person holds in one business, or their role where no business is named. */
function roleIn(member: Member, workspace: string): Role {
  return member.roles_by_workspace?.find((row) => row.workspace === workspace)?.role ?? member.role;
}

function scopeLabel(scope: string[] | null, workspaces: Workspace[]) {
  if (scope === null) return 'All workspaces';
  return scope.map((id) => workspaces.find((item) => item.id === id)?.name ?? id).join(', ') || 'No workspaces';
}

function TableSkeleton() {
  return (
    <div>
      {[0, 1, 2, 3].map((index) => (
        <div key={index} className="flex h-14 items-center gap-4 border-b border-line px-6 last:border-b-0">
          <Skeleton className="size-6 rounded-full" />
          <Skeleton className="w-40" />
          <Skeleton className="ml-auto w-24" />
        </div>
      ))}
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
      <span className="text-13 text-muted">Differs by workspace</span>
    ) : canChange(member) ? (
      <Select
        size="sm"
        className="w-28"
        aria-label={`Role for ${member.user.name}`}
        value={member.role}
        options={roleOptions(access.role)}
        onValueChange={(role) => update.mutate({ id: member.id, role: role as Role }, { onSuccess: () => toast('Role updated') })}
      />
    ) : (
      <span className="text-13 text-muted">{roleLabel[member.role]}</span>
    );

  return (
    <>
      <SettingsHeader title="Members" description={alone ? 'People who can use this computer, and what they can do.' : 'People in this org and what they can do.'} />
      <Card className="overflow-hidden">
        <QueryView query={members} skeleton={<TableSkeleton />}>
          {(items) => (
            <Table>
              <thead className="hidden md:table-header-group">
                <tr>
                  <Th>Member</Th>
                  <Th className="w-36">Role</Th>
                  <Th className="hidden xl:table-cell">Workspaces</Th>
                  <Th className="w-28">Last active</Th>
                  <Th className="w-14">
                    <span className="sr-only">Actions</span>
                  </Th>
                </tr>
              </thead>
              <tbody>
                {items.map((member) => (
                  <Tr key={member.id}>
                    <Td className={mainCell}>
                      <div className="flex items-center gap-2">
                        <Avatar name={member.user.name} />
                        <div className="min-w-0">
                          <p className="truncate text-14">
                            {member.user.name}
                            {member.user.id === me ? <span className="text-faint"> (you)</span> : null}
                          </p>
                          <p className="truncate text-13 text-faint">{member.user.email}</p>
                        </div>
                      </div>
                      {/* A phone has no room for this as a column, so it sits under the name. */}
                      <div className="mt-2 pl-8 md:hidden">{roleSelect(member)}</div>
                    </Td>
                    <Td className="hidden md:table-cell">{roleSelect(member)}</Td>
                    <Td className="hidden max-w-72 truncate text-13 text-muted xl:table-cell">{scopeLabel(member.workspaces, workspaces)}</Td>
                    <Td className="tabular hidden text-13 text-faint md:table-cell">
                      {member.user.id === me ? 'Now' : member.last_active_at ? timeAgo(member.last_active_at) : '–'}
                    </Td>
                    <Td className="pr-4 pl-0 text-right md:pr-6">
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
                              <MenuItem className="text-red" onSelect={() => setRemoving(member)}>
                                <Trash2 className="size-4" />
                                Remove access
                              </MenuItem>
                            ) : null}
                          </MenuContent>
                        </Menu>
                      )}
                    </Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          )}
        </QueryView>
      </Card>
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

const inviteStatus: Record<Invite['state'], StatusLabel> = {
  pending: { label: 'Pending', tone: 'info' },
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
  const valid = /^\S+@\S+\.\S+$/.test(email);
  // Judged once they leave the field, never while they are still typing.
  const problem = blurred && email && !valid ? 'Enter a full email address.' : null;
  const error = problem ?? (create.isError && !isPlanLimit(create.error) ? describeError(create.error) : null);
  const orgWide = isOrgWide(role);

  if (!access.can('invites.read')) {
    return <NoAccess />;
  }

  const submit = () => {
    create.mutate(
      { email: email.trim(), role, workspaces: orgWide || scope === 'all' ? null : [scope] },
      {
        onSuccess: (invite) => {
          setEmail('');
          setBlurred(false);
          if (invite.token) {
            setMade({ email: invite.email, link: `${window.location.origin}/invite/${invite.token}` });
            toast('Invite made. Copy the link to send it.');
          } else {
            setMade(null);
            toast('Invite sent');
          }
        },
      },
    );
  };

  return (
    <>
      <SettingsHeader
        title="Invites"
        // A computer's invites last a day, set by its own owner, so the screen says only what is true there.
        description={computer ? 'Links are single use and expire. The time each one has left is shown below.' : 'Links are single use and expire after 7 days.'}
      />
      <div className="grid gap-6">
        <PlanNotice org={org} resource="members" reserved={pending} />
        <FormCard
          title="Invite someone"
          footnote={roleHint[role]}
          action={
            <Button variant="primary" size="sm" icon={<UserPlus className="size-4" />} disabled={!valid || room.full} loading={create.isPending} onClick={submit}>
              Send invite
            </Button>
          }
        >
          <div className="grid gap-4 sm:grid-cols-[minmax(0,1fr)_140px_180px]">
            <Field label="Email" error={error}>
              <Input
                type="email"
                placeholder="name@example.com"
                value={email}
                onChange={(event) => {
                  setEmail(event.target.value);
                  create.reset();
                }}
                onBlur={() => setBlurred(true)}
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
          </div>
        </FormCard>
        {made ? (
          <Card className="grid gap-3 p-4 sm:p-6">
            <p className="text-14">
              Send this link to <span className="text-fg">{made.email}</span>. It opens the invite once, and it is not shown again
              once you leave this page.
            </p>
            <div className="flex flex-col gap-2 sm:flex-row">
              <Input aria-label="Invite link" readOnly value={made.link} className="mono h-10 flex-1 text-13" onFocus={(event) => event.target.select()} />
              <Button
                icon={<Copy className="size-4" />}
                onClick={() =>
                  void navigator.clipboard
                    .writeText(made.link)
                    .then(() => toast('Link copied'))
                    .catch(() => toast.error('Copy the link from the box.'))
                }
              >
                Copy link
              </Button>
            </div>
          </Card>
        ) : null}
        <Card className="overflow-hidden">
          <QueryView query={invites} skeleton={<TableSkeleton />}>
            {(items) =>
              items.length ? (
                <Table>
                  <thead className="hidden md:table-header-group">
                    <tr>
                      <Th>Email</Th>
                      <Th className="w-28">Role</Th>
                      <Th className="w-28">State</Th>
                      <Th className="w-28">Expires in</Th>
                      <Th className="w-24">
                        <span className="sr-only">Actions</span>
                      </Th>
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((invite) => (
                      <Tr key={invite.id}>
                        <Td className={mainCell}>
                          <p className="truncate text-14">{invite.email}</p>
                          <p className="truncate text-13 text-faint">
                            <span className="md:hidden">{roleLabel[invite.role]} · </span>
                            {scopeLabel(invite.workspaces, workspaces)}
                          </p>
                        </Td>
                        <Td className="hidden text-13 text-muted md:table-cell">{roleLabel[invite.role]}</Td>
                        <Td className="hidden md:table-cell">
                          <Status status={inviteStatus[invite.state]} />
                        </Td>
                        <Td className="tabular hidden text-13 text-faint md:table-cell">
                          {invite.state === 'pending' ? timeUntil(invite.expires_at) : '–'}
                        </Td>
                        <Td className="pr-4 text-right md:pr-6">
                          {invite.state === 'pending' ? (
                            <Button size="sm" variant="ghost" onClick={() => revoke.mutate(invite.id, { onSuccess: () => toast('Invite revoked') })}>
                              Revoke
                            </Button>
                          ) : (
                            <Status status={inviteStatus[invite.state]} className="md:hidden" />
                          )}
                        </Td>
                      </Tr>
                    ))}
                  </tbody>
                </Table>
              ) : (
                <EmptyState title="No open invites" description="Invites you send appear here until they are accepted." className="py-8" />
              )
            }
          </QueryView>
        </Card>
      </div>
    </>
  );
}

function AccessRow({ member, workspaces, org }: { member: Member; workspaces: Workspace[]; org: string }) {
  const update = useUpdateMember(org);
  const access = useAccess(org);
  const me = useSession().data?.user.id;
  const canChange = access.can('members.update') && member.user.id !== me && atLeast(access.role, member.role);
  const orgWide = isOrgWide(member.role);
  const everything = member.workspaces === null;
  const scope = member.workspaces ?? [];
  const differs = differsByWorkspace(member);

  const toggle = (id: string) => {
    const next = scope.includes(id) ? scope.filter((item) => item !== id) : [...scope, id];
    update.mutate({ id: member.id, workspaces: next });
  };

  return (
    <li className="grid gap-3 p-4 sm:px-6">
      <div className="flex items-center gap-2">
        <Avatar name={member.user.name} />
        <div className="min-w-0 flex-1">
          <p className="truncate text-14">{member.user.name}</p>
          <p className="text-13 text-faint">{differs ? 'Differs by workspace' : roleLabel[member.role]}</p>
        </div>
        <div className="flex items-center gap-2 text-13 text-muted">
          {/* An owner or admin always sees every workspace, so there is no switch to offer. */}
          {orgWide ? (
            <span>Always all workspaces</span>
          ) : (
            <>
              <span className="hidden sm:inline">All workspaces</span>
              <Switch
                aria-label={`${member.user.name} can see all workspaces`}
                checked={everything}
                onCheckedChange={(checked) => update.mutate({ id: member.id, workspaces: checked ? null : [] })}
              />
            </>
          )}
        </div>
      </div>
      {everything ? null : (
        <div className="flex flex-wrap gap-2 sm:pl-8">
          {workspaces.map((workspace) => {
            const on = scope.includes(workspace.id);
            return (
              <button
                key={workspace.id}
                type="button"
                aria-pressed={on}
                onClick={() => toggle(workspace.id)}
                className={cn(
                  'h-7 rounded-md border px-2 text-13 transition-colors duration-(--duration-fast) ease-standard',
                  on ? 'border-fg text-fg' : 'border-line text-muted hover:border-line-strong',
                )}
              >
                {workspace.name}
              </button>
            );
          })}
        </div>
      )}
      {everything || scope.length === 0 ? null : (
        // What the person may do in each business they can see: changed here, one business at a time.
        <ul className="grid gap-2 sm:pl-8">
          {workspaces
            .filter((workspace) => scope.includes(workspace.id))
            .map((workspace) => (
              <li key={workspace.id} className="flex items-center gap-3 text-13">
                <span className="min-w-0 flex-1 truncate">{workspace.name}</span>
                {canChange ? (
                  <Select
                    size="sm"
                    className="w-32"
                    aria-label={`Role in ${workspace.name} for ${member.user.name}`}
                    value={roleIn(member, workspace.id)}
                    options={roleOptions(access.role)}
                    onValueChange={(role) => update.mutate({ id: member.id, workspace: workspace.id, role: role as Role })}
                  />
                ) : (
                  <span className="text-muted">{roleLabel[roleIn(member, workspace.id)]}</span>
                )}
              </li>
            ))}
        </ul>
      )}
    </li>
  );
}

export function WorkspaceAccessSettings() {
  const { org } = useParams({ from: '/$org/settings/access' });
  const access = useAccess(org);
  const members = useMembers(org);
  const workspaces = useWorkspaces(org).data ?? [];
  if (!access.can('members.update')) return <NoAccess />;
  return (
    <>
      <SettingsHeader title="Workspace access" description="A limited member only ever sees their workspaces." />
      <Card>
        <QueryView query={members} skeleton={<TableSkeleton />}>
          {(items) => (
            <ul className="divide-y divide-line">
              {items.map((member) => (
                <AccessRow key={member.id} member={member} workspaces={workspaces} org={org} />
              ))}
            </ul>
          )}
        </QueryView>
      </Card>
    </>
  );
}
