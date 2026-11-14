import { Link } from '@tanstack/react-router';
import type { NodeProgram, NodeService, ProgramScope, ToolDef } from '@/api/types';
import { Segmented } from '@/components/charts/small';
import { Badge } from '@/components/ui/badge';
import { Card } from '@/components/ui/card';
import { CopyButton } from '@/components/ui/misc';
import { Status } from '@/components/ui/status';
import { Table, Td, Th, Tr, mainCell } from '@/components/ui/table';
import { date, plural, shortHash, timeAgo } from '@/lib/format';
import { toolPlainTitle } from '@/lib/plain-node';
import { effectLabel, effectOrder } from '@/lib/labels';
import type { StatusLabel } from '@/lib/labels';
import { useTechnical } from '@/lib/technical';
import { NONE, NotReported, Panel } from './parts';

export type ScopeFilter = ProgramScope | 'all';

const SCOPES: { value: ScopeFilter; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'node', label: 'Machine' },
  { value: 'lane', label: 'Accounts' },
  { value: 'base_image', label: 'Base image' },
];

const SCOPE_WORDS: Record<ProgramScope, string> = { node: 'Whole machine', lane: 'Only one account', base_image: 'Base image' };

function Programs({ node, programs, scope, onScope }: { node: string; programs: NodeProgram[] | null; scope: ScopeFilter; onScope: (scope: ScopeFilter) => void }) {
  if (programs === null) {
    return (
      <Panel title="Programs">
        <NotReported node={node} what="its programs" />
      </Panel>
    );
  }
  const shown = scope === 'all' ? programs : programs.filter((item) => item.scope === scope);
  // A node that cannot say whether a program is signed leaves it out: that is not the same as unsigned.
  const signing = programs.some((item) => item.signed !== undefined);
  const unsigned = programs.filter((item) => item.signed === false).length;
  return (
    <Panel
      title="Programs"
      description={`${plural(programs.length, 'program')}${!signing ? '' : unsigned ? `, ${unsigned} unsigned` : ', all signed'}. A pinned program only runs if its hash still matches.`}
      action={<Segmented label="Where it is installed" value={scope} onChange={onScope} options={SCOPES} />}
    >
      <Card className="overflow-hidden">
        <Table>
          <thead className="hidden md:table-header-group">
            <tr>
              <Th>Program</Th>
              <Th>Version</Th>
              <Th className="hidden lg:table-cell">Where</Th>
              <Th>Signed</Th>
              <Th className="hidden xl:table-cell">Pinned to</Th>
              <Th className="hidden lg:table-cell">Installed</Th>
            </tr>
          </thead>
          <tbody>
            {shown.map((program) => (
              <Tr key={`${program.name}:${program.scope}`} className="hover:bg-fill-1">
                <Td className={mainCell}>
                  <p className="mono truncate text-13">{program.name}</p>
                  <p className="truncate text-12 text-faint">
                    from {program.source}
                    <span className="md:hidden">
                      {' · '}
                      <span className="mono">{program.version}</span>
                    </span>
                    <span className="lg:hidden"> · {SCOPE_WORDS[program.scope]}</span>
                  </p>
                </Td>
                <Td className="mono hidden text-13 text-muted md:table-cell">{program.version}</Td>
                <Td className="hidden text-13 text-muted lg:table-cell">
                  {SCOPE_WORDS[program.scope]}
                  {program.lanes.length ? <span className="text-faint"> · {program.lanes.length === 1 ? program.lanes[0] : plural(program.lanes.length, 'lane')}</span> : null}
                </Td>
                <Td>
                  {program.signed === undefined ? NONE : <Status status={program.signed ? { label: 'Signed', tone: 'ok' } : { label: 'Unsigned', tone: 'warn' }} />}
                </Td>
                <Td className="hidden xl:table-cell">
                  {program.pinned_hash ? (
                    <span className="inline-flex items-center gap-1">
                      <span className="mono text-12 text-muted">{shortHash(program.pinned_hash)}</span>
                      <CopyButton text={program.pinned_hash} label={`Copy the hash for ${program.name}`} />
                    </span>
                  ) : (
                    <span className="text-13 text-faint">Not pinned</span>
                  )}
                </Td>
                <Td className="hidden text-13 text-muted lg:table-cell">{program.installed_at ? <span title={date(program.installed_at)}>{timeAgo(program.installed_at)}</span> : NONE}</Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      </Card>
    </Panel>
  );
}

const serviceStatus = (state: string): StatusLabel =>
  state === 'running' ? { label: 'Running', tone: 'ok' } : state === 'stopped' ? { label: 'Stopped', tone: 'idle' } : { label: state.slice(0, 24), tone: 'warn' };

function Services({ org, node, services }: { org: string; node: string; services: NodeService[] | null }) {
  if (services === null) {
    return (
      <Panel title="Services">
        <NotReported node={node} what="its services" />
      </Panel>
    );
  }
  const restarting = services.filter((item) => item.restarts > 2);
  return (
    <Panel
      title="Services"
      description={restarting.length ? `${restarting.map((item) => item.name).join(', ')} restarted more than twice this week.` : 'Programs that keep running on this machine and in its accounts.'}
    >
      <Card className="overflow-hidden">
        <Table>
          <thead className="hidden md:table-header-group">
            <tr>
              <Th>Service</Th>
              <Th>State</Th>
              <Th className="hidden lg:table-cell">Ports</Th>
              <Th className="text-right">Restarts</Th>
              <Th className="hidden text-right md:table-cell">CPU</Th>
              <Th className="hidden text-right md:table-cell">Memory</Th>
            </tr>
          </thead>
          <tbody>
            {services.map((service) => {
              const [ws, name] = service.lane?.split('/') ?? [];
              return (
                <Tr key={service.name} className="hover:bg-fill-1">
                  <Td className={mainCell}>
                    <p className="mono truncate text-13">{service.name}</p>
                    <p className="truncate text-12 text-faint">
                      {ws && name ? (
                        <Link to="/$org/lanes/$ws/$name" params={{ org, ws, name }} className="hover:text-fg">
                          in {service.lane}
                        </Link>
                      ) : (
                        'On this machine'
                      )}
                      {service.started_at ? ` · up ${timeAgo(service.started_at).replace(' ago', '')}` : null}
                    </p>
                  </Td>
                  <Td>
                    <Status status={serviceStatus(service.state)} />
                  </Td>
                  <Td className="mono hidden text-13 text-muted lg:table-cell">{service.ports?.length ? service.ports.join(', ') : NONE}</Td>
                  <Td className="tabular text-right text-13">
                    <span className={service.restarts > 2 ? 'text-amber' : 'text-muted'}>{service.restarts}</span>
                  </Td>
                  <Td className="mono tabular hidden text-right text-13 text-muted md:table-cell">{service.cpu_pct === null ? NONE : `${service.cpu_pct}%`}</Td>
                  <Td className="mono tabular hidden text-right text-13 text-muted md:table-cell">{service.mem_mb === null ? NONE : `${service.mem_mb} MB`}</Td>
                </Tr>
              );
            })}
          </tbody>
        </Table>
      </Card>
    </Panel>
  );
}

function Tools({ node, tools }: { node: string; tools: ToolDef[] | null }) {
  const [technical] = useTechnical();
  if (tools === null) {
    return (
      <Panel title="Tools for AI apps">
        <NotReported node={node} what="its tools for AI apps" />
      </Panel>
    );
  }
  // Heaviest effect first, the same order Needs you groups by.
  const rank = (tool: ToolDef) => (tool.effect ? effectOrder.indexOf(tool.effect) : effectOrder.length);
  const sorted = [...tools].sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name));
  return (
    <Panel title="Tools for AI apps" description={`What an AI app attached to this machine can call here. ${plural(tools.length, 'tool')}, biggest effect first.`}>
      <Card className="grid gap-px overflow-hidden bg-line sm:grid-cols-2 xl:grid-cols-3">
        {sorted.map((tool) => (
          <div key={tool.name} className="flex min-w-0 items-start justify-between gap-3 bg-bg px-4 py-3">
            <div className="min-w-0">
              <p className="truncate text-13">{toolPlainTitle(tool.name)}</p>
              {technical ? <p className="mono truncate text-12 text-faint">{tool.name}</p> : null}
              {technical && tool.description ? <p className="truncate text-12 text-faint">{tool.description}</p> : null}
            </div>
            {tool.effect ? <Badge className="shrink-0">{effectLabel[tool.effect]}</Badge> : null}
          </div>
        ))}
      </Card>
    </Panel>
  );
}

interface InstalledProps {
  org: string;
  node: string;
  programs: NodeProgram[] | null;
  services: NodeService[] | null;
  tools: ToolDef[] | null;
  scope: ScopeFilter;
  onScope: (scope: ScopeFilter) => void;
}

export function InstalledTab({ org, node, programs, services, tools, scope, onScope }: InstalledProps) {
  // One notice, not three of the same: a build that reports none of these says so once.
  if (programs === null && services === null && tools === null) return <NotReported node={node} what="what is installed on it" />;
  return (
    <div className="grid gap-10">
      <Programs node={node} programs={programs} scope={scope} onScope={onScope} />
      <Services org={org} node={node} services={services} />
      <Tools node={node} tools={tools} />
    </div>
  );
}
