/**
 * Environments: the cloud computers agents rent (docs/design/environments.md). Open ones first, newest
 * first, with what each is doing and how it stays, its repository and branch, the agent, its age, what it
 * cost so far and the time left before the node closes it. Then the saves of kept ones, which an agent brings
 * back by opening them again. Closed ones below, the newest five, the rest a click away.
 * Everything is what the node serves; the page reads it again on its own (no events carry environments).
 */
import { Link, useNavigate, useParams } from '@tanstack/react-router';
import { ChevronDown, Lock, MonitorCloud } from 'lucide-react';
import { useState } from 'react';
import { useEnvironments } from '@/api/hooks';
import { ApiError, isNotServed } from '@/api/http';
import type { EnvSummary } from '@/api/types';
import { BranchPill } from '@/components/ui/branch';
import { Button } from '@/components/ui/button';
import { Card, CardHeader } from '@/components/ui/card';
import { CodeBlock, CopyButton, PageHeader } from '@/components/ui/misc';
import { EmptyState, ErrorState, NotLive } from '@/components/ui/states';
import { Status } from '@/components/ui/status';
import { CellTitle, NoValue, RowActions, Table, TableScroll, TableSkeleton, Td, Th, Tr } from '@/components/ui/table';
import { cn } from '@/lib/cn';
import { dateTime, plural, timeAgo } from '@/lib/format';
import { PLACES } from '@/lib/vocabulary';
import { useNow } from '@/lib/use-now';
import { SavedCard } from './saved';
import { ENV_STATE_SENTENCE, ENV_STATUS, KEEP, SHORT_MS, ageOf, agentWords, euro, hourly, idleCost, idleShort, isOpen, keepOf, leftOf, machineSize, openSummary, repoName, span, splitEnvironments } from './words';

/** Closed ones shown before "Show all". */
export const CLOSED_SHOWN = 5;
const OPEN_COLUMNS = ['Environment', 'State', 'Repository', 'Agent', 'Age', 'Cost', 'Time left'];

/** How an agent asks for one, as it would call it. */
export const OPEN_CALL = 'environment_open(title="Fix the login page", repo="github.com/you/app")';

export function EnvironmentsPage() {
  const { org } = useParams({ from: '/$org/environments' });
  const query = useEnvironments(org);
  const environments = query.data?.environments;
  const open = environments?.filter(isOpen) ?? [];

  return (
    <div className="grid gap-6">
      <PageHeader
        title={PLACES.environments.word}
        description={
          <>
            Cloud computers your agents rent, each with a terminal, a browser and your repository. A single-use one is deleted when the
            work is done. A kept one stays running, or is saved and comes back when an agent opens it again.
            {environments?.length ? <span className="text-fg"> {openSummary(open)}</span> : null}
          </>
        }
      />
      <EnvironmentsBody org={org} query={query} />
    </div>
  );
}

function EnvironmentsBody({ org, query }: { org: string; query: ReturnType<typeof useEnvironments> }) {
  if (query.isPending) {
    return (
      <Card role="status" aria-label="Loading environments">
        <TableSkeleton columns={OPEN_COLUMNS} rows={4} />
      </Card>
    );
  }
  if (isNotServed(query.error)) {
    return (
      <Card>
        <NotLive />
      </Card>
    );
  }
  if (query.error instanceof ApiError && query.error.status === 403) {
    return (
      <Card>
        <EmptyState
          icon={<Lock />}
          title="Environments are not shown to your role"
          description="Seeing them needs the env workspace in your role. An owner or admin can add it."
        />
      </Card>
    );
  }
  if (query.isError || !query.data) {
    return (
      <Card>
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      </Card>
    );
  }
  const { open, closed } = splitEnvironments(query.data.environments);
  const saved = query.data.saved ?? [];
  if (!open.length && !closed.length && !saved.length) {
    return (
      <Card>
        <EmptyState
          icon={<MonitorCloud />}
          title="No environments yet"
          description="An environment is a cloud computer of the agent's own, with a terminal, a browser and your repository. It is single use, or kept for the next chat. An agent connected to agentos asks for one with environment_open."
          action={<CodeBlock code={OPEN_CALL} label="Copy the call" className="max-w-full text-left" />}
        />
      </Card>
    );
  }
  return (
    <>
      <OpenCard org={org} open={open} />
      {saved.length ? <SavedCard org={org} saved={saved} now={Date.now()} /> : null}
      {closed.length ? <ClosedCard org={org} closed={closed} /> : null}
    </>
  );
}

function OpenCard({ org, open }: { org: string; open: EnvSummary[] }) {
  const now = useNow(open.length > 0);
  return (
    <Card className="overflow-hidden">
      <CardHeader title="Open" meta={open.length ? plural(open.length, 'environment') : undefined} />
      {open.length ? (
        <EnvTable org={org} rows={open} now={now} label="Open environments" />
      ) : (
        <p className="px-5 py-6 text-14 text-muted">
          Nothing is open. An agent gets one with <code className="font-mono text-code text-fg">environment_open</code>, and it shows here
          while it works.
        </p>
      )}
    </Card>
  );
}

function ClosedCard({ org, closed }: { org: string; closed: EnvSummary[] }) {
  const [all, setAll] = useState(false);
  const shown = all ? closed : closed.slice(0, CLOSED_SHOWN);
  const hidden = closed.length - CLOSED_SHOWN;
  return (
    <Card className="overflow-hidden">
      <CardHeader title="Closed" meta={plural(closed.length, 'environment')} />
      <EnvTable org={org} rows={shown} now={Date.now()} closed label="Closed environments" />
      {hidden > 0 ? (
        <div className="border-t border-line px-3 py-2">
          <Button variant="ghost" size="sm" aria-expanded={all} onClick={() => setAll((value) => !value)} icon={<ChevronDown className={cn('size-4 transition-transform duration-(--duration-fast)', all && 'rotate-180')} />}>
            {all ? 'Show the newest 5' : `Show all ${closed.length}`}
          </Button>
        </div>
      ) : null}
    </Card>
  );
}

function EnvTable({ org, rows, now, closed = false, label }: { org: string; rows: EnvSummary[]; now: number; closed?: boolean; label: string }) {
  const navigate = useNavigate();
  return (
    <TableScroll>
      <Table stack aria-label={label}>
        <thead>
          <tr>
            <Th>Environment</Th>
            <Th>{closed ? 'Result' : 'State'}</Th>
            <Th className="md:max-lg:hidden">Repository</Th>
            <Th className="md:max-lg:hidden" phone="hide">
              Agent
            </Th>
            <Th align="end" phone="hide">
              {closed ? 'Ran' : 'Age'}
            </Th>
            <Th align="end">Cost</Th>
            <Th align="end">{closed ? 'Closed' : 'Time left'}</Th>
            <Th className="w-1" phone="hide">
              <span className="sr-only">Actions</span>
            </Th>
          </tr>
        </thead>
        <tbody>
          {rows.map((env) => (
            <EnvRow key={env.id} org={org} env={env} now={now} closed={closed} onOpen={() => void navigate({ to: '/$org/environments/$id', params: { org, id: env.id } })} />
          ))}
        </tbody>
      </Table>
    </TableScroll>
  );
}

function EnvRow({ org, env, now, closed, onOpen }: { org: string; env: EnvSummary; now: number; closed: boolean; onOpen: () => void }) {
  const status = ENV_STATUS[env.state];
  const left = leftOf(env, now);
  return (
    <Tr onActivate={onOpen} aria-label={`${env.title}, ${status.label}`}>
      <Td main>
        <CellTitle
          title={
            <Link
              to="/$org/environments/$id"
              params={{ org, id: env.id }}
              onClick={(event) => event.stopPropagation()}
              tabIndex={-1}
              title={env.title}
              className="block truncate text-fg hover:underline"
            >
              {env.title}
            </Link>
          }
          second={
            <span className="font-mono text-code-caption" title={`${env.machine.server_type} in ${env.machine.location}, ${hourly(env.price_hourly_eur)}`}>
              {env.id} · {machineSize(env.machine)} · {env.machine.location}
            </span>
          }
        />
      </Td>
      <Td>
        {/* a flex column, not a grid: a grid in a table cell overflowed into the next column when the
            mode caption is longer than the status */}
        <span className="flex flex-col items-start gap-1">
          <span title={ENV_STATE_SENTENCE[env.state]}>
            <Status status={status} />
          </span>
          {/* a closed single-use one is gone for good: saying so on every row is noise */}
          {env.keep && !(closed && keepOf(env) === 'none') ? <KeepCaption env={env} /> : null}
        </span>
      </Td>
      <Td className="md:max-lg:hidden" phone={env.repo ? undefined : 'hide'}>
        {env.repo ? (
          <div className="grid w-52 min-w-0 justify-items-start gap-1 max-md:w-auto">
            <span className="max-w-full truncate font-mono text-code-caption text-muted" title={env.repo}>
              {repoName(env.repo)}
            </span>
            {env.branch ? <BranchPill name={env.branch} main={env.branch === 'main' || env.branch === 'master'} className="max-w-full" /> : null}
          </div>
        ) : (
          <span className="text-secondary text-faint">No repository</span>
        )}
      </Td>
      <Td className="md:max-lg:hidden" phone="hide">
        <span className="block max-w-44 truncate text-secondary text-fg" title={agentWords(env.agent)}>
          {env.agent.name}
          {env.agent.client && env.agent.client !== env.agent.name ? <span className="text-faint"> in {env.agent.client}</span> : null}
        </span>
      </Td>
      <Td numeric label={closed ? 'Ran' : 'Age'} phone="hide">
        <span title={`Opened ${dateTime(env.created_at)}`} className="text-muted">
          {span(ageOf(env, now))}
        </span>
      </Td>
      <Td numeric label="Cost">
        <span title={hourly(env.price_hourly_eur)}>{euro(env.cost_eur)}</span>
      </Td>
      <Td numeric label={closed ? 'Closed' : 'Time left'}>
        {closed ? (
          env.closed_at ? (
            <time dateTime={env.closed_at} title={dateTime(env.closed_at)} className="text-muted">
              {timeAgo(env.closed_at, now)}
            </time>
          ) : (
            <NoValue />
          )
        ) : left === null ? (
          <NoValue />
        ) : (
          <time dateTime={env.expires_at} title={`The node closes it at ${dateTime(env.expires_at)}`} className={left < SHORT_MS ? 'text-warn' : 'text-fg'}>
            {left <= 0 ? 'Ends now' : span(left)}
          </time>
        )}
      </Td>
      <Td className="w-1 text-right" phone="hide" onClick={(event) => event.stopPropagation()}>
        <RowActions>
          <CopyButton text={env.id} label={`Copy the id of ${env.title}`} />
        </RowActions>
      </Td>
    </Tr>
  );
}

/** How it stays, and what that costs while unused, under its state: "Saves when idle · cents a month". */
function KeepCaption({ env }: { env: EnvSummary }) {
  const keep = KEEP[keepOf(env)];
  const cost = idleShort(env);
  return (
    <span className="text-caption text-faint" title={`${keep.sentence} ${idleCost(env)}.`}>
      {keep.label}
      {cost ? ` · ${cost}` : null}
    </span>
  );
}
