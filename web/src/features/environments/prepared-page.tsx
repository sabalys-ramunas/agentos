/**
 * Prepared machines (/environments/prepared, the owner's): what the node keeps ready ahead of an open, and
 * what that costs, the warm pool's worst month included. The last opens first, as a picture: seconds to ready on one scale, a dot each, filled
 * from a warm machine, ringed from a prepared one, hollow from cold, so the saving is visible before it is
 * read. Then each repository's prepared machine (built from which commit, when, the setup it saves, its
 * size and what it costs a month, building with its step or failed with the end of its log), then the warm
 * pool. Nothing here changes a setting: each change is the command the owner runs on the node.
 */
import { Link, useParams } from '@tanstack/react-router';
import { ChevronDown, Flame, Layers, Lock } from 'lucide-react';
import { useState } from 'react';
import type { ReactNode } from 'react';
import { usePreparedMachines } from '@/api/hooks';
import { ApiError, isNotServed } from '@/api/http';
import type { PreparedList, PreparedMachine, WarmPool } from '@/api/types';
import { BranchPill } from '@/components/ui/branch';
import { Card, CardHeader } from '@/components/ui/card';
import { CodeBlock, CopyButton, PageHeader } from '@/components/ui/misc';
import { StatStrip, StatTile } from '@/components/ui/stat-tile';
import { EmptyState, ErrorState, NotLive, Skeleton } from '@/components/ui/states';
import { Status } from '@/components/ui/status';
import { CellTitle, NoValue, RowActions, Table, TableEmpty, TableScroll, Td, Th, Tr } from '@/components/ui/table';
import { Terminal } from '@/components/ui/terminal';
import { usePageCrumb } from '@/components/shell/crumb';
import { cn } from '@/lib/cn';
import { dateTime, plural, timeAgo } from '@/lib/format';
import { seconds } from './opened-words';
import { euro, hourly, repoName } from './words';

const STATE = {
  ready: { label: 'Ready', tone: 'ok' },
  building: { label: 'Building', tone: 'live', pulse: true },
  failed: { label: 'Build failed', tone: 'bad' },
} as const;

export const prepareCommand = (repo: string) => `agentos environments prepare ${repoName(repo)}`;

export function PreparedPage() {
  const { org } = useParams({ from: '/$org/environments/prepared' });
  const query = usePreparedMachines(org);
  usePageCrumb('Prepared machines', { mono: false });
  return (
    <div className="grid gap-6">
      <PageHeader
        eyebrow={
          <Link to="/$org/environments" params={{ org }} className="text-caption text-muted hover:text-fg hover:underline">
            Environments
          </Link>
        }
        title="Prepared machines"
        description="A prepared machine is an image of a repository with its setup already run, so an open skips the install. The warm pool keeps a machine booted for the next open. Both cost money while they exist, and every euro is shown here."
      />
      {query.isPending ? (
        <div role="status" aria-label="Loading prepared machines" className="grid gap-6">
          <Skeleton className="h-40 w-full rounded-xl" />
          <Skeleton className="h-72 w-full rounded-xl" />
        </div>
      ) : isNotServed(query.error) ? (
        <Card>
          <NotLive />
        </Card>
      ) : query.error instanceof ApiError && query.error.status === 403 ? (
        <Card>
          <EmptyState icon={<Lock />} title="The owner's page" description="Prepared machines and the warm pool cost the owner money, so only the owner sees them here." />
        </Card>
      ) : !query.data ? (
        <Card>
          <ErrorState error={query.error} onRetry={() => void query.refetch()} />
        </Card>
      ) : (
        <PreparedBody data={query.data} />
      )}
    </div>
  );
}

/** Seconds as a stat tile's number and its unit: "2 h" and "7 min", "52" and "s", "0" and "min". */
function durationTile(total: number): { value: string; unit?: string } {
  const s = Math.max(0, Math.round(total));
  if (s < 60) return s ? { value: String(s), unit: 's' } : { value: '0', unit: 'min' };
  const minutes = Math.round(s / 60);
  if (minutes < 60) return { value: String(minutes), unit: 'min' };
  const hours = Math.floor(minutes / 60);
  return { value: `${hours} h`, unit: minutes % 60 ? `${minutes % 60} min` : undefined };
}

function PreparedBody({ data }: { data: PreparedList }) {
  const images = data.eur_month ?? data.machines.reduce((sum, machine) => sum + machine.cost_eur_month, 0);
  const month = data.month;
  const pool = data.pool;
  return (
    <>
      {month ? (
        <StatStrip>
          <StatTile label="Opens this month" value={String(month.opens)} hint={`${month.from_prepared} from a prepared machine, ${month.from_warm} of them warm`} />
          <StatTile label="Setup skipped" {...durationTile(month.setup_saved_s)} hint={month.setup_saved_s ? 'Time agents did not wait this month' : 'No open started from a prepared machine yet'} />
          <StatTile label="Images" value={euro(images)} unit="a month" hint={images ? `${plural(data.machines.filter((machine) => machine.gb).length, 'image')} at ${euro(data.eur_per_gb_month, 4)} a GB` : 'Local images cost nothing'} />
          <StatTile
            label="Warm pool"
            value={euro(pool.month_eur ?? 0)}
            unit="this month"
            hint={pool.target ? `${pool.warm} of ${pool.target} warm now${pool.worst_eur_month ? `, ${euro(pool.worst_eur_month)} a month at most` : ''}` : 'Off: nothing bills ahead of an open'}
          />
        </StatStrip>
      ) : null}
      {month?.recent?.length ? <OpenTimes recent={month.recent} /> : null}
      <MachinesCard machines={data.machines} />
      <PoolCard pool={data.pool} />
    </>
  );
}

const FROM = {
  warm: { label: 'Warm machine', mark: 'bg-fg border-fg' },
  prepared: { label: 'Prepared machine', mark: 'border-fg border-2 bg-card' },
  cold: { label: 'Cold open', mark: 'border-faint bg-card' },
} as const;

/** The last opens, each a dot at its seconds to ready, on a log scale from 1 s to 5 min. */
function OpenTimes({ recent }: { recent: NonNullable<NonNullable<PreparedList['month']>['recent']> }) {
  const lines = [1, 10, 60, 300];
  const y = (value: number) => 1 - Math.log10(Math.max(1, value)) / Math.log10(300);
  const median = (from: keyof typeof FROM) => {
    const values = recent.filter((open) => open.from === from).map((open) => open.seconds).sort((a, b) => a - b);
    return values.length ? values[Math.floor(values.length / 2)] : null;
  };
  return (
    <Card className="overflow-hidden">
      <CardHeader title="How fast they opened" meta={`the last ${recent.length} opens`} />
      <div className="grid gap-6 px-5 py-5 md:grid-cols-[minmax(0,1fr)_220px]">
        <figure aria-label="Seconds to ready of each recent open" className="grid grid-cols-[40px_minmax(0,1fr)] gap-2">
          <div aria-hidden="true" className="relative h-44">
            {lines.map((line) => (
              <span key={line} className="absolute right-0 -translate-y-1/2 font-mono text-code-caption text-faint tabular" style={{ top: `${y(line) * 100}%` }}>
                {line < 60 ? `${line} s` : `${line / 60} min`}
              </span>
            ))}
          </div>
          <div className="relative h-44">
            {lines.map((line) => (
              <span key={line} aria-hidden="true" className="absolute inset-x-0 h-px bg-chart-grid" style={{ top: `${y(line) * 100}%` }} />
            ))}
            {recent.map((open, index) => (
              <span
                key={open.at}
                title={`${seconds(open.seconds)}, ${FROM[open.from].label.toLowerCase()}, ${dateTime(open.at)}`}
                className={cn('absolute size-3 -translate-x-1/2 -translate-y-1/2 rounded-full border transition-transform duration-(--duration-fast) hover:scale-150', FROM[open.from].mark)}
                style={{ left: `${((index + 0.5) / recent.length) * 100}%`, top: `${y(open.seconds) * 100}%` }}
              />
            ))}
          </div>
        </figure>
        <ul aria-label="Legend" className="grid content-start gap-3">
          {(Object.keys(FROM) as (keyof typeof FROM)[]).map((from) => {
            const value = median(from);
            return (
              <li key={from} className="grid grid-cols-[12px_minmax(0,1fr)_auto] items-center gap-3">
                <span aria-hidden="true" className={cn('size-3 rounded-full border', FROM[from].mark)} />
                <span className="text-secondary text-muted">{FROM[from].label}</span>
                <span className="font-mono text-code-caption text-fg tabular">{value === null || value === undefined ? <NoValue /> : seconds(value)}</span>
              </li>
            );
          })}
          <li className="text-caption text-faint">The typical open of each kind. Oldest on the left.</li>
        </ul>
      </div>
    </Card>
  );
}

function MachinesCard({ machines }: { machines: PreparedMachine[] }) {
  const [open, setOpen] = useState<string | null>(machines.find((machine) => machine.state === 'failed')?.key ?? null);
  return (
    <Card className="overflow-hidden">
      <CardHeader
        title={
          <>
            <Layers aria-hidden="true" className="size-4 text-faint" />
            By repository
          </>
        }
        meta={plural(machines.length, 'prepared machine')}
      />
      <TableScroll>
        <Table stack aria-label="Prepared machines">
          <thead>
            <tr>
              <Th>Repository</Th>
              <Th>State</Th>
              <Th align="end" phone="hide">
                Setup it skips
              </Th>
              <Th align="end" phone="hide">
                Size
              </Th>
              <Th align="end">A month</Th>
              <Th align="end">Last used</Th>
              <Th className="w-1" phone="hide">
                <span className="sr-only">Actions</span>
              </Th>
            </tr>
          </thead>
          <tbody>
            {machines.length ? null : (
              <TableEmpty colSpan={7}>
                <EmptyState icon={<Layers />} title="No prepared machines yet" description="A repository with a setup in its .agentos/environment.toml gets one after its first open, so the opens after it skip the install." />
              </TableEmpty>
            )}
            {machines.map((machine) => (
              <MachineRow key={machine.key} machine={machine} expanded={open === machine.key} onToggle={() => setOpen((value) => (value === machine.key ? null : machine.key))} />
            ))}
          </tbody>
        </Table>
      </TableScroll>
      <div className="grid gap-2 border-t border-line px-5 py-4">
        <p className="text-secondary text-muted">A repository gets one when its .agentos/environment.toml has a setup. To build one now, or again, run on the node:</p>
        <CodeBlock code="agentos environments prepare <repository> --force" label="Copy the command" />
      </div>
    </Card>
  );
}

function MachineRow({ machine, expanded, onToggle }: { machine: PreparedMachine; expanded: boolean; onToggle: () => void }) {
  const failed = machine.state === 'failed';
  return (
    <>
      <Tr onActivate={failed ? onToggle : undefined} aria-expanded={failed ? expanded : undefined}>
        <Td main>
          <CellTitle
            title={<span className="font-mono text-code">{repoName(machine.repo)}</span>}
            second={
              <span className="flex min-w-0 items-center gap-2">
                {machine.ref ? <BranchPill name={machine.ref} /> : <span className="text-faint">default branch</span>}
                {machine.commit ? <span className="font-mono text-code-caption text-faint">{machine.commit.slice(0, 7)}</span> : null}
                {machine.built_at ? <span title={dateTime(machine.built_at)}>built {machine.age ?? timeAgo(machine.built_at)} ago</span> : null}
              </span>
            }
          />
        </Td>
        <Td>
          <span className="flex flex-col items-start gap-1">
            <Status status={STATE[machine.state]} />
            {machine.state === 'building' && machine.step ? <span className="text-caption text-faint">{machine.step}</span> : null}
            {failed ? (
              <span className="inline-flex items-center gap-1 text-caption text-faint">
                {expanded ? 'Hide' : 'Show'} the end of its log
                <ChevronDown aria-hidden="true" className={cn('size-3.5 transition-transform duration-(--duration-fast)', expanded && 'rotate-180')} />
              </span>
            ) : null}
          </span>
        </Td>
        <Td numeric label="Setup it skips" phone="hide">
          {machine.setup_s ? seconds(machine.setup_s) : <NoValue />}
        </Td>
        <Td numeric label="Size" phone="hide">
          {machine.gb ? `${machine.gb} GB` : <NoValue />}
        </Td>
        <Td numeric label="A month">
          {machine.cost_eur_month ? euro(machine.cost_eur_month) : <span className="text-faint">{euro(0)}</span>}
        </Td>
        <Td numeric label="Last used">
          {machine.last_used_at ? (
            <span title={`${dateTime(machine.last_used_at)}, ${plural(machine.uses, 'open')} from it`} className="text-muted">
              {timeAgo(machine.last_used_at)}
              <span className="block text-caption text-faint">{plural(machine.uses, 'open')}</span>
            </span>
          ) : (
            <span className="text-faint">Not yet</span>
          )}
        </Td>
        <Td className="w-1 text-right" phone="hide" onClick={(event) => event.stopPropagation()}>
          <RowActions>
            <CopyButton text={prepareCommand(machine.repo)} label={`Copy the command that builds ${repoName(machine.repo)} again`} />
          </RowActions>
        </Td>
      </Tr>
      {failed && expanded && machine.error ? (
        <tr>
          <td colSpan={7} className="border-t border-line px-5 py-4">
            <div className="grid gap-2">
              <p className="text-secondary text-fg">Opens of this repository run its setup on the machine until a build works. The last good image is kept meanwhile{machine.gb ? ` (${machine.gb} GB)` : ''}.</p>
              <Terminal lines={machine.error.split('\n')} label={`The end of the build log of ${repoName(machine.repo)}`} highlight={{ 1: 'bad', 2: 'bad' }} heightClass="max-h-48" />
            </div>
          </td>
        </tr>
      ) : null}
    </>
  );
}

function PoolCard({ pool }: { pool: WarmPool }) {
  const worst = pool.worst_eur_month ?? null;
  return (
    <Card className="overflow-hidden">
      <CardHeader
        title={
          <>
            <Flame aria-hidden="true" className="size-4 text-faint" />
            Warm pool
          </>
        }
        meta={pool.target ? `${pool.warm} of ${pool.target} warm` : 'Off'}
      />
      {pool.target ? (
        <div className="grid gap-x-8 gap-y-4 border-b border-line px-5 py-4 sm:grid-cols-3">
          <PoolFact label="At most a month" value={worst === null ? <NoValue /> : euro(worst)} hint={worst === null ? 'The node does not know the hourly price' : 'If no open ever claims a machine'} strong />
          <PoolFact label="This month so far" value={euro(pool.month_eur ?? 0)} hint={`${plural(pool.target, 'machine')} kept warm`} />
          <PoolFact label="Each machine" value={pool.price_hourly_eur ? hourly(pool.price_hourly_eur) : <NoValue />} hint={`For ${pool.minutes} min at most, unless an open claims it`} />
        </div>
      ) : null}
      {pool.machines.length ? (
        <ul className="divide-y divide-line">
          {pool.machines.map((machine) => (
            <li key={machine.vm} className="flex min-h-14 flex-wrap items-center gap-x-4 gap-y-1 px-5 py-3">
              <Status status={machine.state === 'ready' ? { label: 'Warm', tone: 'ok' } : { label: 'Booting', tone: 'live', pulse: true }} />
              <span className="font-mono text-code text-fg">{machine.vm}</span>
              <span className="text-secondary text-muted">
                {machine.server_type} in {machine.location}
                {machine.since ? ` · booted ${timeAgo(machine.since).toLowerCase()}` : ''}
              </span>
              <span className="ml-auto font-mono text-code-caption text-fg tabular" title={machine.price_hourly_eur ? hourly(machine.price_hourly_eur) : undefined}>
                {euro(machine.cost_eur ?? 0)} so far
              </span>
            </li>
          ))}
        </ul>
      ) : null}
      <div className="grid gap-3 px-5 py-4">
        <p className="text-secondary text-muted">
          {pool.target
            ? `A warm machine bills from its boot until an open claims it, or for ${pool.minutes} min at most, then it is deleted before its first billed hour ends and another takes its place. It counts against the machines you allow at once, and yields to any open.`
            : 'Off. Nothing bills until an agent opens a machine. With one warm machine, an open skips the rent and the boot.'}
        </p>
        <CodeBlock code={pool.target ? 'agentos environments allow --warm 0' : 'agentos environments allow --warm 1'} label="Copy the command" />
      </div>
    </Card>
  );
}

function PoolFact({ label, value, hint, strong = false }: { label: string; value: ReactNode; hint: string; strong?: boolean }) {
  return (
    <div className="grid content-start gap-1">
      <span className="text-caption text-faint">{label}</span>
      <span className={cn('font-mono tabular', strong ? 'text-section-title text-fg' : 'text-code text-fg')}>{value}</span>
      <span className="text-caption text-muted">{hint}</span>
    </div>
  );
}
