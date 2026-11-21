/**
 * Workspace access: a grid of people by workspaces, a tick where someone sees one. Owners and admins always see
 * every workspace; a member's role can differ from one workspace to the next, changed in the card under it.
 */
import { useParams } from '@tanstack/react-router';
import { Check } from 'lucide-react';
import { useAccess, useMembers, useSession, useUpdateMember, useWorkspaces } from '@/api/hooks';
import type { Member, Role, Workspace } from '@/api/types';
import { Card, FormCard } from '@/components/ui/card';
import { Select } from '@/components/ui/select';
import { NoAccess, QueryView } from '@/components/ui/states';
import { NoValue, Table, TableScroll, Td, Th, Tr } from '@/components/ui/table';
import { cn } from '@/lib/cn';
import { roleLabel } from '@/lib/labels';
import { atLeast, isOrgWide } from '@/lib/permissions';
import { differsByWorkspace, Person, roleIn, roleOptions, TableSkeleton } from './members';
import { SettingsHeader } from './page';
import { chosen, toastChange } from '@/components/ui/toast';

/** Short column heads for the access grid: each workspace's first word, unless two would read the same. */
function shortNames(workspaces: Workspace[]): Map<string, string> {
  const first = (name: string) => name.split(/\s+/)[0] || name;
  const counts = new Map<string, number>();
  for (const workspace of workspaces) counts.set(first(workspace.name), (counts.get(first(workspace.name)) ?? 0) + 1);
  return new Map(workspaces.map((workspace) => [workspace.id, counts.get(first(workspace.name)) === 1 ? first(workspace.name) : workspace.name]));
}

/** One member's row of the access grid: what they see, and a tick per workspace. */
function AccessRow({ member, workspaces, org, me }: { member: Member; workspaces: Workspace[]; org: string; me: string | undefined }) {
  const update = useUpdateMember(org);
  const access = useAccess(org);
  const canChange = access.can('members.update') && member.user.id !== me && atLeast(access.role, member.role);
  const orgWide = isOrgWide(member.role);
  const everything = member.workspaces === null;
  const scope = member.workspaces ?? [];
  const differs = differsByWorkspace(member);
  const name = member.user.name;

  // Each change saves at once; the toast says it, and Undo puts back what this person saw before.
  const save = (workspaces: string[] | null, said: string, undone: string) => {
    const before = member.workspaces;
    update.mutate({ id: member.id, workspaces }, { onSuccess: () => toastChange({ said, undone, undo: () => update.mutateAsync({ id: member.id, workspaces: before }) }) });
  };
  const toggle = (id: string) => {
    const seen = scope.includes(id);
    const next = seen ? scope.filter((item) => item !== id) : [...scope, id];
    const where = workspaces.find((item) => item.id === id)?.name ?? id;
    save(next, seen ? `${name} no longer sees ${where}` : `${name} sees ${where} now`, seen ? `${name} sees ${where} again` : `${name} no longer sees ${where}`);
  };

  return (
    <Tr>
      <Td className="sticky left-0 z-1 bg-card">
        <Person member={member} me={member.user.id === me} second={differs ? 'Differs by workspace' : roleLabel[member.role]} />
      </Td>
      <Td>
        {/* An owner or admin always sees every workspace, so there is nothing to choose. */}
        {orgWide ? (
          <span className="text-secondary text-muted">Always all</span>
        ) : canChange ? (
          <Select
            size="sm"
            className="w-24"
            aria-label={`What ${name} sees`}
            value={everything ? 'all' : 'some'}
            options={[
              { value: 'all', label: 'All' },
              { value: 'some', label: 'Some' },
            ]}
            onValueChange={(value) =>
              save(value === 'all' ? null : scope, value === 'all' ? `${name} sees every workspace` : `${name} sees some workspaces`, value === 'all' ? `${name} sees some workspaces again` : `${name} sees every workspace again`)
            }
          />
        ) : (
          <span className="text-secondary text-muted">{everything ? 'All' : 'Some'}</span>
        )}
        {!orgWide && !everything && scope.length === 0 ? <p className="mt-1 text-caption text-warn">None yet</p> : null}
      </Td>
      {workspaces.map((workspace) => {
        const on = orgWide || everything || scope.includes(workspace.id);
        const fixed = orgWide || everything || !canChange;
        const label = `${name} sees ${workspace.name}`;
        return (
          <Td key={workspace.id} className="px-1! text-center">
            {fixed ? (
              <span
                className="inline-flex size-8 items-center justify-center"
                title={orgWide ? `${roleLabel[member.role]}s always see every workspace` : everything ? `${name} sees every workspace` : undefined}
              >
                {on ? <Check role="img" aria-label={label} className="size-4 text-muted" /> : <NoValue />}
              </span>
            ) : (
              <button
                type="button"
                aria-pressed={on}
                aria-label={label}
                onClick={() => toggle(workspace.id)}
                className={cn(
                  'inline-flex size-8 items-center justify-center rounded-md transition-colors duration-(--duration-fast) ease-standard',
                  'hover:bg-hover active:bg-pressed',
                  on ? 'text-accent-text' : 'text-faint',
                )}
              >
                {on ? <Check aria-hidden className="size-4" strokeWidth={2.25} /> : <span aria-hidden>–</span>}
              </button>
            )}
          </Td>
        );
      })}
    </Tr>
  );
}

/** What a person may do in each business they see, changed one business at a time. */
function RolesByWorkspace({ members, workspaces, org, me }: { members: Member[]; workspaces: Workspace[]; org: string; me: string | undefined }) {
  const update = useUpdateMember(org);
  const access = useAccess(org);
  const scoped = members.filter((member) => !isOrgWide(member.role) && (member.workspaces?.length ?? 0) > 0);
  if (!scoped.length) return null;
  return (
    <FormCard title="Role in each workspace" description="A person can do more in one workspace than in another. Owners and admins are the same everywhere.">
      <ul className="grid divide-y divide-line">
        {scoped.map((member) => {
          const canChange = access.can('members.update') && member.user.id !== me && atLeast(access.role, member.role);
          return (
            <li key={member.id} className="grid gap-3 py-3 first:pt-0 last:pb-0 sm:grid-cols-[minmax(0,14rem)_minmax(0,1fr)]">
              <Person member={member} me={member.user.id === me} second={differsByWorkspace(member) ? 'Differs by workspace' : roleLabel[member.role]} />
              <ul className="grid gap-2">
                {workspaces
                  .filter((workspace) => member.workspaces?.includes(workspace.id))
                  .map((workspace) => (
                    <li key={workspace.id} className="flex min-h-8 items-center gap-3">
                      <span className="min-w-0 flex-1 truncate text-secondary" title={workspace.name}>
                        {workspace.name}
                      </span>
                      {canChange ? (
                        <Select
                          size="sm"
                          className="w-32"
                          aria-label={`Role in ${workspace.name} for ${member.user.name}`}
                          value={roleIn(member, workspace.id)}
                          options={roleOptions(access.role)}
                          onValueChange={(role) =>
                            update.mutate(
                              { id: member.id, workspace: workspace.id, role: role as Role },
                              {
                                onSuccess: () =>
                                  toastChange({
                                    ...chosen(`${member.user.name}'s role in ${workspace.name}`, roleLabel[role as Role], roleLabel[roleIn(member, workspace.id)]),
                                    undo: () => update.mutateAsync({ id: member.id, workspace: workspace.id, role: roleIn(member, workspace.id) }),
                                  }),
                              },
                            )
                          }
                        />
                      ) : (
                        <span className="text-secondary text-muted">{roleLabel[roleIn(member, workspace.id)]}</span>
                      )}
                    </li>
                  ))}
              </ul>
            </li>
          );
        })}
      </ul>
    </FormCard>
  );
}

export function WorkspaceAccessSettings() {
  const { org } = useParams({ from: '/$org/settings/access' });
  const access = useAccess(org);
  const members = useMembers(org);
  const workspaces = useWorkspaces(org).data ?? [];
  const me = useSession().data?.user.id;
  const short = shortNames(workspaces);
  if (!access.can('members.update')) return <NoAccess />;
  return (
    <>
      <SettingsHeader title="Workspace access" description="A limited member only ever sees their workspaces." />
      <QueryView query={members} card skeleton={<Card><TableSkeleton /></Card>}>
        {(items) => (
          <div className="grid gap-4">
            <div>
              <Card className="overflow-hidden">
                <TableScroll>
                  <Table density="relaxed" aria-label="Who sees which workspace">
                    <thead>
                      <tr>
                        <Th className="sticky left-0 z-1 min-w-48 bg-card">Member</Th>
                        <Th className="w-24">Sees</Th>
                        {workspaces.map((workspace) => (
                          <Th key={workspace.id} className="w-20 max-w-24 px-1! text-center" title={workspace.name}>
                            <span aria-hidden className="block truncate">
                              {short.get(workspace.id)}
                            </span>
                            <span className="sr-only">{workspace.name}</span>
                          </Th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {items.map((member) => (
                        <AccessRow key={member.id} member={member} workspaces={workspaces} org={org} me={me} />
                      ))}
                    </tbody>
                  </Table>
                </TableScroll>
              </Card>
              <p className="mt-3 text-secondary text-muted">Click a tick to give or take a workspace. Owners and Admins always see all of them.</p>
            </div>
            <RolesByWorkspace members={items} workspaces={workspaces} org={org} me={me} />
          </div>
        )}
      </QueryView>
    </>
  );
}
