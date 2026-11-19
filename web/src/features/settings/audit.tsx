import { useParams } from '@tanstack/react-router';
import { useMemo } from 'react';
import { useAccess, useAudit, useEnvironments, useWorkspaces } from '@/api/hooks';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { EmptyState, ErrorState, NoAccess, Skeleton } from '@/components/ui/states';
import { Table, Td, Th, Tr } from '@/components/ui/table';
import { dateTime } from '@/lib/format';
import { auditWords, placeNames } from '@/lib/plain-node';
import { useTechnical } from '@/lib/technical';
import { useRunsAlone } from '../nodes/add-node';
import { SettingsHeader } from './page';

export function AuditSettings() {
  const { org } = useParams({ from: '/$org/settings/audit' });
  const access = useAccess(org);
  const audit = useAudit(org);
  const workspaces = useWorkspaces(org);
  const environments = useEnvironments(org, 'all').data?.environments;
  const names = useMemo(() => placeNames(workspaces.data, environments), [workspaces.data, environments]);
  const [technical] = useTechnical();
  const alone = useRunsAlone(org);
  const rows = audit.data?.pages.flatMap((page) => page.items) ?? [];
  if (!access.can('audit.read')) return <NoAccess />;
  return (
    <>
      <SettingsHeader title="Audit log" description={alone ? 'Admin and security events on this computer.' : 'Admin and security events in this organization.'} />
      <Card className="overflow-hidden">
        {audit.isPending ? (
          <div role="status" aria-label="Loading" className="grid gap-4 p-6">
            <Skeleton />
            <Skeleton />
            <Skeleton className="w-2/3" />
          </div>
        ) : audit.isError ? (
          <ErrorState error={audit.error} onRetry={() => void audit.refetch()} />
        ) : rows.length === 0 ? (
          <EmptyState title="Nothing logged yet" description="Sign-ins, role changes and agent access are recorded here." />
        ) : (
          <Table>
            <thead className="hidden md:table-header-group">
              <tr>
                <Th className="w-36">Time</Th>
                <Th>Event</Th>
                <Th className="hidden w-36 xl:table-cell">IP</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <Tr key={row.id}>
                  <Td className="tabular hidden text-secondary whitespace-nowrap text-faint md:table-cell">{dateTime(row.at)}</Td>
                  <Td className="w-full max-w-0 pl-4 md:pl-4">
                    <p className="truncate text-secondary">{auditWords(row.action, row.target, names)}</p>
                    {technical ? (
                      <p className="mono truncate text-caption text-faint">
                        {row.action} {row.target}
                      </p>
                    ) : null}
                    <p className="truncate text-caption text-faint">
                      {row.actor}
                      <span className="md:hidden"> · {dateTime(row.at)}</span>
                    </p>
                  </Td>
                  <Td className="mono hidden text-secondary text-faint xl:table-cell">{row.ip ?? '–'}</Td>
                </Tr>
              ))}
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
