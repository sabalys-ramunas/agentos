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
import { Field, Input } from '@/components/ui/field';
import { CodeBlock } from '@/components/ui/misc';
import { Select } from '@/components/ui/select';
import { Status } from '@/components/ui/status';
import { EmptyState, NoAccess, QueryView, Skeleton } from '@/components/ui/states';
import { mainCell, Table, Td, Th, Tr } from '@/components/ui/table';
import { date, slugify } from '@/lib/format';
import { effectLabel } from '@/lib/labels';
import { useRunsAlone } from '@/features/nodes/add-node';
import { SettingsHeader } from './page';

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
    }
  };

  const submit = () => {
    create.mutate(
      {
        subject: slugify(subject),
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
        description="This is the only time it is shown. agentos keeps only a scrambled copy, so it cannot be recovered."
        footer={
          <Button variant="primary" onClick={() => close(false)}>
            Done
          </Button>
        }
      >
        <CodeBlock code={token} label="Copy token" />
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
          <Button variant="primary" disabled={!slugify(subject)} loading={create.isPending} onClick={submit}>
            Create grant
          </Button>
        </>
      }
    >
      <div className="grid gap-4">
        <Field label="Subject" hint="Who this token is for.">
          <Input mono autoFocus placeholder="claude-code-aster" value={subject} onChange={(event) => setSubject(event.target.value)} autoComplete="off" />
        </Field>
        <Field label="Workspaces">
          <Select
            value={workspace}
            onValueChange={setWorkspace}
            options={[{ value: 'all', label: 'All workspaces' }, ...workspaces.map((item) => ({ value: item.id, label: item.name }))]}
          />
        </Field>
        <div className="grid grid-cols-2 gap-4">
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
      </div>
    </Dialog>
  );
}

export function GrantsSettings() {
  const { org } = useParams({ from: '/$org/settings/grants' });
  const access = useAccess(org);
  const alone = useRunsAlone(org);
  const grants = useGrants(org);
  const revoke = useRevokeGrant(org);
  const [creating, setCreating] = useState(false);
  const [revoking, setRevoking] = useState<Grant | null>(null);

  if (!access.can('grants.manage')) return <NoAccess />;

  return (
    <>
      <SettingsHeader
        title="Grants"
        description={alone ? 'Which AI apps can use agentos here. This computer checks their access offline, and revoking is immediate.' : 'Which AI apps can use agentos. Computers check their access offline, and revoking is immediate.'}
        action={
          isNotServed(grants.error) ? undefined : (
            <Button icon={<Plus className="size-4" />} onClick={() => setCreating(true)}>
              New grant
            </Button>
          )
        }
      />
      <Card className="overflow-hidden">
        <QueryView
          query={grants}
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
              <Table>
                <thead className="hidden md:table-header-group">
                  <tr>
                    <Th>Subject</Th>
                    <Th className="w-28">Up to</Th>
                    <Th className="w-32">Expires</Th>
                    <Th className="w-24">
                      <span className="sr-only">Actions</span>
                    </Th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((grant) => (
                    <Tr key={grant.id}>
                      <Td className={mainCell}>
                        <p className="mono truncate text-13">{grant.subject}</p>
                        <p className="text-13 whitespace-normal text-faint">
                          <span className="md:hidden">{effectLabel[grant.ceiling]} · </span>
                          {grant.workspaces.join(', ')}
                          {grant.budget ? ` · ${grant.budget}` : ''}
                        </p>
                      </Td>
                      <Td className="hidden text-13 text-muted md:table-cell">{effectLabel[grant.ceiling]}</Td>
                      <Td className="tabular hidden text-13 text-faint md:table-cell">
                        {grant.expires_at ? date(grant.expires_at) : 'Never'}
                      </Td>
                      <Td className="pr-4 text-right md:pr-6">
                        {grant.revoked ? (
                          <Status status={{ label: 'Revoked', tone: 'idle' }} className="pr-2" />
                        ) : (
                          <Button size="sm" variant="ghost" onClick={() => setRevoking(grant)}>
                            Revoke
                          </Button>
                        )}
                      </Td>
                    </Tr>
                  ))}
                </tbody>
              </Table>
            ) : (
              <EmptyState
                icon={<KeyRound />}
                title="No grants yet"
                description="Give an AI app access to connect it. Its key is shown only once."
                action={<Button onClick={() => setCreating(true)}>New grant</Button>}
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
