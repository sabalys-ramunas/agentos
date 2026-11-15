/**
 * Home's open environments: at the top, one compact row each (state, title, repository and branch, the
 * agent, cost so far, time left), and nothing at all when none is open or the node cannot say. It reads the
 * same list as the page and the rail, so the three agree.
 */
import { Link } from '@tanstack/react-router';
import { ChevronRight, MonitorCloud } from 'lucide-react';
import { useEnvironments } from '@/api/hooks';
import type { EnvSummary } from '@/api/types';
import { BranchPill } from '@/components/ui/branch';
import { Card, CardHeader } from '@/components/ui/card';
import { Status } from '@/components/ui/status';
import { cn } from '@/lib/cn';
import { plural } from '@/lib/format';
import { useNow } from '@/lib/use-now';
import { ENV_STATUS, SHORT_MS, euro, leftOf, leftWords, repoName, splitEnvironments } from './words';

/** Rows before "See all": Home stays a glance. */
const HOME_ROWS = 4;

export function HomeEnvironments({ org }: { org: string }) {
  const query = useEnvironments(org);
  const open = query.data ? splitEnvironments(query.data.environments).open : [];
  const now = useNow(open.length > 0);
  if (!open.length) return null;
  const shown = open.slice(0, HOME_ROWS);
  return (
    <Card className="mb-10 overflow-hidden">
      <CardHeader
        title={
          <>
            <MonitorCloud aria-hidden="true" className="size-4 text-faint" />
            Environments
          </>
        }
        meta={`${plural(open.length, 'open', 'open')}`}
        action={
          <Link
            to="/$org/environments"
            params={{ org }}
            className="inline-flex h-7 items-center gap-1 rounded-md px-2 text-13 font-medium text-muted hover:bg-hover hover:text-fg"
          >
            {open.length > HOME_ROWS ? `See all ${open.length}` : 'See all'}
            <ChevronRight aria-hidden="true" className="size-4" />
          </Link>
        }
      />
      <ul aria-label="Open environments" className="divide-y divide-line">
        {shown.map((env) => (
          <HomeRow key={env.id} org={org} env={env} now={now} />
        ))}
      </ul>
    </Card>
  );
}

function HomeRow({ org, env, now }: { org: string; env: EnvSummary; now: number }) {
  const left = leftOf(env, now);
  return (
    <li>
      <Link
        to="/$org/environments/$id"
        params={{ org, id: env.id }}
        className="group grid min-h-13 grid-cols-[6rem_minmax(0,1fr)_auto] items-center gap-x-4 gap-y-1 px-5 py-2 transition-colors duration-(--duration-fast) hover:bg-hover max-sm:grid-cols-[minmax(0,1fr)_auto]"
      >
        <span className="max-sm:order-2 max-sm:justify-self-end">
          <Status status={ENV_STATUS[env.state]} />
        </span>
        <span className="grid min-w-0 gap-1 max-sm:order-1 sm:flex sm:items-center sm:gap-3">
          <span className="truncate text-14 font-medium text-fg" title={env.title}>
            {env.title}
          </span>
          {env.repo ? (
            <span className="flex min-w-0 items-center gap-2">
              <span className="min-w-0 shrink truncate font-mono text-code-caption text-faint" title={env.repo}>
                {repoName(env.repo)}
              </span>
              {/* The repo gives way first, so a short branch ("main") is never cut to "m…". */}
              {env.branch ? <BranchPill name={env.branch} main={env.branch === 'main' || env.branch === 'master'} className="max-w-[60%] shrink-0 sm:max-w-48" /> : null}
            </span>
          ) : null}
        </span>
        <span className="flex items-center gap-4 max-sm:order-3 max-sm:col-span-2 max-sm:gap-3">
          <span className="text-secondary text-muted max-md:hidden">{env.agent.name}</span>
          <span className="font-mono text-number text-fg tabular" title="Cost so far">
            {euro(env.cost_eur)}
          </span>
          {left !== null ? (
            <span className={cn('font-mono text-number tabular', left < SHORT_MS ? 'text-warn' : 'text-muted')} title="Before the node closes it">
              {leftWords(left)}
            </span>
          ) : null}
        </span>
      </Link>
    </li>
  );
}
