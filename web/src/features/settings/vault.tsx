import { useParams } from '@tanstack/react-router';
import { ArrowUpRight, LockKeyhole, Search, Trash2 } from 'lucide-react';
import { useEffect, useState } from 'react';
import { toast } from 'sonner';
import { useAccess, useLockVault, useProviders, useRemoveVaultEntry, useVault, useVaultStatus } from '@/api/hooks';
import type { VaultEntry, VaultKind, VaultStatus } from '@/api/types';
import { Button, IconButton } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { ConfirmDialog, Dialog } from '@/components/ui/dialog';
import { Field, Input } from '@/components/ui/field';
import { Select } from '@/components/ui/select';
import { EmptyState, InlineError, QueryView, Skeleton } from '@/components/ui/states';
import { mainCell, Table, Td, Th, Tr } from '@/components/ui/table';
import { timeAgo } from '@/lib/format';
import { vaultKind } from '@/lib/labels';
import { FORM_WORDS, useFormOnComputer } from './on-computer';
import { SettingsHeader } from './page';

const KINDS: VaultKind[] = ['password', 'totp', 'cookie', 'api_key'];
const KIND_OPTIONS = KINDS.map((value) => ({ value, label: vaultKind[value] }));
const ACCOUNT_PATTERN = /^[a-z0-9][a-z0-9_-]*\/[a-z0-9][a-z0-9_.-]*$/;

function StatusLine({ status }: { status: VaultStatus | undefined }) {
  if (!status) return null;
  const words =
    status.state === 'unsealed'
      ? 'Unlocked. Names are shown here. Secrets stay on this computer.'
      : status.state === 'sealed'
        ? 'Locked. Saved logins cannot be used until the vault is unlocked on this computer.'
        : status.state === 'uninitialized'
          ? 'There is no vault on this computer yet.'
          : `The vault could not be read${status.error ? `: ${status.error.message}` : '.'}`;
  return <p className="mb-4 text-body text-muted">{words}</p>;
}

function AddDialog({ org, open, onOpenChange }: { org: string; open: boolean; onOpenChange: (open: boolean) => void }) {
  const form = useFormOnComputer(org);
  const [name, setName] = useState('');
  const [kind, setKind] = useState<VaultKind>('password');
  const [domain, setDomain] = useState('');
  const [account, setAccount] = useState('');
  const ready = name.trim() && domain.trim() && ACCOUNT_PATTERN.test(account.trim());
  const done = form.stage === 'done';

  useEffect(() => {
    if (!done) return;
    toast('Saved on this computer');
    onOpenChange(false);
  }, [done, onOpenChange]);

  const reset = () => {
    form.reset();
    setName('');
    setDomain('');
    setAccount('');
    setKind('password');
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) reset();
        onOpenChange(next);
      }}
      title="Add a saved login"
      description="You type the secret on this computer, in a page it serves. agentos Cloud never sees it."
      footer={
        form.link ? (
          <Button asChild>
            <a href={form.link} target="_blank" rel="noreferrer">
              Open the page again
              <ArrowUpRight aria-hidden="true" className="size-4" />
            </a>
          </Button>
        ) : (
          <>
            <Button onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button
              variant="primary"
              loading={form.pending}
              disabled={!ready}
              onClick={() => void form.open('vault_entry', { name: name.trim(), kind, domain: domain.trim(), lanes: account.trim() })}
            >
              Continue on this computer
            </Button>
          </>
        )
      }
    >
      {form.failed ? <InlineError className="mb-4">That did not start. Nothing was changed.</InlineError> : null}
      {form.link ? (
        <p className="text-body text-muted" role="status">{FORM_WORDS[form.stage ?? 'waiting']}</p>
      ) : (
        <div className="grid gap-4">
          <Field label="Name"><Input value={name} onChange={(event) => setName(event.target.value)} placeholder="Shop Instagram" autoFocus /></Field>
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="grid gap-2 text-secondary font-medium">
              <span>Kind</span>
              <Select value={kind} onValueChange={(value) => setKind(value as VaultKind)} options={KIND_OPTIONS} aria-label="Kind" />
            </div>
            <Field label="Site"><Input value={domain} onChange={(event) => setDomain(event.target.value)} placeholder="instagram.com" /></Field>
          </div>
          <Field label="Account" hint="The account it signs in: the workspace, a slash, then the account's name."><Input className="mono" value={account} onChange={(event) => setAccount(event.target.value)} placeholder="aster/instagram" /></Field>
        </div>
      )}
    </Dialog>
  );
}

export function VaultSettings() {
  const { org } = useParams({ from: '/$org/settings/vault' });
  const vault = useVault(org);
  const access = useAccess(org);
  // The owner alone reads the state, locks, unlocks and adds: the node answers no one else for those.
  const owner = access.can('vault.open');
  const status = useVaultStatus(org, owner);
  const lock = useLockVault(org);
  const remove = useRemoveVaultEntry(org);
  const unlock = useFormOnComputer(org);
  const [query, setQuery] = useState('');
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState<VaultEntry | null>(null);
  const term = query.trim().toLowerCase();
  // An admin may remove a name, as the node allows, and sees no state to act on.
  // Removing a name is the computer's to do, so the cloud shows names and no control to remove one.
  const onComputer = useProviders().data?.code === true;
  const canManage = access.can('vault.manage') && onComputer;
  const unlocked = owner && status.data?.state === 'unsealed';
  const sealed = owner && status.data?.state === 'sealed';

  return (
    <>
      <SettingsHeader
        title="Vault"
        description="Names only. Secrets stay on your computers and never leave them."
        action={
          <div className="flex flex-wrap items-center justify-end gap-2">
            {sealed ? (
              <Button variant="primary" loading={unlock.pending} onClick={() => void unlock.open('vault_unlock')}>
                Unlock
              </Button>
            ) : null}
            {unlocked ? (
              <>
                <Button variant="ghost" loading={lock.isPending} onClick={() => lock.mutate()}>
                  Lock
                </Button>
                <Button onClick={() => setAdding(true)}>Add a saved login</Button>
              </>
            ) : null}
          </div>
        }
      />
      {unlock.failed ? <InlineError className="mb-4">That did not start. Nothing was changed.</InlineError> : null}
      {unlock.link && sealed ? (
        <p className="mb-4 text-body text-muted" role="status">
          {FORM_WORDS[unlock.stage ?? 'waiting']}{' '}
          <a className="underline underline-offset-2" href={unlock.link} target="_blank" rel="noreferrer">
            Open the page again
          </a>
        </p>
      ) : null}
      <StatusLine status={status.data} />
      {vault.data?.length ? (
        <div className="relative mb-4 max-w-xs">
          <Search className="pointer-events-none absolute top-2 left-2 size-4 text-faint" />
          <Input aria-label="Filter vault" placeholder="Filter by name or domain" className="pl-8" value={query} onChange={(event) => setQuery(event.target.value)} />
        </div>
      ) : null}
      <Card className="overflow-hidden">
        <QueryView
          query={vault}
          skeleton={
            <div className="grid gap-4 p-6">
              <Skeleton />
              <Skeleton />
              <Skeleton className="w-2/3" />
            </div>
          }
        >
          {(items) => {
            const rows = items.filter((item) => !term || item.name.includes(term) || item.domain?.includes(term));
            if (items.length === 0) {
              return (
                <EmptyState
                  icon={<LockKeyhole />}
                  title="The vault is empty"
                  description={owner ? 'Add a saved login here. This list then shows its name, never the secret.' : 'The owner adds saved logins on this computer. This list then shows their names, never the secret.'}
                  action={unlocked ? <Button variant="primary" onClick={() => setAdding(true)}>Add a saved login</Button> : undefined}
                />
              );
            }
            if (rows.length === 0) return <EmptyState title="Nothing matches that" className="py-10" />;
            return (
              <Table>
                <thead className="hidden md:table-header-group">
                  <tr>
                    <Th>Name</Th>
                    <Th className="w-36">Kind</Th>
                    <Th className="hidden xl:table-cell">Accounts</Th>
                    <Th className="w-28">Last used</Th>
                    {canManage ? (
                      <Th className="w-12">
                        <span className="sr-only">Actions</span>
                      </Th>
                    ) : null}
                  </tr>
                </thead>
                <tbody>
                  {rows.map((item) => (
                    <Tr key={item.id}>
                      <Td className={mainCell}>
                        <p className="mono truncate text-secondary">{item.name}</p>
                        <p className="truncate text-secondary text-faint">
                          {item.domain ?? 'No domain'}
                          <span className="md:hidden"> · {vaultKind[item.kind]}</span>
                        </p>
                      </Td>
                      <Td className="hidden text-secondary text-muted md:table-cell">{vaultKind[item.kind]}</Td>
                      <Td className="mono hidden max-w-56 truncate text-secondary text-muted xl:table-cell">{item.lanes.join(', ')}</Td>
                      <Td className="tabular text-secondary text-faint md:text-left">{item.last_used ? timeAgo(item.last_used) : 'Never'}</Td>
                      <Td className="pr-4 text-right md:pr-6">
                        {canManage ? (
                          <IconButton label={`Remove ${item.name}`} onClick={() => setRemoving(item)}>
                            <Trash2 className="size-4" />
                          </IconButton>
                        ) : null}
                      </Td>
                    </Tr>
                  ))}
                </tbody>
              </Table>
            );
          }}
        </QueryView>
      </Card>
      <AddDialog org={org} open={adding} onOpenChange={setAdding} />
      <ConfirmDialog
        open={removing !== null}
        onOpenChange={(open) => !open && setRemoving(null)}
        title={`Remove ${removing?.name ?? 'this login'}?`}
        description={`The saved login and its secret leave this computer. Accounts that use it will ask you to sign in again. This cannot be undone.`}
        confirmLabel="Remove the saved login"
        danger
        typeToConfirm={removing?.name}
        loading={remove.isPending}
        onConfirm={() => {
          if (!removing) return;
          remove.mutate(removing.id, { onSuccess: () => setRemoving(null) });
        }}
      />
    </>
  );
}
