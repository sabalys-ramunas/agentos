import { useParams } from '@tanstack/react-router';
import { KeyRound, Plus } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';
import { useAccess, useCreateGrant, useGrants, useRevokeGrant, useWorkspaces } from '@/api/hooks';
import { isNotServed } from '@/api/http';
import type { Effect, Grant } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { ConfirmDialog, Dialog } from '@/components/ui/dialog';
import { CopyField, Field, Input } from '@/components/ui/field';
import { describeError } from '@/lib/errors';
import { Select } from '@/components/ui/select';
import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/cn';
import { EmptyState, NoAccess, QueryView } from '@/components/ui/states';
import { mainCell, Table, TableScroll, TableSkeleton, Td, Th, Tr } from '@/components/ui/table';
import { date, slugify } from '@/lib/format';
import { effectLabel } from '@/lib/labels';
import { useRunsAlone } from '@/features/nodes/add-node';
import { SettingsHeader } from './page';
import { workspaceWords } from '@/lib/plain-node';

const CEILINGS: Effect[] = ['read', 'draft', 'commit', 'pay', 'irreversible'];
const TTLS = [
  { value: '7d', label: '7 days' },
  { value: '30d', label: '30 days' },
  { value: '90d', label: '90 days' },
];

function NewGrantDialog({ org, open, onOpenChange }: { org: string; open: boolean; onOpenChange: (open: boolean) => void }) {
  const workspaces = useWorkspaces(org).data ?? [];
  const create = useCreateGrant(org);
  const [subject, setSubject] = useState('');
  const [workspace, setWorkspace] = useState('all');
  const [ceiling, setCeiling] = useState<Effect>('draft');
  const [ttl, setTtl] = useState('30d');
  const [token, setToken] = useState<string | null>(null);

  const close = (next: boolean) => {
    onOpenChange(next);
    if (!next) {
      setToken(null);
      setSubject('');
      create.reset();
    }
  };

  const slug = slugify(subject);
  const submit = () => {
    create.mutate(
      {
        subject: slug,
        workspaces: workspace === 'all' ? workspaces.map((item) => item.id) : [workspace],
        ceiling,
        specials: [],
        ttl,
      },
      { onSuccess: (result) => setToken(result.token) },
    );
  };

  if (token) {
    return (
      <Dialog
        open={open}
        onOpenChange={close}
        title="Copy your token"
        description="This is the only time it is shown. openajar keeps only a scrambled copy, so it cannot be recovered."
        footer={
          <Button variant="primary" onClick={() => close(false)}>
            Done
          </Button>
        }
      >
        <CopyField label="Token" value={token} />
      </Dialog>
    );
  }

  return (
    <Dialog
      open={open}
      onOpenChange={close}
      title="New grant"
      description="A token an agent uses to act, scoped to workspaces and effects."
      footer={
        <>
          <Button onClick={() => close(false)}>Cancel</Button>
          <Button type="submit" form="new-grant" variant="primary" disabled={!slug} loading={create.isPending}>
            Create grant
          </Button>
        </>
      }
    >
      <form
        id="new-grant"
        className="grid gap-4"
        onSubmit={(event) => {
          event.preventDefault();
          if (slug) submit();
        }}
      >
        <Field
          label="App"
          required
          hint={slug && slug !== subject.trim() ? <>It is saved as <span className="mono text-fg">{slug}</span>.</> : 'Which app this access is for, so you know it again in this list.'}
          error={create.isError ? describeError(create.error) : null}
        >
          <Input mono autoFocus placeholder="claude-code-aster" value={subject} onChange={(event) => setSubject(event.target.value)} autoComplete="off" spellCheck={false} />
        </Field>
        <Field label="Workspaces">
          <Select
            value={workspace}
            onValueChange={setWorkspace}
            options={[{ value: 'all', label: 'All workspaces' }, ...workspaces.map((item) => ({ value: item.id, label: item.name }))]}
          />
        </Field>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Highest effect" hint={ceiling === 'pay' || ceiling === 'irreversible' ? 'Asks you to confirm it is you.' : undefined}>
            <Select
              value={ceiling}
              onValueChange={(value) => setCeiling(value as Effect)}
              options={CEILINGS.map((value) => ({ value, label: effectLabel[value] }))}
            />
          </Field>
          <Field label="Expires after">
            <Select value={ttl} onValueChange={setTtl} options={TTLS} />
          </Field>
        </div>
      </form>
    </Dialog>
  );
}

export function GrantsSettings() {
  const { org } = useParams({ from: '/$org/settings/grants' });
  const access = useAccess(org);
  const alone = useRunsAlone(org);
  const grants = useGrants(org);
  const workspaces = useWorkspaces(org).data ?? [];
  const revoke = useRevokeGrant(org);
  const [creating, setCreating] = useState(false);
  const [revoking, setRevoking] = useState<Grant | null>(null);

  if (!access.can('grants.manage')) return <NoAccess />;

  return (
    <>
      <SettingsHeader
        title="Grants"
        description={alone ? 'Which AI apps can use openajar here. This computer checks their access offline, and revoking is immediate.' : 'Which AI apps can use openajar. Computers check their access offline, and revoking is immediate.'}
        action={
          isNotServed(grants.error) ? undefined : (
            <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setCreating(true)}>
              New grant
            </Button>
          )
        }
      />
      <Card className="overflow-hidden">
        <QueryView query={grants} skeleton={<TableSkeleton columns={['App', 'Up to', 'Expires', '']} rows={4} />}>
          {(items) =>
            items.length ? (
              <TableScroll>
                <Table>
                  <thead className="hidden md:table-header-group">
                    <tr>
                      <Th>App</Th>
                      <Th className="w-36">Up to</Th>
                      <Th className="w-32">Expires</Th>
                      <Th className="w-28">
                        <span className="sr-only">Actions</span>
                      </Th>
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((grant) => {
                      const where = grant.workspaces.map((id) => workspaceWords(id, workspaces.find((item) => item.id === id)?.name)).join(', ');
                      return (
                        <Tr key={grant.id}>
                          <Td className={mainCell}>
                            <p title={grant.subject} className={cn('mono truncate text-code font-medium', grant.revoked ? 'text-faint' : 'text-fg')}>
                              {grant.subject}
                            </p>
                            <p className="text-secondary whitespace-normal text-faint">
                              <span className="md:hidden">{effectLabel[grant.ceiling]} · </span>
                              {where}
                              {grant.budget ? ` · ${grant.budget}` : ''}
                            </p>
                          </Td>
                          <Td className={cn('hidden md:table-cell', grant.revoked && 'text-muted')}>{effectLabel[grant.ceiling]}</Td>
                          <Td className={cn('tabular hidden md:table-cell', grant.revoked && 'text-muted')}>
                            {grant.expires_at ? date(grant.expires_at) : 'Never'}
                          </Td>
                          <Td className="pr-4 text-right md:pr-5">
                            {grant.revoked ? (
                              <Badge>Revoked</Badge>
                            ) : (
                              <Button size="sm" variant="ghost" onClick={() => setRevoking(grant)}>
                                Revoke
                              </Button>
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
                icon={<KeyRound />}
                title="No grants yet"
                description="Give an AI app access to connect it. Its key is shown only once."
                action={
                  <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setCreating(true)}>
                    New grant
                  </Button>
                }
              />
            )
          }
        </QueryView>
      </Card>
      <NewGrantDialog org={org} open={creating} onOpenChange={setCreating} />
      <ConfirmDialog
        open={revoking !== null}
        onOpenChange={(open) => !open && setRevoking(null)}
        title={`Revoke ${revoking?.subject}?`}
        description="Agents using it stop at their next call, and any grant made from it stops too."
        confirmLabel="Revoke grant"
        danger
        loading={revoke.isPending}
        onConfirm={() =>
          revoking &&
          revoke.mutate(revoking.id, {
            onSuccess: () => {
              setRevoking(null);
              toast('Grant revoked');
            },
          })
        }
      />
    </>
  );
}
