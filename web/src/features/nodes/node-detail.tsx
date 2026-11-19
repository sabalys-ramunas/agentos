/**
 * `/machines/<id>`: with no tab, the Machines table with this machine open beside it. With a tab, the
 * machine's full page: About This Mac, Activity Monitor and Console in one. A live header says what
 * the machine is and does, and the twelve tabs hold the depth. Every view is in the URL.
 */
import { Link, useNavigate, useParams, useSearch } from '@tanstack/react-router';
import { ChevronLeft } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { toast } from 'sonner';
import {
  useAccess,
  useFreezeStatus,
  useNode,
  useNodeConnections,
  useNodeHealth,
  useNodeMetrics,
  useNodePrograms,
  useNodeSchedules,
  useNodeServices,
  useNodeSystem,
  useNodeTools,
  useProviders,
  useRemoveNode,
  useTopology,
  useWorkspaces,
} from '@/api/hooks';
import type { LogLevel, NodeDetail } from '@/api/types';
import type { NodeSearch, NodeTab } from './search';
import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/dialog';
import { FreezeButton, FrozenNotice } from '@/features/freeze/freeze';
import { Skeleton } from '@/components/ui/states';
import { QueryView } from '@/components/ui/states';
import { Tab, TabList, TabPanel, Tabs } from '@/components/ui/tabs';
import { cn } from '@/lib/cn';
import { computerFreeze } from '@/lib/freeze';
import { computerName } from '@/lib/computers';
import { localZone } from '@/lib/zone';
import { describeNode, incidentsFrom } from './describe';
import { NodeHeader } from './header';
import { ConnectionTab } from './tab-connection';
import { BackupsTab } from './tab-backups';
import { EnvironmentsTab } from './tab-environments';
import { HealthTab } from './tab-health';
import { InstalledTab } from './tab-installed';
import { LanesTab } from './tab-lanes';
import { LogsTab } from './tab-logs';
import { ResourcesTab } from './tab-resources';
import { SchedulesTab } from './tab-schedules';
import { SettingsTab } from './tab-settings';
import { WorkersTab } from '@/features/workers/on-computer';
import { BrowsersTab } from './tab-browsers';
import { Machines } from './machines';
import { TAB_WORDS } from './machine-aside';

const LABELS = Object.fromEntries(Object.entries(TAB_WORDS).map(([tab, words]) => [tab, words.label])) as Record<NodeTab, string>;

/** A tab's count sits in a fixed slot, so a number arriving never nudges the tab bar. */
function TabCount({ value, tone }: { value: number | null | undefined; tone?: 'warn' }) {
  if (value === null || value === undefined) return null;
  return <span className={cn('tabular ml-2 text-caption', tone === 'warn' ? 'text-warn' : 'text-faint')}>{value}</span>;
}

function Body({ org, node }: { org: string; node: NodeDetail }) {
  const search = useSearch({ from: '/$org/nodes/$id' });
  const navigate = useNavigate({ from: '/$org/nodes/$id' });
  const access = useAccess(org);
  // A machine on its own has nothing to be removed from.
  const onComputer = useProviders().data?.code === true;
  const remove = useRemoveNode(org);
  const freezes = useFreezeStatus(org);
  const [confirming, setConfirming] = useState(false);
  const tab = search.tab ?? 'lanes';
  const id = node.id;
  const frozen = computerFreeze(freezes.data);
  const freezable = freezes.data !== undefined && freezes.data.available !== false;
  // Health, backups, environments, identity and settings change or read this machine as its owner, so only the owner sees them act.
  const owner = access.role === 'owner';

  const system = useNodeSystem(org, id);
  const schedules = useNodeSchedules(org, id);
  const programs = useNodePrograms(org, id);
  const services = useNodeServices(org, id);
  const tools = useNodeTools(org, id);
  const health = useNodeHealth(org, id);
  const connections = useNodeConnections(org, id);
  const hour = useNodeMetrics(org, id, '1h');
  const day = useNodeMetrics(org, id, '24h');
  const range = search.range ?? '24h';
  const ranged = useNodeMetrics(org, id, range);
  const workspaces = useWorkspaces(org).data ?? [];
  const topology = useTopology(org).data;

  const bands = useMemo(() => incidentsFrom(connections.data), [connections.data]);
  const brains = useMemo(
    () =>
      (topology?.links ?? [])
        .filter((link) => link.kind === 'uses_brain' && link.from === `node:${id}`)
        .map((link) => topology?.entities.find((entity) => entity.id === link.to)?.label ?? link.to.replace(/^brain:/, ''))
        .sort(),
    [topology, id],
  );
  const owners = workspaces.filter((workspace) => node.lanes.some((lane) => lane.workspace === workspace.id)).map((workspace) => workspace.name);
  const sentence = schedules.isPending || !topology ? null : describeNode({ lanes: node.lanes.length, workspaces: owners, brains, schedules: schedules.data ?? null });
  const zone = system.data?.timezone ?? node.timezone ?? localZone();
  const warnings = health.data?.filter((check) => check.status !== 'ok').length;

  const setSearch = useCallback(
    (next: Partial<NodeSearch>) =>
      void navigate({ search: (previous) => ({ ...previous, ...next }), replace: true, resetScroll: false }),
    [navigate],
  );
  const onFilters = useCallback((next: { source?: string; level?: LogLevel; q?: string }) => {
    const level = 'level' in next ? (next.level === 'info' ? undefined : next.level) : undefined;
    setSearch({ ...next, ...('level' in next ? { level } : {}) } as Partial<NodeSearch>);
  }, [setSearch]);

  // On a phone the tab bar scrolls, so the chosen tab is brought into view, sideways only.
  // Counts arrive late and widen the tabs, so this runs again when they land.
  const tabBar = useRef<HTMLDivElement>(null);
  const schedulesCount = schedules.data?.length;
  const programsCount = programs.data?.length;
  useEffect(() => {
    const bar = tabBar.current;
    const active = bar?.querySelector<HTMLElement>('[data-state="active"]');
    if (!bar || !active) return;
    // The bar is sticky, so it is the offset parent and offsetLeft is already within it.
    const left = active.offsetLeft;
    const right = left + active.offsetWidth;
    if (left < bar.scrollLeft) bar.scrollTo({ left: Math.max(0, left - 16) });
    else if (right > bar.scrollLeft + bar.clientWidth) bar.scrollTo({ left: right - bar.clientWidth + 16 });
  }, [tab, warnings, schedulesCount, programsCount]);

  const counts: Partial<Record<NodeTab, React.ReactNode>> = {
    lanes: <TabCount value={node.lanes.length} />,
    schedules: <TabCount value={schedules.data?.length} />,
    installed: <TabCount value={programs.data?.length} />,
    health: warnings ? <TabCount value={warnings} tone="warn" /> : null,
  };

  return (
    <>
      <NodeHeader
        node={node}
        sentence={sentence}
        system={system.data}
        hour={hour.data}
        day={day.data}
        connections={connections.data}
        bands={bands}
        frozen={frozen}
        actions={
          <>
            {access.can('freeze.computer') && freezable && !frozen ? <FreezeButton org={org} scope="node" name={computerName(id)} /> : null}
            {access.can('nodes.remove') && !onComputer ? (
              <Button size="sm" variant="danger" onClick={() => setConfirming(true)}>
                Remove machine
              </Button>
            ) : null}
          </>
        }
      />
      {frozen ? (
        <FrozenNotice org={org} record={frozen} name={computerName(id)} canLift={access.can('freeze.lift')} className="mt-10" />
      ) : null}
      <Tabs value={tab} onValueChange={(next) => setSearch({ tab: next as NodeTab })} className="mt-10">
        <TabList ref={tabBar} aria-label="Machine" className="sticky top-(--topbar-height) z-10 bg-card">
          {(Object.keys(LABELS) as NodeTab[]).map((key) => (
            <Tab key={key} value={key}>
              {LABELS[key]}
              {counts[key]}
            </Tab>
          ))}
        </TabList>
        <div className="pt-8">
          <TabPanel value="lanes">
            <LanesTab org={org} node={node} workspaces={workspaces} />
          </TabPanel>
          <TabPanel value="workers">
            <WorkersTab org={org} node={node} />
          </TabPanel>
          <TabPanel value="browsers">
            <BrowsersTab org={org} />
          </TabPanel>
          <TabPanel value="schedules">
            {schedules.isPending ? <Skeleton className="h-64 rounded-xl" /> : <SchedulesTab org={org} node={id} zone={zone} schedules={schedules.data ?? null} canOperate={access.can('lanes.operate')} />}
          </TabPanel>
          <TabPanel value="logs">
            <LogsTab
              org={org}
              node={id}
              away={node.state === 'down'}
              allowed={access.can('nodes.manage')}
              source={search.source}
              level={search.level ?? 'info'}
              q={search.q ?? ''}
              onFilters={onFilters}
            />
          </TabPanel>
          <TabPanel value="installed">
            {programs.isPending || services.isPending || tools.isPending ? (
              <Skeleton className="h-96 rounded-xl" />
            ) : (
              <InstalledTab
                org={org}
                node={id}
                programs={programs.data ?? null}
                services={services.data ?? null}
                tools={tools.data ?? null}
                scope={search.scope ?? 'all'}
                onScope={(scope) => setSearch({ scope: scope === 'all' ? undefined : scope })}
              />
            )}
          </TabPanel>
          <TabPanel value="environments">
            <EnvironmentsTab org={org} node={id} owner={owner} />
          </TabPanel>
          <TabPanel value="resources">
            <ResourcesTab node={node} metrics={ranged.data} services={services.data ?? null} range={range} onRange={(next) => setSearch({ range: next === '24h' ? undefined : next })} bands={bands} />
          </TabPanel>
          <TabPanel value="connection">
            <ConnectionTab org={org} node={node} system={system.data} connections={connections.data} owner={owner} />
          </TabPanel>
          <TabPanel value="health">{health.isPending ? <Skeleton className="h-96 rounded-xl" /> : <HealthTab org={org} node={id} checks={health.data ?? null} owner={owner} />}</TabPanel>
          <TabPanel value="backups">
            <BackupsTab org={org} node={id} owner={owner} />
          </TabPanel>
          <TabPanel value="settings">
            <SettingsTab org={org} node={id} />
          </TabPanel>
        </div>
      </Tabs>
      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title={`Remove ${id}?`}
        description={`Accounts on this machine stop and show as read-only here. agentos forgets ${id}, so adding it again needs a new install command. Profiles and logins stay on the machine.`}
        confirmLabel="Remove machine"
        danger
        typeToConfirm={id}
        loading={remove.isPending}
        onConfirm={() =>
          remove.mutate(id, {
            onSuccess: () => {
              toast('Machine removed');
              void navigate({ to: '/$org/nodes', params: { org } });
            },
          })
        }
      />
    </>
  );
}

function HeaderSkeleton() {
  return (
    <div className="grid gap-6">
      <div>
        <Skeleton className="h-8 w-48" />
        <Skeleton className="mt-3 h-4 w-96 max-w-full" />
        <Skeleton className="mt-3 h-3 w-80 max-w-full" />
      </div>
      <Skeleton className="h-44 rounded-xl" />
      <Skeleton className="mt-4 h-10" />
      <Skeleton className="h-72 rounded-xl" />
    </div>
  );
}

function FullPage({ org, id }: { org: string; id: string }) {
  const node = useNode(org, id);
  return (
    <>
      <Link to="/$org/nodes/$id" params={{ org, id }} className="mb-4 inline-flex items-center gap-1 rounded-md text-secondary text-faint hover:text-fg">
        <ChevronLeft className="size-4" />
        Machines
      </Link>
      <QueryView card query={node} skeleton={<HeaderSkeleton />}>
        {(data) => <Body org={org} node={data} />}
      </QueryView>
    </>
  );
}

export function NodeDetailScreen() {
  const { org, id } = useParams({ from: '/$org/nodes/$id' });
  const search = useSearch({ from: '/$org/nodes/$id' });
  const navigate = useNavigate({ from: '/$org/nodes/$id' });
  const [adding, setAdding] = useState(false);
  if (search.tab) return <FullPage org={org} id={id} />;
  const table = { sort: search.sort, dir: search.dir };
  return (
    <Machines
      org={org}
      selected={id}
      table={table}
      addOpen={adding}
      onAdd={setAdding}
      onSelect={(next) =>
        void (next
          ? navigate({ to: '/$org/nodes/$id', params: { org, id: next }, search: table, resetScroll: false })
          : navigate({ to: '/$org/nodes', params: { org }, search: table, resetScroll: false }))
      }
      onSort={(next) => void navigate({ search: (previous) => ({ ...previous, ...next }), replace: true, resetScroll: false })}
    />
  );
}
