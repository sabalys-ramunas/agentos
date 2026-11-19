/** The lower half of an account's page: what it did lately, what it needs first, its settings and its technical details. */
import { Link } from '@tanstack/react-router';
import { ChevronDown } from 'lucide-react';
import { AccountWorkerRow } from '@/features/workers/in-place';
import { useActivity, useApprovals, useFlows } from '@/api/hooks';
import type { Lane, TraceEvent } from '@/api/types';
import { Card, Section } from '@/components/ui/card';
import { Drawer } from '@/components/ui/dialog';
import { CopyButton, Detail } from '@/components/ui/misc';
import { StatusDot } from '@/components/ui/status';
import { EmptyState, Skeleton } from '@/components/ui/states';
import { dateTime, timeAgo, usd } from '@/lib/format';
import { presetLabel, runStatus } from '@/lib/labels';
import { cn } from '@/lib/cn';
import { routineName } from '@/lib/principal';
import { useTechnical } from '@/lib/technical';
import { didSentence, isStory } from '@/features/home/words';
import { RUN_WORDS } from '@/lib/vocabulary';
import { AccountAskPanel, RoutineAskWord } from '@/features/ask-first/ask-first';
import { LaneStanding } from '@/features/standing/lane-standing';
import { KeepaliveSection } from './account-health';
import { ownActionSentence } from './account-words';
import { AccountFilesSection } from './account-files';
import { LimitsSection } from './account-limits';
import { TaintSection } from './account-people';
import { PaceSection } from './account-pace';
import { SitesSection } from './account-sites';
import { WatchSection } from './account-watch';

function ActivityRow({ row }: { row: TraceEvent }) {
  const [technical] = useTechnical();
  // A change the person made is not a check on a site, so it carries no "Not checked" word.
  const own = ownActionSentence(row);
  return (
    <li className="flex h-10 items-center gap-4 border-b border-line px-4 last:border-b-0">
      <StatusDot tone={runStatus[row.status].tone} />
      <span className={cn('min-w-0 flex-1 truncate text-secondary', technical && 'mono')}>{technical ? row.summary : (own ?? didSentence(row))}</span>
      {own ? null : <span className="hidden text-caption text-faint sm:inline">{RUN_WORDS[row.status].word}</span>}
      <span className="tabular w-14 shrink-0 text-right text-caption text-faint">{timeAgo(row.at)}</span>
    </li>
  );
}

export function LaneActivity({ org, lane }: { org: string; lane: Lane }) {
  const activity = useActivity(org, { lane: lane.id });
  const [technical] = useTechnical();
  // What agents did, and what the person did to the account (take over, freeze, pace, limits), as the audit names it.
  const rows = (activity.data?.pages[0]?.items ?? []).filter((item) => technical || isStory(item) || ownActionSentence(item) !== null).slice(0, 8);
  return (
    <Section
      title="History"
      action={
        <Link to="/$org/activity" params={{ org }} search={{ lane: lane.id }} className="inline-flex min-h-8 items-center text-secondary text-muted hover:text-fg md:min-h-0">
          All of its history
        </Link>
      }
    >
      <Card>
        {activity.isPending ? (
          <div className="grid gap-4 p-4">
            <Skeleton />
            <Skeleton className="w-2/3" />
          </div>
        ) : rows.length ? (
          <ul>
            {rows.map((row) => (
              <ActivityRow key={row.id} row={row} />
            ))}
          </ul>
        ) : (
          <EmptyState title="Nothing done here yet" description="What agents do on this account appears here as it happens." className="py-8" />
        )}
      </Card>
    </Section>
  );
}

/**
 * What needs a person first, then the less used settings. A short list of what the account is doing now is
 * above this, on the page itself. Nothing here is a problem twice: the problem is shown once, above.
 */
export function NeedsFirst({ org, lane }: { org: string; lane: Lane }) {
  const pending = (useApprovals(org).data ?? []).filter((item) => item.lane === lane.id);
  if (!pending.length) return <TaintSection org={org} lane={lane} />;
  return (
    <div className="grid gap-8">
      <Link
        to="/$org/needs-you"
        params={{ org }}
        className="flex min-h-12 items-center justify-between gap-3 rounded-xl border border-line px-4 py-2 text-body transition-colors duration-(--duration-base) ease-standard hover:border-line-strong hover:bg-hover"
      >
        {pending.length === 1 ? 'One thing waits for your yes' : `${pending.length} things wait for your yes`}
        <span className="shrink-0 text-secondary whitespace-nowrap text-muted">Look and decide</span>
      </Link>
      <TaintSection org={org} lane={lane} />
    </div>
  );
}

/** The settings that change how this account runs, closed until someone opens them, so the page reads calmly. */
export function AccountSettings({ org, lane }: { org: string; lane: Lane }) {
  const [technical] = useTechnical();
  const flows = (useFlows(org).data ?? []).filter((item) => item.lane === lane.id);
  return (
    <details className="group rounded-xl border border-line">
      <summary className="flex min-h-12 cursor-pointer list-none items-center justify-between gap-3 rounded-xl px-4 py-3 text-label transition-colors duration-(--duration-base) ease-standard hover:bg-hover [&::-webkit-details-marker]:hidden">
        <span>Settings</span>
        <span className="hidden min-w-0 flex-1 truncate text-secondary text-muted sm:block">Runs on its own, pace, limits, sites, sign-in upkeep, files and a watch link</span>
        <ChevronDown aria-hidden="true" className="size-4 shrink-0 text-faint transition-transform duration-(--duration-base) ease-standard group-open:rotate-180" />
      </summary>
      <div className="grid gap-8 border-t border-line px-4 py-6">
        <AccountAskPanel org={org} lane={lane} />
        <AccountWorkerRow org={org} lane={lane} />
        <PaceSection org={org} lane={lane} />
        <LimitsSection org={org} lane={lane} />
        <SitesSection org={org} lane={lane} />
        <KeepaliveSection org={org} lane={lane} />
        <AccountFilesSection org={org} lane={lane} />
        <WatchSection org={org} lane={lane} />
        <LaneStanding org={org} lane={lane} />
        {flows.length ? (
          <div>
            <h2 className="mb-2 text-secondary text-faint font-semibold">Routines for this account</h2>
            <ul className="grid gap-1">
              {flows.map((flow) => (
                <li key={flow.id} className="flex items-center justify-between gap-4 py-1">
                  <span className="min-w-0">
                    <Link to="/$org/flows/$flow" params={{ org, flow: flow.id }} className={cn('block truncate text-secondary hover:underline', technical && 'mono')}>
                      {technical ? flow.id : routineName(flow.id)}
                    </Link>
                    <span className="block text-caption text-faint">{flow.last_run ? (technical ? usd(flow.last_run.cost.usd) : timeAgo(flow.last_run.started_at)) : 'Not run yet'}</span>
                  </span>
                  <span className="shrink-0">
                    <RoutineAskWord org={org} flow={flow} />
                  </span>
                </li>
              ))}
            </ul>
          </div>
        ) : null}
      </div>
    </details>
  );
}

/** The cloud sends a proxy as its label alone, and never its address. Only a full pair is shown. */
export function proxyHost(proxy: Lane['proxy']): string | null {
  return proxy?.host && proxy.port ? `${proxy.host}:${proxy.port}` : null;
}

export function Inspect({ lane, open, onOpenChange }: { lane: Lane; open: boolean; onOpenChange: (open: boolean) => void }) {
  const group = 'divide-y divide-line';
  return (
    <Drawer open={open} onOpenChange={onOpenChange} title="Technical details" description={lane.id}>
      <div className="grid gap-6">
        <section>
          <h3 className="mb-1 text-secondary font-medium">Identity</h3>
          <dl className={group}>
            <Detail label="Lane id">
              {/* Reversed, so the value stays on the right edge with the rows around it. */}
              <span className="inline-flex flex-row-reverse items-center gap-1">
                <span className="mono">{lane.id}</span>
                <CopyButton text={lane.id} label="Copy lane id" />
              </span>
            </Detail>
            <Detail label="Kind">{lane.kind}</Detail>
            <Detail label="Isolation">
              {presetLabel[lane.preset]?.name ?? lane.preset}
              <span className="block text-caption text-faint">{presetLabel[lane.preset]?.hint}</span>
            </Detail>
            <Detail label="Engine">
              <span className="mono">{lane.engine ?? '–'}</span>
            </Detail>
            <Detail label="Login hint">{lane.logged_in_hint.replace('_', ' ')}</Detail>
          </dl>
        </section>
        <section>
          <h3 className="mb-1 text-secondary font-medium">Network</h3>
          <dl className={group}>
            <Detail label="Proxy">
              <span className="mono">{lane.proxy?.label ?? 'None'}</span>
            </Detail>
            {proxyHost(lane.proxy) ? (
              <Detail label="Proxy host">
                <span className="mono">{proxyHost(lane.proxy)}</span>
              </Detail>
            ) : null}
            <Detail label="Exit IP">
              <span className="mono">{lane.exit_ip ?? '–'}</span>
            </Detail>
          </dl>
          <p className="mt-2 text-caption text-faint">Proxy credentials stay on the computer that runs this account.</p>
        </section>
        <section>
          <h3 className="mb-1 text-secondary font-medium">Runtime</h3>
          <dl className={group}>
            <Detail label="Node">
              <span className="mono">{lane.node}</span>
            </Detail>
            <Detail label="State">{lane.state}</Detail>
            <Detail label="Signal">{lane.signal}</Detail>
            {lane.signal_detail ? <Detail label="Rule that matched">{lane.signal_detail}</Detail> : null}
            <Detail label="Hold">{lane.hold ?? 'None'}</Detail>
            {lane.hold_until ? <Detail label="Hold ends">{dateTime(lane.hold_until)}</Detail> : null}
            <Detail label="Quarantine reason">{lane.quarantine_reason ?? 'None'}</Detail>
          </dl>
        </section>
      </div>
    </Drawer>
  );
}

export function LaneSkeleton() {
  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_280px]">
      <Skeleton className="aspect-[16/10] h-auto rounded-xl" />
      <Skeleton className="h-40 rounded-xl" />
    </div>
  );
}
