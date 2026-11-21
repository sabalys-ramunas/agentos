import { useMemo, useState } from 'react';
import { useAddSite, useNodeIdentity, useRemoveSite, useWorkspaces } from '@/api/hooks';
import type { IdentityAccount } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Input, Label } from '@/components/ui/field';
import { Status } from '@/components/ui/status';
import { ErrorState, InlineError, Skeleton } from '@/components/ui/states';
import { plural, timeAgo } from '@/lib/format';
import { identityState } from '@/lib/labels';
import { placeName, placeNames } from '@/lib/plain-node';
import { useTechnical } from '@/lib/technical';
import { NotReported, Panel, TypedCommand } from './parts';

/** The statement a site is shown for an account that signs its requests (the same words as the command line). */
const STATEMENT = 'On the listed sites this account identifies itself as an automated agent.';

/** The one sentence a person reads first: which accounts sign, and to how many sites. */
export function identityAnswer(accounts: IdentityAccount[]): string {
  const signing = accounts.filter((account) => account.state === 'signing');
  const sites = signing.reduce((count, account) => count + account.hosts.length, 0);
  if (!accounts.length) return 'No account on this machine yet.';
  if (!signing.length) return 'No account signs its requests yet. Add a site to one of them.';
  return `${plural(signing.length, 'account')} ${signing.length === 1 ? 'signs' : 'sign'} its requests, to ${plural(sites, 'site')}.`;
}

function accountSentence(account: IdentityAccount): string {
  switch (account.state) {
    case 'signing':
      return `Signs its requests to ${plural(account.hosts.length, 'site')} below. ${STATEMENT}`;
    case 'off':
      return 'Signs nothing, so it has no route. Add a site to start.';
    case 'stale':
      return 'Its profile changed since its key was made, so it needs a new key. That is made in a terminal on the machine.';
    case 'unreadable':
      return 'Its identity files cannot be read, so nothing here can change them. Look at them in a terminal on the machine.';
    default:
      return 'It has no identity yet. Make one in a terminal on the machine, with the address its signature comes from.';
  }
}

function AccountIdentity({ org, node, account, owner, title }: { org: string; node: string; account: IdentityAccount; owner: boolean; title: string }) {
  const add = useAddSite(org, node);
  const remove = useRemoveSite(org, node);
  const [technical] = useTechnical();
  const [origin, setOrigin] = useState('');
  const [reason, setReason] = useState('');
  const canChange = owner && (account.state === 'signing' || account.state === 'off');
  const busy = add.isPending || remove.isPending;

  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    if (busy || !origin.trim() || !reason.trim()) return;
    add.mutate({ lane: account.lane, origin: origin.trim(), reason: reason.trim() }, { onSuccess: () => { setOrigin(''); setReason(''); } });
  };

  return (
    <Card className="grid gap-3 p-4 sm:p-6">
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <div className="min-w-0">
          <p className="truncate text-body">{title}</p>
          <TypedCommand command={account.lane} className="mt-0" />
        </div>
        <Status status={identityState[account.state]} />
      </div>
      <p className="text-secondary text-muted">{accountSentence(account)}</p>

      {account.hosts.length ? (
        <ul className="divide-y divide-line border-t border-line">
          {account.hosts.map((host) => (
            <li key={host.origin} className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 py-3">
              <div className="min-w-0">
                <p className="mono truncate text-secondary">{host.origin}</p>
                <p className="truncate text-caption text-muted">
                  {host.reason}
                  {host.added_at ? `, added ${timeAgo(host.added_at)}` : ''}
                </p>
              </div>
              {canChange ? (
                <Button
                  size="sm"
                  variant="ghost"
                  loading={remove.isPending && remove.variables?.origin === host.origin}
                  onClick={() => {
                    if (!busy) remove.mutate({ lane: account.lane, origin: host.origin });
                  }}
                >
                  Stop signing
                </Button>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}

      {canChange ? (
        <form onSubmit={submit} className="grid gap-3 border-t border-line pt-3 sm:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] sm:items-end" aria-label={`Add a site for ${account.lane}`}>
          <div className="grid gap-2">
            <Label htmlFor={`origin-${account.lane}`}>Site</Label>
            <Input id={`origin-${account.lane}`} mono value={origin} onChange={(event) => setOrigin(event.target.value)} placeholder="https://example.com" autoComplete="off" spellCheck={false} />
          </div>
          <div className="grid gap-2">
            <Label htmlFor={`reason-${account.lane}`}>Why</Label>
            <Input id={`reason-${account.lane}`} value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Read-only research" autoComplete="off" />
          </div>
          <Button type="submit" variant="secondary" loading={add.isPending} aria-disabled={busy} className="justify-self-start sm:justify-self-auto">
            Add site
          </Button>
          {add.isError ? (
            <InlineError className="sm:col-span-3">
              {(add.error as Error).message}
            </InlineError>
          ) : null}
        </form>
      ) : null}

      {account.state === 'none' ? (
        <div className="text-secondary">
          <p>Making one takes a terminal on the machine. Technical details has the command.</p>
          <TypedCommand command={`openajar identity init ${account.lane} --agent <https URL>`} />
        </div>
      ) : null}

      {technical && account.keyid ? (
        <p className="text-caption text-faint">
          Key id <span className="mono">{account.keyid}</span>. The private key stays on this machine and is never shown.
        </p>
      ) : null}
    </Card>
  );
}

/**
 * Each account's Web Bot identity: whether it signs its requests, to which sites, and why. A site is added
 * or taken away by the owner, in person. The key is made on the command line and never leaves the node.
 */
export function IdentityPanel({ org, node, owner }: { org: string; node: string; owner: boolean }) {
  const identity = useNodeIdentity(org, node);
  const workspaces = useWorkspaces(org);
  const names = useMemo(() => placeNames(workspaces.data), [workspaces.data]);
  if (identity.isPending) return <Skeleton className="h-48 rounded-xl" />;
  if (identity.isError) return <ErrorState error={identity.error} onRetry={() => void identity.refetch()} />;
  if (identity.data === null) return <NotReported node={node} what="its identities" />;
  const accounts = identity.data.accounts;
  // An account with no identity yet is one quiet line, not a card of its own.
  const unset = accounts.filter((account) => account.state === 'none');
  const configured = accounts.filter((account) => account.state !== 'none');
  return (
    <Panel title="Identity on sites" description="Some sites want to know which automated agent is calling. An account can sign its requests so they can tell.">
      <p className="text-body" aria-live="polite">
        {identityAnswer(accounts)}
      </p>
      {configured.length ? (
        <div className="grid gap-3">
          {configured.map((account) => (
            <AccountIdentity key={account.lane} org={org} node={node} account={account} owner={owner} title={placeName(account.lane, names)} />
          ))}
        </div>
      ) : null}
      {unset.length ? (
        <Card className="grid gap-2 p-4 text-secondary sm:p-6">
          <p className="text-muted">
            {unset.length === 1 ? 'This account has' : 'These accounts have'} no identity yet:{' '}
            {unset.map((account, index) => (
              <span key={account.lane}>
                <span className="text-fg">{placeName(account.lane, names)}</span>
                {index < unset.length - 1 ? ', ' : ''}
              </span>
            ))}
            .
          </p>
          <p>Making an identity takes a terminal on the machine. Technical details has the command.</p>
          <TypedCommand command="openajar identity init <account> --agent <https URL>" />
        </Card>
      ) : null}
      {owner && configured.length ? <p className="text-caption text-faint">Changes take effect when the account next opens.</p> : null}
    </Panel>
  );
}
