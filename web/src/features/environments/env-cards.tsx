/** An environment's cards: its browser, the commands its terminal ran, git, ports, calls and the machine. */
import { Link } from '@tanstack/react-router';
import { ArrowUpRight, Globe, MonitorCloud, Plug, SquareTerminal } from 'lucide-react';
import { useState } from 'react';
import type { ReactNode } from 'react';
import { useAccess, useLane, useLaneAction } from '@/api/hooks';
import { ApiError } from '@/api/http';
import type { EnvDetail } from '@/api/types';
import { BranchPill } from '@/components/ui/branch';
import { Button } from '@/components/ui/button';
import { Card, CardHeader } from '@/components/ui/card';
import { CopyButton, Detail } from '@/components/ui/misc';
import { Spinner } from '@/components/ui/spinner';
import { Skeleton } from '@/components/ui/states';
import { Status, StatusDot } from '@/components/ui/status';
import { Table, TableScroll, Td, Th, Tr } from '@/components/ui/table';
import { cn } from '@/lib/cn';
import { dateTime, plural, safeUrl, timeAgo } from '@/lib/format';
import { toolPlainTitle } from '@/lib/plain-node';
import { useTechnical } from '@/lib/technical';
import { ControlFrame } from '@/features/lane/control';
import { LiveView } from '@/features/lane/live-view';
import { SegmentedControl } from '@/components/ui/tabs';
import { UnsavedList, unsavedWords } from './close-flow';
import { ReplayCard } from './replay';
import {
  KEEP,
  commandStatus,
  hourly,
  idleCost,
  isJob,
  isOpen,
  keepOf,
  providerName,
  pullRequestLink,
  shortCommit,
} from './words';
import { CALLS_SHOWN } from './environment-page';

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

export function BrowserCard({ org, env }: { org: string; env: EnvDetail }) {
  const [ws = 'env', name = ''] = env.lanes.browser.split('/');
  const starting = env.state === 'starting';
  // Concept: what its browser did so far, beside the live view, as a view of the same thing.
  const [view, setView] = useState<'live' | 'replay'>('live');
  const replayable = !starting && Boolean(env.replay?.frames);
  return (
    <section aria-label="Browser" className="grid gap-3">
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <h2 className="flex items-center gap-2 text-card-title">
          <Globe aria-hidden="true" className="size-4 text-faint" />
          Browser
        </h2>
        {view === 'live' ? (
          <p className="min-w-0 text-secondary text-faint max-md:order-last max-md:w-full">
            Its traffic leaves from the machine, so <span className="font-mono text-code-caption text-muted">localhost</span> is the machine.
          </p>
        ) : null}
        {replayable ? (
          <SegmentedControl<'live' | 'replay'>
            label="Browser view"
            value={view}
            onValueChange={setView}
            options={[
              { value: 'live', label: 'Live' },
              { value: 'replay', label: 'Replay so far' },
            ]}
            className="ml-auto"
          />
        ) : null}
      </div>
      {view === 'replay' && replayable ? (
        <Card className="overflow-hidden">
          <ReplayCard org={org} env={env} embedded />
        </Card>
      ) : starting ? (
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

export function CommandsCard({ org, env, now }: { org: string; env: EnvDetail; now: number }) {
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

/** A pushed branch's way to review: GitHub's page that opens a pull request for it. */
function PullRequestLink({ repo, branch }: { repo: string | null | undefined; branch: string }) {
  const link = pullRequestLink(repo, branch);
  if (!link) return null;
  return (
    <a
      href={link}
      target="_blank"
      rel="noreferrer"
      aria-label={`Open a pull request for ${branch}`}
      className="inline-flex shrink-0 items-center gap-1 text-caption font-medium text-blue hover:underline"
    >
      Open a pull request
      <ArrowUpRight className="size-3.5" aria-hidden />
    </a>
  );
}

export function GitCard({ env, now }: { env: EnvDetail; now: number }) {
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
                    <PullRequestLink repo={env.repo} branch={push.branch} />
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

export function PortsCard({ env }: { env: EnvDetail }) {
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
              <li key={port.port} className="flex min-h-12 flex-wrap items-center gap-x-3 gap-y-1 px-5 py-2">
                <span className="font-mono text-code text-fg tabular">{port.port}</span>
                <span className="min-w-0 flex-1 truncate text-13 text-muted" title={port.process ?? undefined}>
                  {port.process ?? 'Process not known'}
                </span>
                {link && port.tunnel ? (
                  <p className="order-last w-full text-caption text-faint">
                    First, on your computer: <code className="select-all font-mono text-code text-muted">{port.tunnel}</code>
                  </p>
                ) : null}
                {link ? (
                  <Button asChild size="sm" icon={<ArrowUpRight className="size-4" />}>
                    <a href={link} target="_blank" rel="noreferrer" aria-label={`Open port ${port.port}${port.process ? `, ${port.process}` : ''}`}>
                      Open
                    </a>
                  </Button>
                ) : (
                  <span className="text-caption text-faint" title="The agent can share it with environment_port">
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

export function CallsCard({ env, now }: { env: EnvDetail; now: number }) {
  const [all, setAll] = useState(false);
  // What each call did, in words; the tool's own name under Technical details.
  const [technical] = useTechnical();
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
                  {technical ? (
                    <span className="truncate font-mono text-code text-fg">{call.tool}</span>
                  ) : (
                    <span className="truncate text-14 text-fg" title={call.tool}>
                      {toolPlainTitle(call.tool)}
                    </span>
                  )}
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

export function MachineCard({ env }: { env: EnvDetail }) {
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
        {env.keep ? (
          <>
            <Detail label="Stays">
              <span title={KEEP[keepOf(env)].sentence}>{KEEP[keepOf(env)].label}</span>
            </Detail>
            <Detail label="While unused">{idleCost(env)}</Detail>
          </>
        ) : null}
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
