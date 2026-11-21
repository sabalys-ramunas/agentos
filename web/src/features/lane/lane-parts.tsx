/** The lower half of an account's page: its settings behind one disclosure, and its technical details. */
import { Link } from '@tanstack/react-router';
import { ChevronDown } from 'lucide-react';
import { AccountWorkerRow } from '@/features/workers/in-place';
import { useFlows } from '@/api/hooks';
import type { Lane } from '@/api/types';
import { Drawer } from '@/components/ui/dialog';
import { CopyButton, Detail } from '@/components/ui/misc';
import { Skeleton } from '@/components/ui/states';
import { dateTime, timeAgo, usd } from '@/lib/format';
import { presetLabel } from '@/lib/labels';
import { cn } from '@/lib/cn';
import { routineName } from '@/lib/principal';
import { useTechnical } from '@/lib/technical';
import { AccountAskPanel, RoutineAskWord } from '@/features/ask-first/ask-first';
import { LaneStanding } from '@/features/standing/lane-standing';
import { KeepaliveSection } from './account-health';
import { AccountFilesSection } from './account-files';
import { LimitsSection } from './account-limits';
import { PaceSection } from './account-pace';
import { SitesSection } from './account-sites';
import { WatchSection } from './account-watch';

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

/** The screen and the panels beside it, on the night, shaped like what comes. */
export function LaneSkeleton() {
  const night = 'bg-side-hover';
  return (
    <div role="status" aria-label="Loading the account" className="grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_312px]">
      <div className="grid gap-4">
        <Skeleton className={`aspect-[16/10] h-auto w-full rounded-lg ${night}`} />
        <Skeleton className={`mx-auto h-12 w-80 max-w-full rounded-full ${night}`} />
      </div>
      <div className="hidden gap-3 lg:grid">
        <Skeleton className={`h-32 rounded-xl ${night}`} />
        <Skeleton className={`h-64 rounded-xl ${night}`} />
      </div>
    </div>
  );
}
