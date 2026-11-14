/**
 * One environment (/environments/:id): the browser's live view large, with take over as on an account; the
 * commands its terminal ran, each job linked to its run page; the ports with a link where the node forwards
 * one; git (branch, ahead, changed files, what was pushed); the agent's last calls; the machine and its cost;
 * and Extend and Close. A closed one is a read-only summary that says why it closed. Read every few seconds
 * while it has a machine, since no event carries it.
 */
import { Link, useParams } from '@tanstack/react-router';
import { ArrowUpRight, Clock, Globe, Lock, MonitorCloud, Plug, SearchX, SquareTerminal } from 'lucide-react';
import { useState } from 'react';
import type { ReactNode } from 'react';
import { toast } from 'sonner';
import { useAccess, useEnvironment, useExtendEnvironment, useLane, useLaneAction } from '@/api/hooks';
import { ApiError, isNotServed } from '@/api/http';
import type { EnvDetail } from '@/api/types';
import { usePageCrumb } from '@/components/shell/crumb';
import { BranchPill } from '@/components/ui/branch';
import { Button } from '@/components/ui/button';
import { Card, CardHeader } from '@/components/ui/card';
import { Menu, MenuContent, MenuItem, MenuTrigger } from '@/components/ui/menu';
import { CopyButton, Detail } from '@/components/ui/misc';
import { Spinner } from '@/components/ui/spinner';
import { StatStrip, StatTile } from '@/components/ui/stat-tile';
import { EmptyState, ErrorState, NotLive, Skeleton } from '@/components/ui/states';
import { Status, StatusDot } from '@/components/ui/status';
import { NoValue, Table, TableScroll, Td, Th, Tr } from '@/components/ui/table';
import { cn } from '@/lib/cn';
import { clock, dateTime, plural, safeUrl, timeAgo } from '@/lib/format';
import { useNow } from '@/lib/use-now';
import { ControlFrame } from '@/features/lane/control';
import { LiveView } from '@/features/lane/lane';
import { CloseEnvironment, UnsavedList, unsavedWords } from './close-flow';
import {
  ENV_STATE_SENTENCE,
  ENV_STATUS,
  SHORT_MS,
  ageOf,
  agentWords,
  closedReason,
  commandStatus,
  euro,
  hourly,
  isJob,
  isOpen,
  leftOf,
  machineWhere,
  providerName,
  repoName,
  shortCommit,
  span,
} from './words';

const EXTEND = [
  { minutes: 30, label: '30 minutes' },
  { minutes: 60, label: '1 hour' },
  { minutes: 120, label: '2 hours' },
];
const CALLS_SHOWN = 10;

export function EnvironmentPage() {
  const { org, id } = useParams({ from: '/$org/environments/$id' });
  const query = useEnvironment(org, id);
  usePageCrumb(query.data?.title, { mono: false });

  if (query.isPending) return <EnvSkeleton />;
  if (query.isError || !query.data) return <EnvMissing org={org} id={id} error={query.error} onRetry={() => void query.refetch()} />;
  return <EnvLoaded org={org} env={query.data} />;
}

function EnvSkeleton() {
  return (
    <div role="status" aria-label="Loading the environment" className="grid gap-6">
      <div className="grid gap-3">
        <Skeleton className="h-8 w-72 max-w-full" />
        <Skeleton className="h-5 w-96 max-w-full" />
      </div>
      <Skeleton className="h-24 w-full rounded-xl" />
      <div className="grid gap-6 lg:grid-cols-12">
        <Skeleton className="aspect-[16/10] h-auto w-full rounded-xl lg:col-span-8" />
        <Skeleton className="h-64 w-full rounded-xl lg:col-span-4" />
      </div>
    </div>
  );
}

function EnvMissing({ org, id, error, onRetry }: { org: string; id: string; error: unknown; onRetry: () => void }) {
  const back = (
    <Button asChild>
      <Link to="/$org/environments" params={{ org }}>
        All environments
      </Link>
    </Button>
  );
  if (isNotServed(error)) {
    return (
      <Card>
        <NotLive />
      </Card>
    );
  }
  if (error instanceof ApiError && error.status === 404) {
    return (
      <Card>
        <EmptyState
          icon={<SearchX />}
          title="This environment is not here"
          description={
            <>
              This computer has no environment called <span className="font-mono text-code">{id}</span>. The address may not be quite right.
            </>
          }
          action={back}
        />
      </Card>
    );
  }
  if (error instanceof ApiError && error.status === 403) {
    return (
      <Card>
        <EmptyState icon={<Lock />} title="This environment is not for you" description="Seeing it needs the env workspace in your role. An owner or admin can add it." action={back} />
      </Card>
    );
  }
  return (
    <Card>
      <ErrorState error={error} onRetry={onRetry} />
    </Card>
  );
}

function EnvLoaded({ org, env }: { org: string; env: EnvDetail }) {
  const open = isOpen(env);
  const now = useNow(open);
  return (
    <div className="grid gap-6">
      <EnvHeader org={org} env={env} now={now} />
      {open ? null : <ClosedNotice env={env} />}
      <Facts env={env} now={now} />
      <div className="grid items-start gap-6 lg:grid-cols-12">
        <div className="grid min-w-0 gap-6 lg:col-span-8">
          {open ? <BrowserCard org={org} env={env} /> : null}
          <CommandsCard org={org} env={env} now={now} />
          <CallsCard env={env} now={now} />
        </div>
        <div className="grid min-w-0 gap-6 lg:col-span-4">
          <GitCard env={env} now={now} />
          {open ? <PortsCard env={env} /> : null}
          <MachineCard env={env} />
        </div>
      </div>
    </div>
  );
}

function EnvHeader({ org, env, now }: { org: string; env: EnvDetail; now: number }) {
  const access = useAccess(org);
  const canOperate = access.can('lanes.operate');
  const actionable = isOpen(env) && env.state !== 'closing';
  // the branch it is on now (the agent may have made one), else the one it was opened on
  const branch = env.git?.branch ?? env.branch;
  return (
    <header className="grid gap-3">
      <div className="flex flex-wrap items-start gap-x-4 gap-y-3">
        <div className="grid w-full min-w-0 gap-2 md:w-auto md:flex-1">
          <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
            <h1 tabIndex={-1} className="min-w-0 text-page-title-compact [overflow-wrap:anywhere] md:text-page-title">
              {env.title}
            </h1>
            <span title={ENV_STATE_SENTENCE[env.state]}>
              <Status status={ENV_STATUS[env.state]} />
            </span>
          </div>
          <p className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-14 text-muted">
            <span>{agentWords(env.agent)}</span>
            <span aria-hidden="true" className="text-faint">
              ·
            </span>
            <span title={dateTime(env.created_at)}>opened {timeAgo(env.created_at, now).toLowerCase()}</span>
            {env.repo ? (
              <>
                <span aria-hidden="true" className="text-faint">
                  ·
                </span>
                <span className="min-w-0 truncate font-mono text-code" title={env.repo}>
                  {repoName(env.repo)}
                </span>
                {branch ? <BranchPill name={branch} main={branch === 'main' || branch === 'master'} /> : null}
              </>
            ) : null}
          </p>
        </div>
        {actionable && canOperate ? (
          <div className="flex flex-wrap items-center gap-2">
            <ExtendMenu org={org} env={env} />
            <CloseEnvironment org={org} env={env} />
          </div>
        ) : null}
      </div>
    </header>
  );
}

function ExtendMenu({ org, env }: { org: string; env: EnvDetail }) {
  const extend = useExtendEnvironment(org);
  return (
    <Menu>
      <MenuTrigger asChild>
        <Button icon={<Clock className="size-4" />} loading={extend.isPending}>
          Extend
        </Button>
      </MenuTrigger>
      <MenuContent align="end" aria-label="Extend by">
        {EXTEND.map((option) => (
          <MenuItem
            key={option.minutes}
            onSelect={() =>
              extend.mutate(
                { id: env.id, minutes: option.minutes },
                {
                  onSuccess: (next) => toast.success(`Extended by ${option.label}. The node now closes it at ${clock(next.expires_at)}.`),
                  onError: (error) => toast.error(error instanceof ApiError ? error.message : 'It could not be extended. Nothing was changed.'),
                },
              )
            }
          >
            Extend by {option.label}
          </MenuItem>
        ))}
      </MenuContent>
    </Menu>
  );
}

function ClosedNotice({ env }: { env: EnvDetail }) {
  const failed = env.state === 'failed';
  const lead = failed ? 'It failed to start.' : env.closed_at ? `Closed ${timeAgo(env.closed_at).toLowerCase()}.` : 'Closed.';
  return (
    <div role="status" className="flex items-start gap-3 rounded-xl border border-line bg-card px-5 py-4 shadow-lift">
      <StatusDot tone={failed ? 'bad' : 'neutral'} className="mt-2" />
      <p className="min-w-0 text-14">
        <span className="font-medium text-fg">{lead}</span> <span className="text-muted">{closedReason(env.closed_reason, env.state)}</span>
        <span className="mt-1 block text-13 text-faint">Its machine and browser are deleted. What is below is what the node kept.</span>
      </p>
    </div>
  );
}

/** A span as a big number and its small unit, so a tile at a tablet width keeps it on one line: "1 h" and "45 min". */
function spanTile(ms: number): { value: string; unit?: string } {
  const minutes = Math.floor(Math.max(0, ms) / 60_000);
  if (minutes < 1) return { value: 'Under 1', unit: 'min' };
  if (minutes < 60) return { value: String(minutes), unit: 'min' };
  const hours = Math.floor(minutes / 60);
  if (hours >= 48) return { value: String(Math.round(hours / 24)), unit: 'days' };
  return { value: `${hours} h`, unit: minutes % 60 ? `${minutes % 60} min` : undefined };
}

function Facts({ env, now }: { env: EnvDetail; now: number }) {
  const open = isOpen(env);
  const left = leftOf(env, now);
  const leftTile = left !== null && left > 0 ? spanTile(left) : null;
  const ranTile = spanTile(ageOf(env, now));
  return (
    <StatStrip>
      {open ? (
        <StatTile
          label="Time left"
          value={left === null ? <NoValue /> : leftTile ? leftTile.value : 'Ends now'}
          unit={leftTile?.unit}
          tone={left !== null && left < SHORT_MS ? 'warn' : undefined}
          hint={env.state === 'closing' ? 'Closing now' : `The node closes it at ${clock(env.expires_at)} at the latest`}
        />
      ) : (
        <StatTile label="Ran for" value={ranTile.value} unit={ranTile.unit} hint={env.closed_at ? `Closed ${dateTime(env.closed_at)}` : undefined} />
      )}
      <StatTile label={open ? 'Cost so far' : 'Cost'} value={euro(env.cost_eur)} hint={hourly(env.price_hourly_eur)} />
      <StatTile
        label="Machine"
        value={`${env.machine.cpu} CPU`}
        unit={`${env.machine.memory_gb} GB`}
        hint={machineWhere(env.machine)}
      />
      <StatTile
        label="Last call"
        value={env.last_call_at ? timeAgo(env.last_call_at, now) : <NoValue />}
        hint={open ? `Open for ${span(ageOf(env, now))}` : env.last_call_at ? dateTime(env.last_call_at) : 'The agent made no call'}
      />
    </StatStrip>
  );
}

/** The middle of the browser card while there is no live view to show. */
function Stage({ icon, title, children }: { icon: ReactNode; title: string; children: ReactNode }) {
  return (
    <div className="flex aspect-[16/10] flex-col items-center justify-center bg-subtle p-6 text-center">
      <div className="mb-4 text-faint [&_svg]:size-6">{icon}</div>
      <h3 className="text-14 font-medium">{title}</h3>
      <p className="mt-1 max-w-sm text-14 text-muted">{children}</p>
    </div>
  );
}

function BrowserCard({ org, env }: { org: string; env: EnvDetail }) {
  const [ws = 'env', name = ''] = env.lanes.browser.split('/');
  const starting = env.state === 'starting';
  return (
    <section aria-label="Browser" className="grid gap-3">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h2 className="flex items-center gap-2 text-card-title">
          <Globe aria-hidden="true" className="size-4 text-faint" />
          Browser
        </h2>
        <p className="min-w-0 text-secondary text-faint">
          Its traffic leaves from the machine, so <span className="font-mono text-code-caption text-muted">localhost</span> is the machine.
        </p>
      </div>
      {starting ? (
        <Card className="overflow-hidden">
          <Stage icon={<Spinner />} title="Starting the machine">
            The browser opens when the machine is ready, usually within two minutes.
          </Stage>
        </Card>
      ) : env.viewer ? (
        <BrowserLive org={org} ws={ws} name={name} />
      ) : (
        <Card className="overflow-hidden">
          <Stage icon={<Globe />} title="No live view">
            The node has no live view of this browser right now. It shows here once the agent opens a page.
          </Stage>
        </Card>
      )}
    </section>
  );
}

function BrowserLive({ org, ws, name }: { org: string; ws: string; name: string }) {
  const lane = useLane(org, ws, name);
  const action = useLaneAction(org, ws, name);
  const access = useAccess(org);
  const [expanded, setExpanded] = useState(false);
  if (lane.isPending) return <Skeleton className="aspect-[16/10] h-auto w-full rounded-xl" />;
  if (!lane.data) {
    return (
      <Card className="overflow-hidden">
        <Stage icon={<Globe />} title="The browser is not reachable">
          {lane.error instanceof ApiError && lane.error.status === 404
            ? 'The node does not list this browser now. It may be closing with the machine.'
            : 'The live view could not be read. It tries again on its own.'}
        </Stage>
      </Card>
    );
  }
  const canTake = access.can('lanes.control');
  const awake = lane.data.state === 'warm' || lane.data.state === 'active';
  return (
    <ControlFrame org={org} lane={lane.data} canControl={canTake} expanded={expanded && awake}>
      <LiveView
        org={org}
        lane={lane.data}
        opening={action.isPending}
        canOperate={access.can('lanes.operate')}
        canTake={canTake}
        onOpen={() => action.mutate('open')}
        expanded={expanded && awake}
        onExpandedChange={setExpanded}
      />
    </ControlFrame>
  );
}

function CommandsCard({ org, env, now }: { org: string; env: EnvDetail; now: number }) {
  const commands = env.commands;
  return (
    <Card className="overflow-hidden">
      <CardHeader
        title={
          <>
            <SquareTerminal aria-hidden="true" className="size-4 text-faint" />
            Terminal
          </>
        }
        meta={commands.length ? plural(commands.length, 'command') : undefined}
        action={<span className="hidden font-mono text-code-caption text-faint sm:inline">{env.lanes.terminal}</span>}
      />
      {commands.length ? (
        <TableScroll>
          <Table stack density="compact" aria-label="Commands">
            <thead>
              <tr>
                <Th>Command</Th>
                <Th>State</Th>
                <Th align="end">Started</Th>
              </tr>
            </thead>
            <tbody>
              {commands.map((command) => (
                <Tr key={command.id}>
                  <Td main>
                    <div className="flex min-w-0 items-center gap-2 font-mono text-code">
                      <span aria-hidden="true" className="text-faint">
                        $
                      </span>
                      {isJob(command) ? (
                        <Link to="/$org/runs/$id" params={{ org, id: command.id }} title={`${command.cmd} (its run page)`} className="min-w-0 truncate text-fg underline decoration-line-strong underline-offset-4 hover:decoration-fg">
                          {command.cmd}
                        </Link>
                      ) : (
                        <span title={command.cmd} className="min-w-0 truncate text-fg">
                          {command.cmd}
                        </span>
                      )}
                    </div>
                  </Td>
                  <Td>
                    <Status status={commandStatus(command)} />
                  </Td>
                  <Td numeric label="Started">
                    <time dateTime={command.started_at} title={dateTime(command.started_at)} className="text-muted">
                      {timeAgo(command.started_at, now)}
                    </time>
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </TableScroll>
      ) : (
        <p className="px-5 py-6 text-14 text-muted">
          {isOpen(env) ? 'Nothing has run yet. What the agent runs in the terminal shows here, and a job links to its output.' : 'Nothing ran in its terminal.'}
        </p>
      )}
    </Card>
  );
}

function GitCard({ env, now }: { env: EnvDetail; now: number }) {
  const git = env.git;
  const unsaved = env.unsaved && (env.unsaved.dirty_files.length || env.unsaved.unpushed.length) ? env.unsaved : null;
  return (
    <Card className="overflow-hidden">
      <CardHeader title="Git" meta={git?.pushed.length ? `${plural(git.pushed.length, 'push', 'pushes')}` : undefined} />
      {git ? (
        <div className="grid gap-4 px-5 py-4">
          {/* once closed the machine is gone, so there is no working copy left to count: only what was pushed */}
          {isOpen(env) ? (
            <dl className="grid divide-y divide-line">
              <Detail label="Branch">{git.branch ? <BranchPill name={git.branch} main={git.branch === 'main' || git.branch === 'master'} /> : 'No branch checked out'}</Detail>
              <Detail label="Not pushed">
                <span className={cn('font-mono tabular', git.ahead ? 'text-warn' : 'text-faint')}>{plural(git.ahead, 'commit')}</span>
              </Detail>
              <Detail label="Changed files">
                <span className={cn('font-mono tabular', git.dirty ? 'text-warn' : 'text-faint')}>{git.dirty}</span>
              </Detail>
            </dl>
          ) : null}
          {unsaved ? (
            <div className="grid gap-3">
              <p className="flex items-start gap-2 text-13 text-fg">
                <StatusDot tone="warn" className="mt-1" />
                <span>
                  <span className="font-medium text-warn">Unsaved work.</span> It has {unsavedWords(unsaved)}. Closing loses it unless the agent pushes first.
                </span>
              </p>
              <UnsavedList unsaved={unsaved} />
            </div>
          ) : null}
          <div className="grid gap-2">
            <h3 className="text-13 font-medium text-fg">Pushed{git.branch && git.pushed.length && git.pushed.every((push) => push.branch === git.branch) ? <span className="font-normal text-faint"> to {git.branch}</span> : null}</h3>
            {git.pushed.length ? (
              <ul className="grid gap-2">
                {git.pushed.map((push) => (
                  <li key={`${push.commit}:${push.at}`} className="flex min-w-0 items-center gap-2">
                    <span className="shrink-0 font-mono text-code-caption text-fg" title={push.commit}>
                      {shortCommit(push.commit)}
                    </span>
                    {push.branch !== git.branch ? <BranchPill name={push.branch} main={push.branch === 'main' || push.branch === 'master'} className="min-w-0" /> : null}
                    <time dateTime={push.at} title={dateTime(push.at)} className="ml-auto shrink-0 text-caption text-faint">
                      {timeAgo(push.at, now)}
                    </time>
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-13 text-faint">Nothing pushed yet.</p>
            )}
          </div>
        </div>
      ) : (
        <p className="px-5 py-4 text-14 text-muted">{env.state === 'failed' ? 'It failed before the repository was cloned.' : env.repo ? 'The node did not report git for it.' : 'It was opened without a repository.'}</p>
      )}
    </Card>
  );
}

function PortsCard({ env }: { env: EnvDetail }) {
  return (
    <Card className="overflow-hidden">
      <CardHeader
        title={
          <>
            <Plug aria-hidden="true" className="size-4 text-faint" />
            Ports
          </>
        }
        meta={env.ports.length ? plural(env.ports.length, 'listening', 'listening') : undefined}
      />
      {env.ports.length ? (
        <ul className="divide-y divide-line">
          {env.ports.map((port) => {
            const link = safeUrl(port.link ?? null) ?? (port.link?.startsWith('/') ? port.link : null);
            return (
              <li key={port.port} className="flex min-h-12 items-center gap-3 px-5 py-2">
                <span className="font-mono text-code text-fg tabular">{port.port}</span>
                <span className="min-w-0 flex-1 truncate text-13 text-muted" title={port.process ?? undefined}>
                  {port.process ?? 'Process not known'}
                </span>
                {link ? (
                  <Button asChild size="sm" icon={<ArrowUpRight className="size-4" />}>
                    <a href={link} target="_blank" rel="noreferrer" aria-label={`Open port ${port.port}${port.process ? `, ${port.process}` : ''}`}>
                      Open
                    </a>
                  </Button>
                ) : (
                  <span className="text-caption text-faint" title="The node does not forward this port">
                    On the machine only
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="px-5 py-4 text-14 text-muted">{env.state === 'starting' ? 'Nothing listens yet.' : 'Nothing listens on the machine. A dev server the agent starts shows here with a link.'}</p>
      )}
    </Card>
  );
}

function CallsCard({ env, now }: { env: EnvDetail; now: number }) {
  const [all, setAll] = useState(false);
  const shown = all ? env.calls : env.calls.slice(0, CALLS_SHOWN);
  return (
    <Card className="overflow-hidden">
      <CardHeader title="Last calls" meta={env.calls.length >= 50 ? 'the newest 50' : env.calls.length ? plural(env.calls.length, 'call') : undefined} />
      {env.calls.length ? (
        <>
          <ol aria-label="Calls, newest first" className="divide-y divide-line">
            {shown.map((call, index) => (
              <li key={`${call.at}:${index}`} className="grid grid-cols-[auto_minmax(0,1fr)_auto] items-start gap-x-3 px-5 py-2">
                <StatusDot tone={call.ok ? 'ok' : 'bad'} className="mt-2" />
                <span className="grid min-w-0">
                  <span className="truncate font-mono text-code text-fg">{call.tool}</span>
                  <span className="truncate text-secondary text-muted" title={call.summary}>
                    {call.ok ? '' : <span className="sr-only">Failed: </span>}
                    {call.summary}
                  </span>
                </span>
                <time dateTime={call.at} title={dateTime(call.at)} className="pt-px text-caption text-faint tabular">
                  {timeAgo(call.at, now)}
                </time>
              </li>
            ))}
          </ol>
          {env.calls.length > CALLS_SHOWN ? (
            <div className="border-t border-line px-3 py-2">
              <Button variant="ghost" size="sm" aria-expanded={all} onClick={() => setAll((value) => !value)}>
                {all ? `Show the newest ${CALLS_SHOWN}` : `Show all ${env.calls.length}`}
              </Button>
            </div>
          ) : null}
        </>
      ) : (
        <p className="px-5 py-4 text-14 text-muted">No calls yet.</p>
      )}
    </Card>
  );
}

function MachineCard({ env }: { env: EnvDetail }) {
  return (
    <Card className="overflow-hidden">
      <CardHeader
        title={
          <>
            <MonitorCloud aria-hidden="true" className="size-4 text-faint" />
            Machine
          </>
        }
      />
      <dl className="grid divide-y divide-line px-5 py-2">
        <Detail label="Provider">{providerName(env.machine.provider)}</Detail>
        <Detail label="Type">
          <span className="font-mono text-code-caption">{env.machine.server_type}</span>
        </Detail>
        <Detail label="Location">
          <span className="font-mono text-code-caption">{env.machine.location}</span>
        </Detail>
        <Detail label="Price">{hourly(env.price_hourly_eur)}</Detail>
        <Detail label="Terminal">
          <span className="inline-flex items-center gap-1">
            <span className="font-mono text-code-caption">{env.lanes.terminal}</span>
            <CopyButton text={env.lanes.terminal} label="Copy the terminal lane" />
          </span>
        </Detail>
        <Detail label="Browser">
          <span className="inline-flex items-center gap-1">
            <span className="font-mono text-code-caption">{env.lanes.browser}</span>
            <CopyButton text={env.lanes.browser} label="Copy the browser lane" />
          </span>
        </Detail>
      </dl>
    </Card>
  );
}
