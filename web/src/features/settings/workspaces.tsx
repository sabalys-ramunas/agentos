import { useParams } from '@tanstack/react-router';
import { Download, MoreHorizontal, Pencil, Plus, Trash2 } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';
import { useAccess, useCreateWorkspace, useDeleteWorkspace, useStatus, useUpdateWorkspace, useWorkspaces } from '@/api/hooks';
import type { Workspace } from '@/api/types';
import { WorkspaceMark } from '@/components/lane-icon';
import { OverLimitBanner, PlanNotice, usePlanRoom } from '@/components/plan-notice';
import { Badge } from '@/components/ui/badge';
import { Button, IconButton } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { ConfirmDialog, Dialog } from '@/components/ui/dialog';
import { Field, Input } from '@/components/ui/field';
import { Menu, MenuContent, MenuItem, MenuTrigger } from '@/components/ui/menu';
import { EmptyState, QueryView, Skeleton } from '@/components/ui/states';
import { describeError, isPlanLimit } from '@/lib/errors';
import { plural } from '@/lib/format';
import { useTechnical } from '@/lib/technical';
import { FormDialog } from './on-computer';
import { SettingsHeader } from './page';

/** Shared with Home: a workspace is one brand or person. */
export function NewWorkspaceDialog({ org, open, onOpenChange }: { org: string; open: boolean; onOpenChange: (open: boolean) => void }) {
  const create = useCreateWorkspace(org);
  const [name, setName] = useState('');
  const close = (next: boolean) => {
    onOpenChange(next);
    if (!next) {
      setName('');
      create.reset();
    }
  };
  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    create.mutate({ name: name.trim() }, { onSuccess: () => (toast('Workspace created'), close(false)) });
  };
  return (
    <Dialog
      open={open}
      onOpenChange={close}
      title="New workspace"
      description="One per brand or person. Its accounts, logins and history stay separate from the rest."
      footer={
        <>
          <Button onClick={() => close(false)}>Cancel</Button>
          <Button type="submit" form="new-workspace" variant="primary" disabled={!name.trim()} loading={create.isPending}>
            Create workspace
          </Button>
        </>
      }
    >
      <form id="new-workspace" onSubmit={submit} className="grid gap-4">
        <Field label="Name" error={create.isError && !isPlanLimit(create.error) ? describeError(create.error) : null}>
          <Input autoFocus maxLength={80} placeholder="Aster Coffee" value={name} onChange={(event) => setName(event.target.value)} autoComplete="off" />
        </Field>
        {isPlanLimit(create.error) ? <PlanNotice org={org} resource="workspaces" /> : null}
      </form>
    </Dialog>
  );
}

/**
 * Brings an export file back on this computer. The name it may need is typed here: an export whose name is
 * already on this computer asks for another, and the form it opens on the computer says so in its own box.
 */
function ImportDialog({ org, open, onOpenChange }: { org: string; open: boolean; onOpenChange: (open: boolean) => void }) {
  const [name, setName] = useState('');
  const typed = name.trim();
  return (
    <FormDialog
      org={org}
      open={open}
      onOpenChange={(next) => {
        if (!next) setName('');
        onOpenChange(next);
      }}
      title="Import a workspace"
      description="Choose the export file, and type the passphrase it was sealed with, on this computer. agentos checks the file first and imports nothing that does not pass."
      kind="workspace_import"
      fields={typed ? { name: typed } : undefined}
      confirmLabel="Continue on this computer"
      doneWords="Imported. The workspace is back on this computer."
    >
      <Field label="New name (optional)" hint="The name the workspace shows on this computer. Its id is made from it.">
        <Input value={name} onChange={(event) => setName(event.target.value)} placeholder="Harbor copy" autoComplete="off" />
      </Field>
    </FormDialog>
  );
}

function RenameDialog({ org, workspace, onClose }: { org: string; workspace: Workspace | null; onClose: () => void }) {
  const update = useUpdateWorkspace(org);
  const [name, setName] = useState<string | null>(null);
  const value = name ?? workspace?.name ?? '';
  const close = () => {
    setName(null);
    onClose();
  };
  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    if (workspace) update.mutate({ id: workspace.id, name: value.trim() }, { onSuccess: close });
  };
  return (
    <Dialog
      open={workspace !== null}
      onOpenChange={(open) => !open && close()}
      title="Rename workspace"
      description="Only the name changes. Its accounts and their links stay as they are."
      footer={
        <>
          <Button onClick={close}>Cancel</Button>
          <Button type="submit" form="rename-workspace" variant="primary" disabled={!value.trim() || value === workspace?.name} loading={update.isPending}>
            Save
          </Button>
        </>
      }
    >
      <form id="rename-workspace" onSubmit={submit}>
        <Field label="Name">
          <Input autoFocus maxLength={80} value={value} onChange={(event) => setName(event.target.value)} autoComplete="off" />
        </Field>
      </form>
    </Dialog>
  );
}

export function WorkspacesSettings() {
  const { org } = useParams({ from: '/$org/settings/workspaces' });
  const [technical] = useTechnical();
  const workspaces = useWorkspaces(org);
  const status = useStatus(org).data;
  const access = useAccess(org);
  const room = usePlanRoom(org, 'workspaces');
  const remove = useDeleteWorkspace(org);
  const [creating, setCreating] = useState(false);
  const [renaming, setRenaming] = useState<Workspace | null>(null);
  const [deleting, setDeleting] = useState<Workspace | null>(null);
  const [importing, setImporting] = useState(false);
  const [exporting, setExporting] = useState<Workspace | null>(null);
  const canCreate = access.can('workspaces.create');
  const canTransfer = access.can('workspaces.transfer');
  const canManage = access.can('workspaces.update') || access.can('workspaces.delete');

  return (
    <>
      <SettingsHeader
        title="Workspaces"
        description="One per brand or person. Each is a clean desk with only its own accounts and logins."
        action={
          canCreate || canTransfer ? (
            <div className="flex flex-wrap items-center justify-end gap-2">
              {canTransfer ? <Button variant="ghost" onClick={() => setImporting(true)}>Import a workspace</Button> : null}
              {canCreate ? (
                <Button icon={<Plus className="size-4" />} disabled={room.full} onClick={() => setCreating(true)}>
                  New workspace
                </Button>
              ) : null}
            </div>
          ) : undefined
        }
      />
      <div className="grid gap-4">
        <OverLimitBanner org={org} />
        {canCreate ? <PlanNotice org={org} resource="workspaces" /> : null}
        <Card className="overflow-hidden">
          <QueryView
            query={workspaces}
            skeleton={
              <div className="grid gap-4 p-6">
                <Skeleton />
                <Skeleton />
                <Skeleton className="w-2/3" />
              </div>
            }
          >
            {(items) =>
              items.length ? (
                <ul className="divide-y divide-line">
                  {items.map((workspace, index) => {
                    // As in the API: past the limit, the newest are read-only.
                    const readOnly = status?.limits !== undefined && index >= status.limits.workspaces;
                    return (
                      <li key={workspace.id} className="flex min-h-14 items-center gap-2 px-4 py-2 sm:px-6">
                        <WorkspaceMark name={workspace.name} color={workspace.color} className="mr-1" />
                        <div className="min-w-0 flex-1">
                          <p className="truncate text-body">{workspace.name}</p>
                          {technical ? <p className="mono truncate text-caption text-faint">{workspace.id}</p> : null}
                        </div>
                        {readOnly ? <Badge>Read-only</Badge> : null}
                        <span className="hidden text-secondary text-muted sm:inline">{plural(workspace.lanes.length, 'account')}</span>
                        {canManage ? (
                          <Menu>
                            <MenuTrigger asChild>
                              <IconButton label={`Actions for ${workspace.name}`} size="sm" className="ml-2">
                                <MoreHorizontal className="size-4" />
                              </IconButton>
                            </MenuTrigger>
                            <MenuContent align="end">
                              {readOnly ? null : (
                                <MenuItem onSelect={() => setRenaming(workspace)}>
                                  <Pencil className="size-4 text-faint" />
                                  Rename
                                </MenuItem>
                              )}
                              {canTransfer ? (
                                <MenuItem onSelect={() => setExporting(workspace)}>
                                  <Download className="size-4 text-faint" />
                                  Export
                                </MenuItem>
                              ) : null}
                              <MenuItem className="text-bad" onSelect={() => setDeleting(workspace)}>
                                <Trash2 className="size-4" />
                                Delete
                              </MenuItem>
                            </MenuContent>
                          </Menu>
                        ) : null}
                      </li>
                    );
                  })}
                </ul>
              ) : (
                <EmptyState
                  title="No workspaces yet"
                  description={canCreate ? 'Add one for each brand or person you run.' : 'An owner or admin sets these up.'}
                  action={canCreate ? <Button onClick={() => setCreating(true)}>New workspace</Button> : undefined}
                />
              )
            }
          </QueryView>
        </Card>
      </div>
      <NewWorkspaceDialog org={org} open={creating} onOpenChange={setCreating} />
      <ImportDialog org={org} open={importing} onOpenChange={setImporting} />
      {exporting ? (
        <FormDialog
          key={exporting.id}
          org={org}
          open
          onOpenChange={(next) => {
            if (!next) setExporting(null);
          }}
          title={`Export ${exporting.name}`}
          description="The file is sealed with a passphrase you type on this computer. Keep the passphrase: without it nobody can open the file. It downloads from that page, and agentos keeps no copy."
          kind="workspace_export"
          fields={{ workspace: exporting.id }}
          confirmLabel="Continue on this computer"
          doneWords="Exported. The file is downloading on this computer."
        />
      ) : null}
      <RenameDialog org={org} workspace={renaming} onClose={() => setRenaming(null)} />
      <ConfirmDialog
        open={deleting !== null}
        onOpenChange={(open) => !open && setDeleting(null)}
        title={`Delete ${deleting?.name}?`}
        description={`This removes ${plural(deleting?.lanes.length ?? 0, 'account')} from agentos, and members lose access to this workspace. Profiles and logins stay on your computers.`}
        confirmLabel="Delete workspace"
        danger
        typeToConfirm={deleting?.id}
        loading={remove.isPending}
        onConfirm={() =>
          deleting &&
          remove.mutate(deleting.id, {
            onSuccess: () => {
              setDeleting(null);
              toast('Workspace deleted');
            },
          })
        }
      />
    </>
  );
}
