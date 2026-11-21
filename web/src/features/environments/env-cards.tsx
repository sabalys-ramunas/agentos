/**
 * What sits under an environment's screen: the commands its terminal ran, what it did (each call in plain
 * words, failures in red), its code, the ports that listen, and one quiet line for the machine and its price.
 */
import { Link } from '@tanstack/react-router';
import { ArrowUpRight, ChevronDown, Cpu, FileText, Globe, Monitor, MonitorCloud, ScanEye, SquareTerminal } from 'lucide-react';
import { useState } from 'react';
import type { EnvDetail } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Card, CardHeader } from '@/components/ui/card';
import { CopyButton, Detail } from '@/components/ui/misc';
import { Table, TableScroll, Td, Th, Tr } from '@/components/ui/table';
import { cn } from '@/lib/cn';
import { clock, dateTime, plural, safeUrl, timeAgo } from '@/lib/format';
import { toolPlainTitle } from '@/lib/plain-node';
import { useTechnical } from '@/lib/technical';
import { UnsavedList, unsavedWords } from './close-flow';
import { StatusPill } from './status-pill';
import type { CallWords } from './words';
import { KEEP, callWords, commandStatus, hourly, idleCost, isJob, isOpen, keepOf, machineWords, pullRequestLink } from './words';

/** Steps shown before "All". */
export const CALLS_SHOWN = 6;

export function CommandsCard({ org, env, now }: { org: string; env: EnvDetail; now: number }) {
  const commands = env.commands;
  return (
    <Card className="overflow-hidden">
      <CardHeader title="Terminal" meta={commands.length ? plural(commands.length, 'command') : undefined} />
      {commands.length ? (
        <TableScroll>
          <Table stack aria-label="Commands">
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
                    {/* Long commands wrap; the time beside them stays on one line. */}
                    <div className="flex min-w-0 gap-2 font-mono text-code whitespace-normal break-words">
                      <span aria-hidden="true" className="text-faint">
                        $
                      </span>
                      {isJob(command) ? (
                        <Link to="/$org/runs/$id" params={{ org, id: command.id }} title={`${command.cmd} (its run page)`} className="min-w-0 text-fg underline decoration-line-strong underline-offset-4 hover:decoration-fg">
                          {command.cmd}
                        </Link>
                      ) : (
                        <span className="min-w-0 text-fg">{command.cmd}</span>
                      )}
                    </div>
                  </Td>
                  <Td>
                    <StatusPill status={commandStatus(command)} />
                  </Td>
                  <Td numeric label="Started">
                    <time dateTime={command.started_at} title={dateTime(command.started_at)} className="whitespace-nowrap text-muted">
                      {timeAgo(command.started_at, now)}
                    </time>
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        </TableScroll>
      ) : (
        <p className="px-5 py-6 text-body text-muted">
          {isOpen(env) ? 'Nothing has run yet. What the agent runs in its terminal shows here, and a long one links to its output.' : 'Nothing ran in its terminal.'}
        </p>
      )}
    </Card>
  );
}

const STEP_ICON: Record<CallWords['kind'], typeof Globe> = {
  page: Globe,
  command: SquareTerminal,
  check: ScanEye,
  file: FileText,
  screen: Monitor,
  machine: MonitorCloud,
};

export function StepsCard({ env, now }: { env: EnvDetail; now: number }) {
  const [all, setAll] = useState(false);
  const [technical] = useTechnical();
  const calls = env.calls;
  const shown = all ? calls : calls.slice(0, CALLS_SHOWN);
  const count = calls.length >= 50 ? 'the newest 50 steps' : plural(calls.length, 'step');
  return (
    <Card className="overflow-hidden">
      <CardHeader
        title="What it did"
        meta={calls.length ? count : undefined}
        action={
          calls.length > CALLS_SHOWN ? (
            <Button
              variant="ghost"
              size="sm"
              aria-expanded={all}
              onClick={() => setAll((value) => !value)}
              icon={<ChevronDown aria-hidden="true" className={cn('size-4 transition-transform duration-(--duration-fast)', all && 'rotate-180')} />}
            >
              {all ? `The newest ${CALLS_SHOWN}` : `All ${calls.length}`}
            </Button>
          ) : undefined
        }
      />
      {calls.length ? (
        <ol aria-label="Steps, newest first" className="divide-y divide-line">
          {shown.map((call, index) => {
            const words = callWords(call.tool, call.summary, toolPlainTitle);
            const Icon = STEP_ICON[words.kind];
            return (
              <li key={`${call.at}:${index}`} className="grid min-h-14 grid-cols-[auto_minmax(0,1fr)_auto] items-center gap-x-4 px-5 py-2">
                <Icon aria-hidden="true" className="size-4 text-faint" />
                <span className="grid min-w-0">
                  <span className="truncate text-body font-medium text-fg" title={call.tool}>
                    {technical ? <span className="font-mono text-code">{call.tool}</span> : words.title}
                    {call.ok ? null : <span className="sr-only">. It failed</span>}
                  </span>
                  <span className={cn('truncate text-secondary', call.ok ? 'text-muted' : 'text-bad')} title={words.detail}>
                    {words.detail}
                  </span>
                </span>
                <time dateTime={call.at} title={dateTime(call.at)} className="text-secondary whitespace-nowrap text-faint">
                  {timeAgo(call.at, now)}
                </time>
              </li>
            );
          })}
        </ol>
      ) : (
        <p className="px-5 py-6 text-body text-muted">{isOpen(env) ? 'Nothing yet. Each step the agent takes shows here in plain words.' : 'It made no call.'}</p>
      )}
    </Card>
  );
}

export function GitCard({ env, now }: { env: EnvDetail; now: number }) {
  const git = env.git;
  const open = isOpen(env);
  const unsaved = env.unsaved && (env.unsaved.dirty_files.length || env.unsaved.unpushed.length) ? env.unsaved : null;
  const branch = git?.branch ?? env.branch ?? null;
  const last = git?.pushed[0];
  const link = branch && git?.pushed.length ? pullRequestLink(env.repo, branch) : null;
  const notPushed = git ? [git.dirty ? plural(git.dirty, 'changed file') : null, git.ahead ? plural(git.ahead, 'commit') : null].filter(Boolean).join(' and ') : '';
  return (
    <Card className="overflow-hidden">
      <CardHeader title="Code" />
      {git ? (
        <div className="grid gap-4 px-5 pt-2 pb-5">
          <dl className="grid divide-y divide-line">
            <Detail label="Branch">{branch ? <span className="font-mono text-code [overflow-wrap:anywhere]">{branch}</span> : 'No branch'}</Detail>
            {/* once closed the machine is gone, so there is no working copy left to count: only what was pushed */}
            {open ? <Detail label="Not pushed">{notPushed ? <span className="font-medium text-warn">{notPushed}</span> : <span className="font-medium">Nothing</span>}</Detail> : null}
            <Detail label="Pushed">
              {last ? (
                <span className="font-medium" title={dateTime(last.at)}>
                  {git.pushed.length === 1 ? 'Once' : `${git.pushed.length} times`}, {timeAgo(last.at, now).toLowerCase()}
                </span>
              ) : (
                <span className="text-faint">Nothing yet</span>
              )}
            </Detail>
          </dl>
          {unsaved && open ? (
            <div className="grid gap-3">
              <p className="text-secondary text-fg">
                <span className="font-medium text-warn">Unsaved work.</span> It has {unsavedWords(unsaved)}. Closing loses it unless the agent pushes first.
              </p>
              <UnsavedList unsaved={unsaved} />
            </div>
          ) : null}
          {link ? (
            <div>
              <Button asChild size="sm" icon={<ArrowUpRight className="size-4" />}>
                <a href={link} target="_blank" rel="noreferrer">
                  Open a pull request
                </a>
              </Button>
            </div>
          ) : null}
        </div>
      ) : (
        <p className="px-5 py-5 text-body text-muted">{env.state === 'failed' ? 'It did not start, so nothing was cloned.' : env.repo ? 'The node did not say anything about its code.' : 'It was opened without a repository.'}</p>
      )}
    </Card>
  );
}

export function PortsCard({ env }: { env: EnvDetail }) {
  return (
    <Card className="overflow-hidden">
      <CardHeader title="Ports" meta={env.ports.length ? plural(env.ports.length, 'listening', 'listening') : undefined} />
      {env.ports.length ? (
        <ul className="divide-y divide-line">
          {env.ports.map((port) => {
            const link = safeUrl(port.link ?? null) ?? (port.link?.startsWith('/') ? port.link : null);
            return (
              <li key={port.port} className="flex min-h-14 flex-wrap items-center gap-x-4 gap-y-1 px-5 py-2">
                <span className="w-12 font-mono text-code text-fg tabular">{port.port}</span>
                <span className="min-w-0 flex-1 truncate text-body text-fg" title={port.process ?? undefined}>
                  {port.process ?? <span className="text-faint">Not known</span>}
                </span>
                {link ? (
                  <Button asChild size="sm" icon={<ArrowUpRight className="size-4" />}>
                    <a href={link} target="_blank" rel="noreferrer" aria-label={`Open port ${port.port}${port.process ? `, ${port.process}` : ''}`}>
                      Open
                    </a>
                  </Button>
                ) : (
                  <span className="text-secondary text-faint" title="The agent can share it with environment_port">
                    On the machine only
                  </span>
                )}
                {link && port.tunnel ? (
                  <p className="w-full pl-16 text-caption text-faint">
                    First, on your computer: <code className="font-mono text-code-caption text-muted select-all [overflow-wrap:anywhere]">{port.tunnel}</code>
                  </p>
                ) : null}
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="px-5 py-5 text-body text-muted">{env.state === 'starting' ? 'Nothing listens yet.' : 'Nothing listens on the machine. A site the agent starts shows here with a link.'}</p>
      )}
    </Card>
  );
}

/** One quiet line for the machine: its size and place, its price, and when the node closes it. */
export function MachineLine({ env }: { env: EnvDetail }) {
  const [technical] = useTechnical();
  const open = isOpen(env);
  const keep = KEEP[keepOf(env)];
  return (
    <div className="grid gap-2 text-secondary text-muted">
      <p className="flex flex-wrap items-center gap-x-5 gap-y-1">
        <Cpu aria-hidden="true" className="size-4 text-faint" />
        <span>{machineWords(env.machine)}</span>
        <span>{hourly(env.price_hourly_eur)}</span>
        {open && env.state !== 'closing' ? <span>The node closes it at {clock(env.expires_at)} at the latest</span> : null}
        {env.keep ? <span title={keep.sentence}>{idleCost(env)}</span> : null}
      </p>
      {technical ? (
        <p className="flex flex-wrap items-center gap-x-5 gap-y-1 pl-9 font-mono text-code-caption text-faint">
          <span>
            {env.machine.server_type} in {env.machine.location}
          </span>
          <span className="inline-flex items-center gap-1">
            {env.lanes.terminal}
            <CopyButton text={env.lanes.terminal} label="Copy the terminal lane" />
          </span>
          <span className="inline-flex items-center gap-1">
            {env.lanes.browser}
            <CopyButton text={env.lanes.browser} label="Copy the browser lane" />
          </span>
          {/* The agent as the node knows it: the grant's subject and the MCP client with its version. */}
          <span title="The agent's grant">{env.agent.name}</span>
          {env.agent.client ? <span title="The MCP client it connected with">{env.agent.client}</span> : null}
        </p>
      ) : null}
    </div>
  );
}
