/**
 * What agents remember about each workspace's work, on the Settings page (owner only: memory is the owner's,
 * in person, and the node refuses everyone else). The page answers first: how many things wait for a look.
 * Then each workspace's room against its caps, then the entries, which search and filter, and export as one file.
 */
import { useParams } from '@tanstack/react-router';
import { AlertTriangle } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useAccess, useExportMemory, useMemoryList, useMemoryWorkspaces, useWorkspaces } from '@/api/hooks';
import type { MemoryEntry, MemoryWorkspace } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Input } from '@/components/ui/field';
import { Meter } from '@/components/ui/misc';
import { Select } from '@/components/ui/select';
import { EmptyState, ErrorState, InlineError, NoAccess, QueryView, Skeleton } from '@/components/ui/states';
import { StatusDot } from '@/components/ui/status';
import { Table, Td, Th, Tr, mainCell, TableScroll } from '@/components/ui/table';
import { Checkbox } from '@/components/ui/toggle';
import { describeError } from '@/lib/errors';
import { timeAgo } from '@/lib/format';
import { memoryTrust, memoryType } from '@/lib/labels';
import { saveFile } from '@/lib/save-file';
import { cn } from '@/lib/cn';
import { SettingsHeader } from '@/features/settings/page';
import { EntrySheet, type EntryTarget } from './entry-sheet';
import { bytesText, keptSentence, RECORD_WARNING, recordSentence, TRUST_FILTERS, TYPE_FILTERS, waitingSentence, waitsForLook } from './memory-words';

export function MemorySettings() {
  const { org } = useParams({ from: '/$org/settings/memory' });
  const access = useAccess(org);
  if (!access.can('memory.read')) {
    return (
      <>
        <SettingsHeader title="Memory" />
        <Card>
          <NoAccess />
        </Card>
      </>
    );
  }
  return <MemoryAdmin org={org} />;
}

/** The text of a search box, read a moment after the person stops typing, so each letter is not a request. */
function useSettled(value: string, wait = 250): string {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setSettled(value), wait);
    return () => window.clearTimeout(timer);
  }, [value, wait]);
  return settled;
}

function MemoryAdmin({ org }: { org: string }) {
  const workspaces = useMemoryWorkspaces(org);
  const names = useWorkspaces(org);
  const nameOf = (id: string) => (names.data ?? []).find((item) => item.id === id)?.name ?? id;
  const [scope, setScope] = useState('');
  const [search, setSearch] = useState('');
  const [type, setType] = useState('all');
  const [trust, setTrust] = useState('all');
  const [waitingOnly, setWaitingOnly] = useState(false);
  const [open, setOpen] = useState<EntryTarget | null>(null);
  const settled = useSettled(search.trim());
  // "all" is the choice that filters nothing: a select cannot hold an empty value.
  const list = useMemoryList(org, {
    workspace: scope || undefined,
    q: settled || undefined,
    type: type === 'all' ? undefined : type,
    trust: trust === 'all' ? undefined : trust,
    unreviewed: waitingOnly || undefined,
  });
  const exportMemory = useExportMemory(org);

  const exportFile = (format: 'jsonl' | 'md') =>
    exportMemory.mutate({ workspace: scope || undefined, format }, { onSuccess: (file) => saveFile(file, `memory.${format}`) });

  return (
    <>
      <SettingsHeader
        title="Memory"
        description="What agents remember about each workspace's work. Only you see it, here. Nothing in it is run or followed, and text an agent took from a page is shown as plain text."
      />
      <QueryView query={workspaces} skeleton={<MemorySkeleton />} card>
        {(data) =>
          data.available && names.isPending ? (
            <MemorySkeleton />
          ) : data.available ? (
            <div className="grid gap-12">
              <section aria-labelledby="memory-rooms" className="grid gap-4">
                <p className="text-label" role="status">
                  <StatusDot tone={data.workspaces.some((row) => row.waiting > 0 || !row.chain_ok) ? 'warn' : 'neutral'} className="mr-2" />
                  {waitingSentence(data.workspaces.reduce((sum, row) => sum + row.waiting, 0))}
                </p>
                <h2 id="memory-rooms" className="sr-only">
                  Workspaces
                </h2>
                <RoomsTable rows={data.workspaces} nameOf={nameOf} scope={scope} onScope={setScope} />
              </section>

              <section aria-labelledby="memory-entries" className="grid gap-4">
                <div className="flex flex-wrap items-end justify-between gap-3">
                  <h2 id="memory-entries" className="text-section-title">
                    Entries
                  </h2>
                  <div className="flex flex-wrap gap-2">
                    <Button size="sm" loading={exportMemory.isPending && exportMemory.variables?.format === 'jsonl'} onClick={() => exportFile('jsonl')}>
                      Export as JSON lines
                    </Button>
                    <Button size="sm" loading={exportMemory.isPending && exportMemory.variables?.format === 'md'} onClick={() => exportFile('md')}>
                      Export as Markdown
                    </Button>
                  </div>
                </div>
                {exportMemory.isError ? (
                  <InlineError>
                    {describeError(exportMemory.error)}
                  </InlineError>
                ) : null}
                <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_200px_240px]">
                  <Input type="search" aria-label="Search memory" placeholder="Search what agents remember" value={search} onChange={(event) => setSearch(event.target.value)} />
                  <Select aria-label="Kind" value={type} onValueChange={setType} options={TYPE_FILTERS} />
                  <Select aria-label="Where it came from" value={trust} onValueChange={setTrust} options={TRUST_FILTERS} />
                </div>
                <div className="flex w-fit items-center gap-2">
                  <Checkbox id="memory-waiting" checked={waitingOnly} onCheckedChange={(checked) => setWaitingOnly(checked === true)} />
                  <label htmlFor="memory-waiting" className="cursor-pointer text-secondary">
                    Only what waits for a look
                  </label>
                </div>
                <EntryList
                  query={list}
                  nameOf={nameOf}
                  filtered={Boolean(scope || settled || type !== 'all' || trust !== 'all' || waitingOnly)}
                  onOpen={(entry) => setOpen({ id: entry.id, workspace: entry.workspace })}
                />
              </section>
            </div>
          ) : (
            <Card>
              <EmptyState
                icon={<AlertTriangle />}
                title="Memory is not on this computer"
                description="The memory service is not running here, so nothing is remembered or listed. Ask whoever runs the computer to turn it on."
              />
            </Card>
          )
        }
      </QueryView>
      <EntrySheet org={org} target={open} workspaceName={nameOf} onClose={() => setOpen(null)} />
    </>
  );
}

function MemorySkeleton() {
  return (
    <div className="grid gap-4">
      <Skeleton className="h-5 w-64" />
      <Skeleton className="h-40 w-full rounded-xl" />
    </div>
  );
}

function RoomsTable({ rows, nameOf, scope, onScope }: { rows: MemoryWorkspace[]; nameOf: (id: string) => string; scope: string; onScope: (id: string) => void }) {
  if (rows.length === 0) {
    return (
      <Card>
        <EmptyState title="Nothing remembered yet" description="When an agent remembers something about a workspace's work, the workspace shows here with its room." />
      </Card>
    );
  }
  return (
    <Card className="overflow-hidden">
      <TableScroll>
      <Table>
        <thead>
          <Tr>
            <Th className={mainCell}>Workspace</Th>
            <Th className="hidden md:table-cell">Kept</Th>
            <Th className="hidden md:table-cell">Text</Th>
            <Th>Waiting</Th>
            <Th className="hidden sm:table-cell">Record</Th>
          </Tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <RoomRow key={row.workspace} row={row} name={nameOf(row.workspace)} selected={scope === row.workspace} onSelect={() => onScope(scope === row.workspace ? '' : row.workspace)} />
          ))}
        </tbody>
      </Table>
      </TableScroll>
    </Card>
  );
}

function RoomRow({ row, name, selected, onSelect }: { row: MemoryWorkspace; name: string; selected: boolean; onSelect: () => void }) {
  return (
    <Tr className={cn(selected && 'bg-hover')}>
      <Td className={mainCell}>
        <button type="button" aria-pressed={selected} onClick={onSelect} className="block max-w-full truncate text-left text-label hover:underline focus-inset">
          {name}
        </button>
        <span className="block text-secondary text-faint sm:hidden">
          {keptSentence(row)} · {recordSentence(row)}
        </span>
      </Td>
      <Td className="hidden w-36 md:table-cell">
        <span className="tabular block text-secondary text-muted">{keptSentence(row)}</span>
        <Meter className="mt-2 shrink-0" value={row.live_entries} max={row.live_entries_cap} label={`Entries kept in ${name}`} />
      </Td>
      <Td className="hidden w-36 md:table-cell">
        <span className="tabular block text-secondary text-muted">
          {bytesText(row.content_bytes)} of {bytesText(row.content_bytes_cap)}
        </span>
        <Meter className="mt-2 shrink-0" value={row.content_bytes} max={row.content_bytes_cap} label={`Text kept in ${name}`} />
      </Td>
      <Td className="tabular text-secondary">{row.waiting > 0 ? <span className="inline-flex items-center gap-2"><StatusDot tone="warn" />{row.waiting}</span> : <span className="text-faint">0</span>}</Td>
      <Td className="hidden sm:table-cell">
        <span className={cn('inline-flex items-center gap-2 text-secondary', row.chain_ok ? 'text-muted' : 'text-bad')}>
          <StatusDot tone={row.chain_ok ? 'ok' : 'bad'} />
          {recordSentence(row)}
        </span>
        {!row.chain_ok ? <span className="mt-1 block text-caption text-muted">{RECORD_WARNING}</span> : null}
      </Td>
    </Tr>
  );
}

function EntryList({ query, nameOf, filtered, onOpen }: { query: ReturnType<typeof useMemoryList>; nameOf: (id: string) => string; filtered: boolean; onOpen: (entry: MemoryEntry) => void }) {
  if (query.isError) {
    return (
      <Card>
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      </Card>
    );
  }
  if (!query.data) {
    return (
      <Card className="grid gap-4 p-4">
        <Skeleton className="h-4 w-2/3" />
        <Skeleton className="h-4 w-1/2" />
      </Card>
    );
  }
  if (!query.data.available) return null;
  const entries = query.data.entries;
  if (entries.length === 0) {
    return (
      <Card>
        <EmptyState
          title={filtered ? 'Nothing matches' : 'Nothing kept yet'}
          description={filtered ? 'Nothing here matches these filters. Clear one to see more.' : 'Entries appear here as agents remember what they learn about the work.'}
        />
      </Card>
    );
  }
  return (
    <Card className="overflow-hidden">
      <ul className="divide-y divide-line">
        {entries.map((entry) => (
          <li key={`${entry.workspace}:${entry.id}`}>
            <button
              type="button"
              onClick={() => onOpen(entry)}
              className="focus-inset flex min-h-12 w-full items-center gap-3 px-4 py-2 text-left transition-colors duration-(--duration-base) ease-standard hover:bg-hover"
            >
              <span className="min-w-0 flex-1">
                <span className="block truncate text-label">{entry.title ?? 'Forgotten entry'}</span>
                <span className="block truncate text-secondary text-faint">
                  {memoryTrust[entry.trust]} · {memoryType[entry.type]} · {nameOf(entry.workspace)} · {timeAgo(entry.at)}
                </span>
              </span>
              {waitsForLook(entry) ? (
                <span className="inline-flex shrink-0 items-center gap-2 text-secondary text-muted">
                  <StatusDot tone="warn" />
                  <span className="hidden sm:inline">Waiting for a look</span>
                </span>
              ) : null}
            </button>
          </li>
        ))}
      </ul>
    </Card>
  );
}

