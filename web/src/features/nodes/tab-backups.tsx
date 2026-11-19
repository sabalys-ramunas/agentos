import { ArrowUpRight } from 'lucide-react';
import { useCheckBackup, useNodeBackups, useStartBackup } from '@/api/hooks';
import type { BackupItem, NodeBackups } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { CodeBlock } from '@/components/ui/misc';
import { Status } from '@/components/ui/status';
import { ErrorState, Skeleton } from '@/components/ui/states';
import { Table, Td, Th, Tr } from '@/components/ui/table';
import { cn } from '@/lib/cn';
import { plainReason } from '@/lib/plain-node';
import { dateTime, gb, timeAgo } from '@/lib/format';
import { backupCheck } from '@/lib/labels';
import { useTechnical } from '@/lib/technical';
import { Facts, NONE, Panel } from './parts';

const DAY = 24 * 60 * 60 * 1000;
const MIB = 1024 * 1024;

/** The one sentence a person reads first. It says what to do only when something needs doing. */
export function backupAnswer(data: NodeBackups, now = Date.now()): string {
  const job = data.job;
  if (job?.state === 'running') return job.kind === 'backup' ? 'Making a backup now.' : `Checking ${job.name ?? 'a backup'} now.`;
  const newest: BackupItem | undefined = data.items[0];
  if (!newest) return 'No backup yet. A dead disk would lose every login, so make one.';
  const ago = timeAgo(newest.created_at, now);
  if (newest.check.state === 'failed') return 'The last backup did not check out. Check it again, or make a new one.';
  if (now - Date.parse(newest.created_at) > 7 * DAY) return `The last backup was ${ago}. Make a new one.`;
  if (newest.check.state === 'not_checked') return `The last backup ran ${ago}. It has not been checked yet.`;
  return `The last backup ran ${ago} and checked out. ${data.items.length} of ${data.keep} are kept.`;
}

/**
 * This machine's backups: when the last one ran, whether it checked out, and how many are kept.
 * Making and checking one ask for the passphrase on the node's own form, so the app never holds it.
 * Restoring writes over the machine's data, so that stays on the command line, and says why.
 */
export function BackupsTab({ org, node, owner }: { org: string; node: string; owner: boolean }) {
  const backups = useNodeBackups(org, node);
  const start = useStartBackup(org, node);
  const check = useCheckBackup(org, node);
  const [technical] = useTechnical();

  if (backups.isPending) return <Skeleton className="h-96 rounded-xl" />;
  if (backups.isError) return <ErrorState error={backups.error} onRetry={() => void backups.refetch()} />;
  const data = backups.data;
  const running = data.job?.state === 'running';
  const busy = running || start.isPending || check.isPending;
  const newest = data.items[0];
  const formWaiting = start.isSuccess && start.data.form_url && !(data.job && Date.parse(data.job.started_at) >= (start.submittedAt ?? 0));

  // The form opens in a tab made in the click, because a tab opened after the answer comes back is blocked.
  const startBackup = () => {
    if (busy) return;
    const tab = window.open('', '_blank');
    start.mutate(undefined, {
      onSuccess: (trip) => {
        if (tab && trip.form_url) tab.location.href = trip.form_url;
        else tab?.close();
      },
      onError: () => tab?.close(),
    });
  };
  const checkBackup = (name: string) => {
    if (busy) return;
    const tab = window.open('', '_blank');
    check.mutate(name, {
      onSuccess: (trip) => {
        if (tab && trip.form_url) tab.location.href = trip.form_url;
        else tab?.close();
      },
      onError: () => tab?.close(),
    });
  };

  return (
    <div className="grid gap-10">
      <section aria-label="Backups" className="grid gap-4">
        <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-3">
          <p className="min-w-0 text-body" aria-live="polite">
            {backupAnswer(data)}
          </p>
          {owner ? (
            <div className="flex flex-wrap items-center gap-2">
              {newest && newest.check.state !== 'verified' ? (
                <Button size="sm" variant="secondary" aria-disabled={busy} onClick={() => checkBackup(newest.name)}>
                  Check the newest
                </Button>
              ) : null}
              <Button size="sm" variant={data.items.length ? 'secondary' : 'primary'} loading={start.isPending} aria-disabled={busy && !start.isPending} onClick={startBackup}>
                Make a backup
              </Button>
            </div>
          ) : null}
        </div>

        {running ? (
          <Card className="flex items-start gap-3 p-4 sm:p-6" role="status">
            <span aria-hidden="true" className="mt-2 size-2 shrink-0 animate-pulse-dot rounded-full bg-fg" />
            <div className="min-w-0">
              <p className="text-body">{data.job?.kind === 'backup' ? 'Making the backup.' : 'Checking the backup.'}</p>
              <p className="mt-1 text-secondary text-muted">
                Started {timeAgo(data.job?.started_at)}. It takes a few minutes, and each open account pauses for a moment while it is copied.
              </p>
            </div>
          </Card>
        ) : null}
        {formWaiting && start.data?.form_url ? (
          <Card className="flex flex-wrap items-center justify-between gap-3 p-4 sm:p-6">
            <p className="text-secondary text-muted">Type the passphrase in the form that opened, to start the backup.</p>
            <a href={start.data.form_url} target="_blank" rel="noreferrer" className="inline-flex h-8 items-center gap-1 text-secondary link hover:underline">
              Open the form again
              <ArrowUpRight aria-hidden="true" className="size-4" />
            </a>
          </Card>
        ) : null}
        {!running && data.job && data.job.state !== 'running' ? (
          <p className={cn('text-secondary', data.job.state === 'failed' ? 'text-bad' : 'text-muted')} role="status">
            {data.job.state === 'failed' ? 'It did not finish. ' : ''}
            {plainReason(data.job.message)}
          </p>
        ) : null}

        <Facts
          items={[
            { label: 'Last backup', value: newest ? timeAgo(newest.created_at) : NONE },
            { label: 'Last check', value: newest ? <Status status={backupCheck[newest.check.state]} /> : NONE },
            { label: 'Kept', value: `${data.items.length} of ${data.keep}` },
            { label: 'Size', value: newest ? gb(newest.size / MIB) : NONE, mono: true },
          ]}
        />
      </section>

      <Panel title="Every backup" description="Newest first. A backup is checked by reading all of it with the passphrase.">
        {data.items.length ? (
          <Card className="overflow-hidden">
            <Table>
              <thead>
                <tr className="border-b border-line">
                  <Th>Made</Th>
                  <Th className="hidden md:table-cell">Size</Th>
                  <Th>Checked</Th>
                  {owner ? <Th aria-label="Actions" /> : null}
                </tr>
              </thead>
              <tbody>
                {data.items.map((item) => (
                  <Tr key={item.name} className="h-12 border-b border-line last:border-b-0">
                    <Td className="max-w-0 pl-4 sm:pl-6">
                      <time dateTime={item.created_at} title={dateTime(item.created_at)} className="text-body">
                        {timeAgo(item.created_at)}
                      </time>
                      <p className="mono mt-1 truncate text-caption text-faint">{item.name}</p>
                    </Td>
                    <Td className="hidden md:table-cell">
                      <span className="tabular text-secondary text-muted">{gb(item.size / MIB)}</span>
                    </Td>
                    <Td>
                      <Status status={backupCheck[item.check.state]} />
                      {item.check.state === 'failed' && item.check.error ? <p className="mt-1 max-w-xs truncate text-caption text-muted">{item.check.error}</p> : null}
                    </Td>
                    {owner ? (
                      <Td className="pr-4 text-right sm:pr-6">
                        {item.check.state !== 'verified' ? (
                          <Button size="sm" variant="ghost" aria-disabled={busy} onClick={() => checkBackup(item.name)}>
                            Check it
                          </Button>
                        ) : null}
                      </Td>
                    ) : null}
                  </Tr>
                ))}
              </tbody>
            </Table>
          </Card>
        ) : (
          <Card className="p-4 text-secondary text-muted sm:p-6">None yet. Make one, and it shows here.</Card>
        )}
      </Panel>

      <Panel title="Restore" description="Restoring writes over everything on this machine, so it is done in a terminal, on purpose.">
        <Card className="grid gap-3 p-4 sm:p-6">
          <p className="text-secondary text-muted">
            A restore stops the accounts and replaces this machine's data with the backup. A browser should not start that, so it stays with the person at the machine.
          </p>
          <details open={technical} className="group">
            <summary className="flex cursor-pointer list-none items-center gap-2 text-secondary text-muted hover:text-fg [&::-webkit-details-marker]:hidden">
              Technical details
            </summary>
            <div className="mt-3 grid gap-3">
              <CodeBlock
                label="Restore a backup"
                code={`agentos backup restore ${data.folder}/<backup file> --passphrase-file <file with the passphrase>`}
              />
              <p className="text-caption text-faint">
                The backups are kept in <span className="mono">{data.folder}</span> on this machine's own disk. Copy one somewhere else too.
              </p>
            </div>
          </details>
        </Card>
      </Panel>
    </div>
  );
}
