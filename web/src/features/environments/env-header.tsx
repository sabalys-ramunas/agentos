/** The top of an environment's page: its name and state, more time, and the facts tiles. */
import { Clock } from 'lucide-react';
import { toast } from 'sonner';
import { useAccess, useExtendEnvironment } from '@/api/hooks';
import { ApiError } from '@/api/http';
import type { EnvDetail } from '@/api/types';
import { BranchPill } from '@/components/ui/branch';
import { Button } from '@/components/ui/button';
import { Menu, MenuContent, MenuItem, MenuTrigger } from '@/components/ui/menu';
import { StatStrip, StatTile } from '@/components/ui/stat-tile';
import { Status, StatusDot } from '@/components/ui/status';
import { NoValue } from '@/components/ui/table';
import { clock, dateTime, timeAgo } from '@/lib/format';
import { CloseEnvironment } from './close-flow';
import {
  ENV_STATE_SENTENCE,
  ENV_STATUS,
  KEEP,
  SHORT_MS,
  ageOf,
  agentWords,
  closedReason,
  continueLine,
  euro,
  hourly,
  isOpen,
  keepOf,
  leftOf,
  machineWhere,
  repoName,
  span,
} from './words';
import { EXTEND } from './environment-page';

export function EnvHeader({ org, env, now }: { org: string; env: EnvDetail; now: number }) {
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
            {env.keep ? (
              <>
                <span aria-hidden="true" className="text-faint">
                  ·
                </span>
                <span title={KEEP[keepOf(env)].sentence}>{KEEP[keepOf(env)].label}</span>
              </>
            ) : null}
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

export function ClosedNotice({ env }: { env: EnvDetail }) {
  const failed = env.state === 'failed';
  const lead = failed ? 'It failed to start.' : env.closed_at ? `Closed ${timeAgo(env.closed_at).toLowerCase()}.` : 'Closed.';
  return (
    <div role="status" className="flex items-start gap-3 rounded-xl border border-line bg-card px-5 py-4 shadow-lift">
      <StatusDot tone={failed ? 'bad' : 'neutral'} className="mt-2" />
      <p className="min-w-0 text-14">
        <span className="font-medium text-fg">{lead}</span> <span className="text-muted">{closedReason(env.closed_reason, env.state)}</span>
        <span className="mt-1 block text-13 text-faint">
          {keepOf(env) === 'save' && !failed
            ? `Its machine and browser are deleted, and its work is under Saved. An agent brings it back when it opens it again: say "${continueLine(env.title)}".`
            : 'Its machine and browser are deleted. What is below is what the node kept.'}
        </span>
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

export function Facts({ env, now }: { env: EnvDetail; now: number }) {
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
