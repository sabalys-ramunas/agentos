import { useParams } from '@tanstack/react-router';
import { Search } from 'lucide-react';
import { useMemo, useState } from 'react';
import { useAccess, useAudit, useEnvironments, useWorkspaces } from '@/api/hooks';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { AffixInput } from '@/components/ui/field';
import { Select } from '@/components/ui/select';
import { EmptyState, ErrorState, NoAccess, Skeleton } from '@/components/ui/states';
import { Table, Td, Th, Tr } from '@/components/ui/table';
import { dateTime } from '@/lib/format';
import { auditWords, placeNames } from '@/lib/plain-node';
import { useTechnical } from '@/lib/technical';
import { useRunsAlone } from '../nodes/add-node';
import { SettingsHeader } from './page';

/** What kind of event an action is, in the words of the filter: grant.issued is a Grant. */
const KINDS: Record<string, string> = {
  grant: 'Grants',
  member: 'Members',
  node: 'Computers',
  invite: 'Invites',
  session: 'Sign-ins',
  workspace: 'Workspaces',
  standing_approval: 'Standing approvals',
  plan: 'Plan',
  panel: 'Opened in openajar',
};
const kindOf = (action: string) => (action.split(' ').pop() ?? action).split('.')[0] ?? action;

export function AuditSettings() {
  const { org } = useParams({ from: '/$org/settings/audit' });
  const access = useAccess(org);
  const audit = useAudit(org);
  const workspaces = useWorkspaces(org);
  const environments = useEnvironments(org, 'all').data?.environments;
  const names = useMemo(() => placeNames(workspaces.data, environments), [workspaces.data, environments]);
  const [technical] = useTechnical();
  const alone = useRunsAlone(org);
  const all = audit.data?.pages.flatMap((page) => page.items) ?? [];
  const [query, setQuery] = useState('');
  const [kind, setKind] = useState('all');
  const [actor, setActor] = useState('all');
  const kinds = [...new Set(all.map((row) => kindOf(row.action)))];
  const actors = [...new Set(all.map((row) => row.actor))].sort();
  const needle = query.trim().toLowerCase();
  const rows = all.filter(
    (row) =>
      (kind === 'all' || kindOf(row.action) === kind) &&
      (actor === 'all' || row.actor === actor) &&
      (!needle || `${auditWords(row.action, row.target, names)} ${row.actor} ${row.ip ?? ''}`.toLowerCase().includes(needle)),
  );
  const filtered = Boolean(needle) || kind !== 'all' || actor !== 'all';
  if (!access.can('audit.read')) return <NoAccess />;
  return (
    <>
      <SettingsHeader title="Audit log" description={alone ? 'Admin and security events on this computer.' : 'Admin and security events in this organization.'} />
      {all.length ? (
        <div className="mb-4 grid gap-2 sm:grid-cols-[minmax(0,1fr)_minmax(0,11rem)_minmax(0,11rem)]">
          <AffixInput
            prefix={<Search aria-hidden className="size-4" />}
            type="search"
            aria-label="Search events"
            placeholder="Search events"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            className="[&>span:first-child]:border-r-0 [&>span:first-child]:bg-transparent [&>span:first-child]:pr-0"
          />
          <Select
            aria-label="Kind of event"
            value={kind}
            onValueChange={setKind}
            options={[{ value: 'all', label: 'Every event' }, ...kinds.map((value) => ({ value, label: KINDS[value] ?? value.replaceAll('_', ' ') }))]}
          />
          <Select aria-label="Who did it" value={actor} onValueChange={setActor} options={[{ value: 'all', label: 'Anyone' }, ...actors.map((value) => ({ value, label: value }))]} />
        </div>
      ) : null}
      <Card className="overflow-hidden">
        {audit.isPending ? (
          <div role="status" aria-label="Loading" className="grid gap-4 p-6">
            <Skeleton />
            <Skeleton />
            <Skeleton className="w-2/3" />
          </div>
        ) : audit.isError ? (
          <ErrorState error={audit.error} onRetry={() => void audit.refetch()} />
        ) : all.length === 0 ? (
          <EmptyState title="Nothing logged yet" description="Sign-ins, role changes and agent access are recorded here." />
        ) : rows.length === 0 && filtered ? (
          <EmptyState
            title="No events match"
            description={audit.hasNextPage ? 'Nothing in what is loaded. Older events may match: load them below.' : 'Try other words, or every event.'}
            action={
              <Button
                size="sm"
                onClick={() => {
                  setQuery('');
                  setKind('all');
                  setActor('all');
                }}
              >
                Clear the filters
              </Button>
            }
          />
        ) : (
          <Table>
            <thead className="hidden md:table-header-group">
              <tr>
                <Th className="w-36">Time</Th>
                <Th>Event</Th>
                <Th className="hidden w-36 lg:table-cell">IP address</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const words = auditWords(row.action, row.target, names);
                const head = auditWords(row.action, null, names);
                const rest = words.startsWith(head) ? words.slice(head.length) : '';
                return (
                <Tr key={row.id}>
                  <Td className="tabular hidden whitespace-nowrap text-secondary text-muted md:table-cell">{dateTime(row.at)}</Td>
                  <Td className="w-full max-w-0 pl-4 md:pl-4">
                    <p className="truncate text-body" title={words}>
                      <span className="font-medium text-fg">{rest ? head : words}</span>
                      {rest ? <span className="text-secondary text-faint">{rest}</span> : null}
                    </p>
                    {technical ? (
                      <p className="mono truncate text-code-caption text-faint">
                        {row.action} {row.target}
                      </p>
                    ) : null}
                    <p className="truncate text-secondary text-muted">
                      {row.actor}
                      <span className="md:hidden"> · {dateTime(row.at)}</span>
                    </p>
                  </Td>
                  <Td className="mono hidden text-code-caption text-muted lg:table-cell">{row.ip ?? '–'}</Td>
                </Tr>
                );
              })}
            </tbody>
          </Table>
        )}
        {audit.hasNextPage ? (
          <div className="flex justify-center border-t border-line p-2">
            <Button size="sm" variant="ghost" loading={audit.isFetchingNextPage} onClick={() => void audit.fetchNextPage()}>
              Load older
            </Button>
          </div>
        ) : null}
      </Card>
    </>
  );
}
