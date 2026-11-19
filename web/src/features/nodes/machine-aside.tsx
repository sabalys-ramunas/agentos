/**
 * One machine beside the table: its load, its lanes by kind with their state, its runs with a link
 * to each, its services, its health, the actions that exist today, and a way to every tab of its
 * full page. A disposable VM gets its time left, its price and what it is for.
 */
import { Link, useNavigate } from '@tanstack/react-router';
import { ArrowRight } from 'lucide-react';
import { useState } from 'react';
import type { ReactNode } from 'react';
import { toast } from 'sonner';
import {
  useAccess,
  useFreezeStatus,
  useNode,
  useNodeHealth,
  useNodeMetrics,
  useNodeServices,
  useNodeSystem,
  useProviders,
  useRemoveNode,
} from '@/api/hooks';
import { isNotServed } from '@/api/http';
import type { Job, Lane, NodeDetail, NodeMetrics, Vm } from '@/api/types';
import { TimeChart } from '@/components/charts/time-chart';
import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/dialog';
import { KindTile } from '@/components/ui/kind';
import { CodeBlock } from '@/components/ui/misc';
import { Status } from '@/components/ui/status';
import { ErrorState, Skeleton } from '@/components/ui/states';
import { FreezeButton } from '@/features/freeze/freeze';
import { cn } from '@/lib/cn';
import { computerName } from '@/lib/computers';
import { clock, plural, timeAgo } from '@/lib/format';
import { computerFreeze } from '@/lib/freeze';
import { laneStatus } from '@/lib/lanes';
import { BUSY_CPU, euros, gbShort, JOB_STATUS, jobKind, lastSeen, nodeStatus, timeLeft, upFor, vmStatus } from './model';
import type { NodeTab } from './search';
import { healthAnswer } from './tab-health';

/** Where each tab of the full page went, in words: the panel links to all twelve. */
export const TAB_WORDS: Record<NodeTab, { label: string; says: string }> = {
  lanes: { label: 'Accounts', says: 'Every account on it, by business' },
  workers: { label: 'Workers', says: 'Who thinks for the accounts here' },
  browsers: { label: 'Browsers', says: 'The browsers people use to watch' },
  schedules: { label: 'Schedules', says: 'What runs on its own, and when' },
  logs: { label: 'Logs', says: 'What it wrote, live' },
  installed: { label: 'Installed', says: 'Programs, services and tools' },
  environments: { label: 'Environments', says: 'Dev environments and their lanes' },
  resources: { label: 'Resources', says: 'CPU, memory and disk over time' },
  connection: { label: 'Connection', says: 'How it reaches agentos' },
  health: { label: 'Health', says: 'Its checks, and the fix for each' },
  backups: { label: 'Backups', says: 'Copies of its data, and their checks' },
  settings: { label: 'Settings', says: 'How this machine behaves' },
};

function Block({ title, meta, action, children, className }: { title: string; meta?: ReactNode; action?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section aria-label={title} className={cn('grid gap-3 border-t border-line py-4 first:border-t-0 first:pt-0', className)}>
      <div className="flex min-h-6 items-center gap-2">
        <h3 className="text-body font-semibold">{title}</h3>
        {meta ? <span className="text-secondary text-faint">{meta}</span> : null}
        {action ? <span className="ml-auto">{action}</span> : null}
      </div>
      {children}
    </section>
  );
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid min-w-0 gap-1">
      <dt className="text-caption text-faint">{label}</dt>
      <dd className="min-w-0 truncate text-body text-fg">{children}</dd>
    </div>
  );
}

function TabLink({ org, id, tab, children }: { org: string; id: string; tab: NodeTab; children: ReactNode }) {
  return (
    <Link to="/$org/nodes/$id" params={{ org, id }} search={{ tab }} className="inline-flex h-7 items-center gap-1 rounded-md px-2 text-secondary text-muted hover:bg-hover hover:text-fg">
      {children}
      <ArrowRight aria-hidden="true" className="size-3" />
    </Link>
  );
}

/** CPU and memory along the last hour: two small charts, one line each, with today's number beside. */
function Load({ node, metrics, quiet }: { node: NodeDetail; metrics: NodeMetrics | undefined | null; quiet: boolean }) {
  const memory = node.mem.total_mb > 0 ? Math.round((node.mem.used_mb / node.mem.total_mb) * 100) : null;
  const points = metrics?.points ?? [];
  const cpuPoints = points.map((point) => ({ at: point.at, value: point.cpu_pct }));
  const memPoints = points.map((point) => ({
    at: point.at,
    value: point.mem_used_mb !== null && point.mem_total_mb ? Math.round((point.mem_used_mb / point.mem_total_mb) * 100) : null,
  }));
  const charts = points.length > 1;
  return (
    <div className="grid gap-3">
      <div className="grid gap-1">
        <div className="flex items-baseline justify-between text-secondary">
          <span className="text-muted">CPU</span>
          <span className={cn('font-mono text-number tabular', node.cpu >= BUSY_CPU ? 'text-warn' : 'text-fg')}>{node.cpu}%</span>
        </div>
        {charts ? <TimeChart points={cpuPoints} label="CPU over the last hour" format={(value) => `${Math.round(value)}%`} bucketSeconds={metrics?.bucket_seconds ?? 60} height={56} max={100} axes={false} /> : null}
      </div>
      {memory !== null ? (
        <div className="grid gap-1">
          <div className="flex items-baseline justify-between text-secondary">
            <span className="text-muted">Memory</span>
            <span className="font-mono text-number text-fg tabular">
              {gbShort(node.mem.used_mb)} of {gbShort(node.mem.total_mb)}
            </span>
          </div>
          {charts ? <TimeChart points={memPoints} label="Memory over the last hour" format={(value) => `${Math.round(value)}%`} bucketSeconds={metrics?.bucket_seconds ?? 60} height={56} max={100} axes={false} /> : null}
        </div>
      ) : null}
      {charts ? <p className="text-caption text-faint">The last hour.</p> : quiet || metrics === undefined ? null : <p className="text-caption text-faint">This machine sends no load history yet, so only the number now is shown.</p>}
    </div>
  );
}

/** For a part the hosted app does not serve: the machine's own panel shows it. */
const NOT_HERE = 'Shown on the machine itself, not here.';

const KIND_GROUPS: { title: string; kinds: Lane['kind'][] }[] = [
  { title: 'Browsers', kinds: ['browser', 'android'] },
  { title: 'Terminals', kinds: ['terminal'] },
  { title: 'Services', kinds: ['service'] },
];
const SHOWN = 5;

/** A lane as one line: its name, who is in it, and its state. */
function LaneLine({ org, lane }: { org: string; lane: Lane }) {
  const used = lane.last_used ? `used ${timeAgo(lane.last_used).toLowerCase()}` : 'not used yet';
  const holder =
    lane.control.state === 'human'
      ? `${lane.control.by ?? 'A person'} has it`
      : lane.claim
        ? `${lane.claim.holder} is in it`
        : [lane.site?.label, used].filter(Boolean).join(', ');
  return (
    <li className="flex min-w-0 items-center gap-3">
      <Link
        to="/$org/lanes/$ws/$name"
        params={{ org, ws: lane.workspace, name: lane.name }}
        className="grid min-w-0 flex-1 rounded-sm hover:text-fg"
      >
        <span className="truncate font-mono text-code text-fg" title={lane.id}>
          {lane.id}
        </span>
        <span className="truncate text-secondary text-faint" title={holder}>
          {holder}
        </span>
      </Link>
      <Status status={laneStatus(lane)} />
    </li>
  );
}

function Lanes({ org, node }: { org: string; node: NodeDetail }) {
  const groups = KIND_GROUPS.map((group) => ({ ...group, lanes: node.lanes.filter((lane) => group.kinds.includes(lane.kind)) })).filter((group) => group.lanes.length);
  if (!groups.length) return <p className="text-body text-muted">No accounts, terminals or services on it yet.</p>;
  return (
    <div className="grid gap-4">
      {groups.map((group) => {
        // The lanes in use first, so what is happening is never below the fold.
        const sorted = [...group.lanes].sort((a, b) => Number(b.state === 'active') - Number(a.state === 'active') || a.id.localeCompare(b.id));
        return (
          <div key={group.title} className="grid gap-2">
            <p className="text-caption text-faint">
              {group.title} <span className="font-mono tabular">{group.lanes.length}</span>
              {group.lanes.some((lane) => lane.state === 'active') ? `, ${group.lanes.filter((lane) => lane.state === 'active').length} in use` : ''}
            </p>
            <ul className="grid gap-3">
              {sorted.slice(0, SHOWN).map((lane) => (
                <LaneLine key={lane.id} org={org} lane={lane} />
              ))}
            </ul>
            {group.lanes.length > SHOWN ? (
              <span>
                <TabLink org={org} id={node.id} tab="lanes">
                  {group.lanes.length - SHOWN} more
                </TabLink>
              </span>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

/** A run as one line, linked to its page. */
export function JobLine({ org, job }: { org: string; job: Job }) {
  const kind = jobKind(job);
  const when = job.finished_at ? `ended ${clock(job.finished_at)}` : `since ${clock(job.started_at)}`;
  return (
    <li>
      <Link to="/$org/runs/$id" params={{ org, id: job.id }} className="flex min-w-0 items-center gap-3 rounded-md hover:text-fg">
        {kind ? <KindTile kind={kind} size="sm" /> : <KindTile kind="lane" size="sm" />}
        <span className="grid min-w-0 flex-1">
          <span className="truncate font-mono text-code text-fg" title={job.command}>
            {job.command}
          </span>
          <span className="truncate text-secondary text-faint" title={`${job.lane}${job.started_by ? `, ${job.started_by}` : ''}, ${when}`}>
            {job.lane}
            {job.started_by ? `, ${job.started_by}` : ''}, {when}
          </span>
        </span>
        <Status status={JOB_STATUS[job.state]} />
      </Link>
    </li>
  );
}

/** What the node said about runs: the list, whether it can run any, or why they are not shown. */
export interface RunsInfo {
  /** Undefined while the first read is on its way. */
  jobs?: Job[];
  available?: boolean;
  /** Set when the runs are not shown here, in words. */
  hidden?: string;
}

function Runs({ org, id, runs }: { org: string; id: string; runs: RunsInfo }) {
  const { jobs, available, hidden } = runs;
  if (hidden) return <p className="text-body text-muted">{hidden}</p>;
  if (available === false) return <p className="text-body text-muted">No runs here: this machine has no terminal lanes to run them in.</p>;
  if (!jobs) return <Skeleton className="h-12" />;
  const running = jobs.filter((job) => job.state === 'running');
  const recent = jobs.filter((job) => job.state !== 'running').slice(0, Math.max(0, 3 - running.length));
  const shown = [...running, ...recent];
  if (!shown.length) return <p className="text-body text-muted">Nothing has run here yet. A run starts with <code className="font-mono text-code">agentos run</code>.</p>;
  return (
    <ul className="grid gap-3" aria-label={`Runs on ${id}`}>
      {shown.map((job) => (
        <JobLine key={job.id} org={org} job={job} />
      ))}
    </ul>
  );
}

function Services({ org, id }: { org: string; id: string }) {
  const services = useNodeServices(org, id);
  if (services.isPending) return <Skeleton className="h-10" />;
  if (isNotServed(services.error)) return <p className="text-body text-muted">{NOT_HERE}</p>;
  if (services.isError) return <p className="text-body text-muted">Its services could not be read just now.</p>;
  if (services.data === null) return <p className="text-body text-muted">This machine does not report its services yet.</p>;
  if (!services.data.length) return <p className="text-body text-muted">No services run on it.</p>;
  return (
    <ul className="grid gap-3">
      {services.data.slice(0, SHOWN).map((service) => (
        <li key={`${service.lane ?? ''}/${service.name}`} className="flex min-w-0 items-center gap-3">
          <span className="grid min-w-0 flex-1">
            <span className="truncate font-mono text-code text-fg">{service.name}</span>
            <span className="truncate text-secondary text-faint">
              {[service.lane, service.ports?.length ? `port ${service.ports.join(', ')}` : null, service.restarts ? plural(service.restarts, 'restart') : null].filter(Boolean).join(', ') || 'on the machine'}
            </span>
          </span>
          <Status status={service.state === 'running' ? { label: 'Running', tone: 'ok' } : service.state === 'failed' ? { label: 'Failed', tone: 'bad' } : { label: service.state, tone: 'idle' }} />
        </li>
      ))}
    </ul>
  );
}

function Health({ org, id }: { org: string; id: string }) {
  const health = useNodeHealth(org, id);
  if (health.isPending) return <Skeleton className="h-10" />;
  if (isNotServed(health.error)) return <p className="text-body text-muted">{NOT_HERE}</p>;
  if (health.isError) return <p className="text-body text-muted">Its health could not be read just now.</p>;
  if (health.data === null) return <p className="text-body text-muted">This machine does not report health checks yet.</p>;
  const checks = health.data;
  const wrong = checks.filter((check) => check.status !== 'ok').sort((a, b) => Number(b.status === 'fail') - Number(a.status === 'fail'));
  return (
    <div className="grid gap-3">
      <p className="text-body text-muted">{checks.length ? healthAnswer(checks) : 'No checks have run yet.'}</p>
      {wrong.length ? (
        <ul className="grid gap-3">
          {wrong.slice(0, 3).map((check) => (
            <li key={check.id ?? check.name} className="grid gap-1">
              <Status status={check.status === 'fail' ? { label: check.name, tone: 'bad' } : { label: check.name, tone: 'warn' }} />
              {check.detail ? <p className="text-secondary text-muted">{check.detail}</p> : null}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

/** The full page, tab by tab: every capability the old node page had stays one click away. */
function Everything({ org, id }: { org: string; id: string }) {
  return (
    <ul className="grid gap-px">
      {(Object.keys(TAB_WORDS) as NodeTab[]).map((tab) => (
        <li key={tab}>
          <Link
            to="/$org/nodes/$id"
            params={{ org, id }}
            search={{ tab }}
            className="group flex min-h-10 items-center gap-3 rounded-md px-2 hover:bg-hover"
          >
            <span className="w-28 shrink-0 text-body text-fg">{TAB_WORDS[tab].label}</span>
            <span className="min-w-0 flex-1 truncate text-secondary text-faint" title={TAB_WORDS[tab].says}>
              {TAB_WORDS[tab].says}
            </span>
            <ArrowRight aria-hidden="true" className="size-3 text-faint transition-transform duration-(--duration-fast) group-hover:translate-x-px" />
          </Link>
        </li>
      ))}
    </ul>
  );
}

function Actions({ org, node }: { org: string; node: NodeDetail }) {
  const navigate = useNavigate();
  const access = useAccess(org);
  const onComputer = useProviders().data?.code === true;
  const freezes = useFreezeStatus(org);
  const remove = useRemoveNode(org);
  const [confirming, setConfirming] = useState(false);
  const frozen = computerFreeze(freezes.data);
  const freezable = freezes.data !== undefined && freezes.data.available !== false;
  const canFreeze = access.can('freeze.computer') && freezable && !frozen;
  const canRemove = access.can('nodes.remove') && !onComputer;
  return (
    <div className="flex flex-wrap gap-2">
      <Button variant="secondary" asChild>
        <Link to="/$org/nodes/$id" params={{ org, id: node.id }} search={{ tab: 'lanes' }}>
          Open the full page
        </Link>
      </Button>
      {canFreeze ? <FreezeButton org={org} scope="node" name={computerName(node.id)} /> : null}
      {frozen ? <span className="self-center text-secondary text-warn">Agents are stopped on this machine.</span> : null}
      {canRemove ? (
        <Button variant="danger" onClick={() => setConfirming(true)}>
          Remove machine
        </Button>
      ) : null}
      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title={`Remove ${node.id}?`}
        description={`Accounts on this machine stop and show as read-only here. agentos forgets ${node.id}, so adding it again needs a new install command. Profiles and logins stay on the machine.`}
        confirmLabel="Remove machine"
        danger
        typeToConfirm={node.id}
        loading={remove.isPending}
        onConfirm={() =>
          remove.mutate(node.id, {
            onSuccess: () => {
              toast('Machine removed');
              setConfirming(false);
              void navigate({ to: '/$org/nodes', params: { org } });
            },
          })
        }
      />
    </div>
  );
}

/** What is on a node right now: the panel's top half, and the single machine's overview. */
export function NodeNow({ org, node, runs }: { org: string; node: NodeDetail; runs: RunsInfo }) {
  const metrics = useNodeMetrics(org, node.id, '1h');
  const mine = runs.jobs?.filter((job) => job.node === node.id);
  return (
    <>
      {node.state === 'down' ? (
        <Block title="Not answering">
          <p className="text-body text-muted">
            It has not answered since {lastSeen(node.last_seen)}. Its accounts are paused, and nothing was lost. When it comes back, agents see it again on their own.
          </p>
        </Block>
      ) : (
        <Block title="Load">
          <Load node={node} metrics={metrics.data} quiet={metrics.isError} />
        </Block>
      )}
      <Block title="Runs" action={mine?.length ? <TabLinkRuns org={org} id={node.id} /> : undefined}>
        <Runs org={org} id={node.id} runs={{ ...runs, jobs: mine }} />
      </Block>
      <Block title="Lanes" meta={plural(node.lanes.length, 'lane')} action={node.lanes.length ? <TabLink org={org} id={node.id} tab="lanes">All of them</TabLink> : undefined}>
        <Lanes org={org} node={node} />
      </Block>
    </>
  );
}

function TabLinkRuns({ org, id }: { org: string; id: string }) {
  return (
    <Link to="/$org/runs" params={{ org }} search={{ machine: id }} className="inline-flex h-7 items-center gap-1 rounded-md px-2 text-secondary text-muted hover:bg-hover hover:text-fg">
      All runs here
      <ArrowRight aria-hidden="true" className="size-3" />
    </Link>
  );
}

function Facts({ org, node }: { org: string; node: NodeDetail }) {
  const system = useNodeSystem(org, node.id);
  const { status } = nodeStatus(node);
  const cores = system.data?.cores;
  const size = [cores ? plural(cores, 'core') : null, node.mem.total_mb > 0 ? gbShort(node.mem.total_mb) : null].filter(Boolean).join(' · ');
  return (
    <dl className="grid grid-cols-2 gap-x-4 gap-y-3">
      <Fact label="Status">
        <Status status={status} />
      </Fact>
      {node.state === 'down' ? (
        <Fact label="Last seen">{lastSeen(node.last_seen)}</Fact>
      ) : (
        <Fact label="Up for">{system.data?.uptime_s ? upFor(system.data.uptime_s) : <span className="text-faint">Not reported</span>}</Fact>
      )}
      <Fact label="Size">{size || <span className="text-faint">Not reported</span>}</Fact>
      <Fact label="Runs">
        <span title={`${node.os} on ${node.arch}, agentos ${node.version}`}>
          {node.os} · <span className="font-mono text-code">v{node.version}</span>
        </span>
      </Fact>
    </dl>
  );
}

export function NodePanel({ org, id, runs }: { org: string; id: string; runs: RunsInfo }) {
  const node = useNode(org, id);
  if (node.isPending) {
    return (
      <div role="status" aria-label="Loading" className="grid gap-4">
        <Skeleton className="h-16" />
        <Skeleton className="h-32" />
        <Skeleton className="h-40" />
      </div>
    );
  }
  if (node.isError) return <ErrorState error={node.error} onRetry={() => void node.refetch()} className="py-8" />;
  const data = node.data;
  return (
    <div className="grid">
      <Block title="About it">
        <Facts org={org} node={data} />
      </Block>
      <NodeNow org={org} node={data} runs={runs} />
      <Block title="Services" action={<TabLink org={org} id={id} tab="installed">Installed</TabLink>}>
        <Services org={org} id={id} />
      </Block>
      <Block title="Health" action={<TabLink org={org} id={id} tab="health">All checks</TabLink>}>
        <Health org={org} id={id} />
      </Block>
      <Block title="Do something">
        <Actions org={org} node={data} />
      </Block>
      <Block title="Everything about it">
        <Everything org={org} id={id} />
      </Block>
    </div>
  );
}

export function VmPanel({ org, vm, runs }: { org: string; vm: Vm; runs: RunsInfo }) {
  const { status } = vmStatus(vm);
  const left = timeLeft(vm);
  const [ws = '', name = ''] = vm.lane.split('/');
  const mine = runs.jobs?.filter((job) => job.lane === vm.lane);
  return (
    <div className="grid">
      <Block title="Rented for one task">
        <dl className="grid grid-cols-2 gap-x-4 gap-y-3">
          <Fact label="Status">
            <Status status={status} />
          </Fact>
          <Fact label="Deletes itself">{vm.expires_at ? `${clock(vm.expires_at)}${left ? `, ${left}` : ''}` : <span className="text-faint">Not set</span>}</Fact>
          <Fact label="Price">{vm.price_hourly_eur !== null ? `${euros(vm.price_hourly_eur, 4)} an hour` : <span className="text-faint">Not known</span>}</Fact>
          <Fact label="So far">{vm.cost_eur_so_far !== null ? euros(vm.cost_eur_so_far) : <span className="text-faint">Not known</span>}</Fact>
          <Fact label="Size">
            <span className="font-mono text-code">{vm.server_type}</span>
            {vm.profile ? `, ${vm.profile}` : ''}
          </Fact>
          <Fact label="Where">{vm.location || <span className="text-faint">Not known</span>}</Fact>
        </dl>
        {vm.purpose ? (
          <div className="grid gap-1">
            <p className="text-caption text-faint">What it is for{vm.started_by ? `, as ${vm.started_by.toLowerCase()} said` : ''}</p>
            <p className="text-body text-fg">{vm.purpose}</p>
          </div>
        ) : null}
      </Block>
      <Block title="Runs" action={mine?.length ? <TabLinkRuns org={org} id={vm.id} /> : undefined}>
        <Runs org={org} id={vm.id} runs={{ ...runs, jobs: mine }} />
      </Block>
      <Block title="Its lane">
        <Link to="/$org/lanes/$ws/$name" params={{ org, ws, name }} className="inline-flex items-center gap-1 font-mono text-code text-fg hover:underline">
          {vm.lane}
          <ArrowRight aria-hidden="true" className="size-3" />
        </Link>
      </Block>
      <Block title="Delete it now">
        <p className="text-body text-muted">It deletes itself on time even if the agent forgets. To end it sooner, run this on the node:</p>
        <CodeBlock code={`agentos vm destroy ${vm.id}`} label="Copy the delete command" />
      </Block>
    </div>
  );
}
