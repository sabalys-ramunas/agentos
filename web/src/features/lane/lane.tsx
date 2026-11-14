import { Link, useParams } from '@tanstack/react-router';
import { ChevronDown, ChevronLeft, Cog, Eye, Moon, PanelRight, Power, SquareTerminal, TriangleAlert } from 'lucide-react';
import { lazy, Suspense, useState } from 'react';
import { AccountWorkerRow, WorkerOnAccount } from '@/features/workers/in-place';
import { useAccess, useActivity, useApprovals, useFlows, useFreezeStatus, useLane, useLaneAction, useNodes, useWorkspaces } from '@/api/hooks';
import { ApiError } from '@/api/http';
import type { Lane, TraceEvent } from '@/api/types';
import { SiteMark } from '@/components/brand/site-mark';
import { Button } from '@/components/ui/button';
import { Card, Section } from '@/components/ui/card';
import { Drawer } from '@/components/ui/dialog';
import { CopyButton, Detail } from '@/components/ui/misc';
import { Spinner } from '@/components/ui/spinner';
import { ConditionBadge, StateBadge } from '@/components/ui/state-badge';
import { StatusDot } from '@/components/ui/status';
import { EmptyState, ErrorState, Skeleton } from '@/components/ui/states';
import { clock, dateTime, timeAgo, timeUntil, usd } from '@/lib/format';
import { laneName, presetLabel, runStatus } from '@/lib/labels';
import { cn } from '@/lib/cn';
import { computerName } from '@/lib/computers';
import { coveringFreeze, frozenSentence } from '@/lib/freeze';
import { routineName } from '@/lib/principal';
import { STATE_WORDS } from '@/lib/states';
import { useTechnical } from '@/lib/technical';
import { accountCondition, accountName, didSentence, isStory } from '@/features/home/words';
import { FREEZE_WORDS, RUN_WORDS } from '@/lib/vocabulary';
import { ControlFrame, useHolder } from './control';
import { useFullWindow } from './live/full-window';
import { MissingLane } from './missing';
import { AccountAskPanel, RoutineAskWord } from '@/features/ask-first/ask-first';
import { LaneStanding } from '@/features/standing/lane-standing';
import { EngageOnAccount } from '@/features/engage/on-account';
import { FreezeButton, FrozenNotice } from '@/features/freeze/freeze';
import { SafeLine, KeepaliveSection } from './account-health';
import { ownActionSentence } from './account-words';
import { AccountFilesSection } from './account-files';
import { LimitsSection } from './account-limits';
import { TaintSection, WaitingSection } from './account-people';
import { PaceSection } from './account-pace';
import { SitesSection } from './account-sites';
import { WatchSection } from './account-watch';

const HELP: Partial<Record<Lane['signal'], { title: string; body: string }>> = {
  captcha: { title: 'The site wants a quick human check', body: 'Take over, do the check in the live view, and the account carries on.' },
  checkpoint: { title: 'The site wants to verify it is you', body: 'Take over and answer it in the live view.' },
  logged_out: { title: 'This account is signed out', body: 'Take over and sign in once. Your agent never sees the password.' },
  rate_limited: { title: 'The site asked this account to slow down', body: 'It tries again by itself. Nothing is needed from you yet.' },
  blocked: { title: 'The site is refusing this account for now', body: 'Take over to see what the site is showing.' },
};

/**
 * Why agents are held off, and until when, in words. Null when nothing is held. The words only
 * ask for what this person can do from here: a lane that is switched off or asleep has nothing
 * to take over yet, and someone who cannot operate it is told who can.
 */
export function holdNotice(lane: Lane, canTakeOver = true): { title: string; body: string } | null {
  const wrong = lane.account?.status === 'wrong_account';
  const help = wrong
    ? { title: 'This account is signed in as someone else', body: 'Take over and sign in to the right one. Agents are held off so nothing goes out as the wrong identity.' }
    : HELP[lane.signal];
  if (!help) return null;
  if (!wrong && lane.hold_until) {
    return { title: help.title, body: `Agents are held off until ${clock(lane.hold_until)}, ${timeUntil(lane.hold_until)} from now. It clears by itself.` };
  }
  if (!/^Take over/.test(help.body)) return help;
  if (!canTakeOver) return { title: help.title, body: 'Only the owner of this computer can take over and sort it out.' };
  // A switched-off lane says why and offers Reopen below. Take over comes after that.
  if (lane.state === 'quarantined') return { title: help.title, body: `It is switched off for safety. Turn it back on below first, then ${lowerFirst(help.body)}` };
  // A sleeping lane has nothing to take over yet, so the first step is to open it.
  if (lane.state === 'cold') return { title: help.title, body: `Wake it up first. ${help.body}` };
  return help;
}

const lowerFirst = (text: string) => text.charAt(0).toLowerCase() + text.slice(1);

function Stage({ children }: { children: React.ReactNode }) {
  return <div className="flex aspect-[16/10] flex-col items-center justify-center bg-subtle p-6 text-center">{children}</div>;
}

const VncScreen = lazy(() => import('./live/vnc-screen'));
const TerminalScreen = lazy(() => import('./live/terminal-screen'));

/** Shown while the viewer's code loads, shaped like the connecting state so nothing jumps. */
function ScreenLoading() {
  return (
    <div className="flex flex-col">
      <Stage>
        <Spinner className="mb-4 size-6 text-faint" />
        <h2 className="text-14 font-medium">Connecting</h2>
      </Stage>
      <div className="h-12 border-t border-line" />
    </div>
  );
}

/**
 * When the relay refuses before opening a socket, the browser hides the reason. The REST API
 * answers the same checks, so ask it.
 */
function useProbe(org: string, lane: Lane) {
  const query = useLane(org, lane.workspace, lane.name);
  const nodes = useNodes(org);
  return async () => {
    const result = await query.refetch();
    if (result.error instanceof ApiError) {
      if (result.error.status === 401) return 'signed_out' as const;
      if (result.error.status === 403) return 'denied' as const;
      if (result.error.status === 404) return 'missing' as const;
    }
    const node = (await nodes.refetch()).data?.find((item) => item.id === lane.node);
    return node?.state === 'down' ? ('away' as const) : null;
  };
}

export function LiveView({
  org,
  lane,
  opening,
  canOperate,
  canTake,
  onOpen,
  expanded = false,
  onExpandedChange = () => {},
}: {
  org: string;
  lane: Lane;
  opening: boolean;
  canOperate: boolean;
  /** Who may type into the live view. Default: the owner, who alone takes over an account (`lanes.control`). */
  canTake?: boolean;
  onOpen: () => void;
  expanded?: boolean;
  onExpandedChange?: (expanded: boolean) => void;
}) {
  const holder = useHolder(lane.control);
  const probe = useProbe(org, lane);
  const access = useAccess(org);
  const canWatch = access.can('lanes.watch');
  // Take over, hand back and stop an agent are the owner's alone: an operator watches and works the account.
  const canControl = (canTake ?? access.can('lanes.control')) && lane.kind !== 'service';
  const person = lane.control.state === 'human';
  if (lane.state === 'cold') {
    return (
      <Stage>
        <Moon className="mb-4 size-6 text-faint" />
        <h2 className="text-14 font-medium">This account is {STATE_WORDS.sleeping.toLowerCase()}</h2>
        <p className="mt-1 max-w-xs text-14 text-muted">
          {canOperate
            ? lane.kind === 'terminal'
              ? 'Open it to start its shell, then take over here to type.'
              : lane.kind === 'service'
                ? 'Open it to start its service. What it does shows in History below.'
                : 'Open it to start the browser, run the self-check and watch it here.'
            : 'A member of this workspace can open it.'}
        </p>
        {canOperate ? (
          <Button className="mt-4" variant="primary" loading={opening} onClick={onOpen}>
            Wake it up
          </Button>
        ) : null}
      </Stage>
    );
  }
  if (lane.state === 'warming') {
    return (
      <Stage>
        <Spinner className="mb-4 size-6 text-faint" />
        <h2 className="text-14 font-medium">Warming up</h2>
        <p className="mt-1 text-14 text-muted">Starting the display and running the self-check.</p>
      </Stage>
    );
  }
  if (lane.state === 'quarantined') {
    return (
      <Stage>
        <TriangleAlert className="mb-4 size-6 text-faint" />
        <h2 className="text-14 font-medium">This account is switched off for safety</h2>
        <p className="mt-1 max-w-sm text-14 text-muted">{lane.quarantine_reason ?? 'It failed its own safety check, and does nothing until someone turns it back on.'}</p>
        {canOperate ? (
          <Button className="mt-4" loading={opening} onClick={onOpen}>
            Turn it back on
          </Button>
        ) : null}
      </Stage>
    );
  }
  if (lane.kind === 'service') {
    return (
      <Stage>
        <Cog className="mb-4 size-6 text-faint" />
        <h2 className="text-14 font-medium">This tool runs a service</h2>
        <p className="mt-1 max-w-sm text-14 text-muted">It has no screen or shell to show. What it does appears in History below.</p>
      </Stage>
    );
  }
  const props = {
    org,
    lane,
    interactive: canControl && person && holder.mine,
    holder: person && !holder.mine ? holder.name : null,
    canTake: canControl,
    expanded,
    onExpandedChange,
    probe,
  };
  if (lane.kind === 'terminal') {
    if (!canControl) {
      return (
        <Stage>
          <SquareTerminal className="mb-4 size-6 text-faint" />
          <h2 className="text-14 font-medium">A shell on this account</h2>
          <p className="mt-1 max-w-sm text-14 text-muted">Only the owner of this computer can open it here.</p>
        </Stage>
      );
    }
    return (
      <Suspense fallback={<ScreenLoading />}>
        <TerminalScreen {...props} />
      </Suspense>
    );
  }
  if (!canWatch) {
    return (
      <Stage>
        <Eye className="mb-4 size-6 text-faint" />
        <h2 className="text-14 font-medium">Live view is for members</h2>
        <p className="mt-1 max-w-sm text-14 text-muted">It shows whatever the account shows (messages, settings, one-time codes), so it is for members who operate this account.</p>
      </Stage>
    );
  }
  return (
    <Suspense fallback={<ScreenLoading />}>
      <VncScreen {...props} />
    </Suspense>
  );
}

function ActivityRow({ row }: { row: TraceEvent }) {
  const [technical] = useTechnical();
  // A change the person made is not a check on a site, so it carries no "Not checked" word.
  const own = ownActionSentence(row);
  return (
    <li className="flex h-10 items-center gap-4 border-b border-line px-4 last:border-b-0">
      <StatusDot tone={runStatus[row.status].tone} />
      <span className={cn('min-w-0 flex-1 truncate text-13', technical && 'mono')}>{technical ? row.summary : (own ?? didSentence(row))}</span>
      {own ? null : <span className="hidden text-12 text-faint sm:inline">{RUN_WORDS[row.status].word}</span>}
      <span className="tabular w-14 shrink-0 text-right text-12 text-faint">{timeAgo(row.at)}</span>
    </li>
  );
}

function LaneActivity({ org, lane }: { org: string; lane: Lane }) {
  const activity = useActivity(org, { lane: lane.id });
  const [technical] = useTechnical();
  // What agents did, and what the person did to the account (take over, freeze, pace, limits), as the audit names it.
  const rows = (activity.data?.pages[0]?.items ?? []).filter((item) => technical || isStory(item) || ownActionSentence(item) !== null).slice(0, 8);
  return (
    <Section
      title="History"
      action={
        <Link to="/$org/activity" params={{ org }} search={{ lane: lane.id }} className="inline-flex min-h-8 items-center text-13 text-muted hover:text-fg md:min-h-0">
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
function NeedsFirst({ org, lane }: { org: string; lane: Lane }) {
  const pending = (useApprovals(org).data ?? []).filter((item) => item.lane === lane.id);
  if (!pending.length) return <TaintSection org={org} lane={lane} />;
  return (
    <div className="grid gap-8">
      <Link
        to="/$org/needs-you"
        params={{ org }}
        className="flex min-h-12 items-center justify-between gap-3 rounded-xl border border-line px-4 py-2 text-14 transition-colors duration-(--duration-base) ease-standard hover:border-line-strong hover:bg-fill-1"
      >
        {pending.length === 1 ? 'One thing waits for your yes' : `${pending.length} things wait for your yes`}
        <span className="shrink-0 text-13 whitespace-nowrap text-muted">Look and decide</span>
      </Link>
      <TaintSection org={org} lane={lane} />
    </div>
  );
}

/** The settings that change how this account runs, closed until someone opens them, so the page reads calmly. */
function AccountSettings({ org, lane }: { org: string; lane: Lane }) {
  const [technical] = useTechnical();
  const flows = (useFlows(org).data ?? []).filter((item) => item.lane === lane.id);
  return (
    <details className="group rounded-xl border border-line">
      <summary className="flex min-h-12 cursor-pointer list-none items-center justify-between gap-3 rounded-xl px-4 py-3 text-14 font-medium transition-colors duration-(--duration-base) ease-standard hover:bg-fill-1 [&::-webkit-details-marker]:hidden">
        <span>Settings</span>
        <span className="hidden min-w-0 flex-1 truncate text-13 font-normal text-muted sm:block">Runs on its own, pace, limits, sites, sign-in upkeep, files and a watch link</span>
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
            <h2 className="mb-2 text-13 text-faint">Routines for this account</h2>
            <ul className="grid gap-1">
              {flows.map((flow) => (
                <li key={flow.id} className="flex items-center justify-between gap-4 py-1">
                  <span className="min-w-0">
                    <Link to="/$org/flows/$flow" params={{ org, flow: flow.id }} className={cn('block truncate text-13 hover:underline', technical && 'mono')}>
                      {technical ? flow.id : routineName(flow.id)}
                    </Link>
                    <span className="block text-12 text-faint">{flow.last_run ? (technical ? usd(flow.last_run.cost.usd) : timeAgo(flow.last_run.started_at)) : 'Not run yet'}</span>
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

function Inspect({ lane, open, onOpenChange }: { lane: Lane; open: boolean; onOpenChange: (open: boolean) => void }) {
  const group = 'divide-y divide-line';
  return (
    <Drawer open={open} onOpenChange={onOpenChange} title="Technical details" description={lane.id}>
      <div className="grid gap-6">
        <section>
          <h3 className="mb-1 text-13 font-medium">Identity</h3>
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
              <span className="block text-12 text-faint">{presetLabel[lane.preset]?.hint}</span>
            </Detail>
            <Detail label="Engine">
              <span className="mono">{lane.engine ?? '–'}</span>
            </Detail>
            <Detail label="Login hint">{lane.logged_in_hint.replace('_', ' ')}</Detail>
          </dl>
        </section>
        <section>
          <h3 className="mb-1 text-13 font-medium">Network</h3>
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
          <p className="mt-2 text-12 text-faint">Proxy credentials stay on the computer that runs this account.</p>
        </section>
        <section>
          <h3 className="mb-1 text-13 font-medium">Runtime</h3>
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

function LaneSkeleton() {
  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_280px]">
      <Skeleton className="aspect-[16/10] h-auto rounded-xl" />
      <Skeleton className="h-40 rounded-xl" />
    </div>
  );
}

export function LaneWindow() {
  const { org, ws, name } = useParams({ from: '/$org/lanes/$ws/$name' });
  const lane = useLane(org, ws, name);
  const workspace = useWorkspaces(org).data?.find((item) => item.id === ws);
  const action = useLaneAction(org, ws, name);
  const [inspect, setInspect] = useState(false);
  const [expanded, setExpanded] = useFullWindow();
  const access = useAccess(org);
  const canOperate = access.can('lanes.operate');
  const canTakeOver = access.can('lanes.control');
  const freezes = useFreezeStatus(org);
  const data = lane.data;
  const awake = data && data.state !== 'cold' && data.state !== 'warming' && data.state !== 'quarantined';
  const help = data ? holdNotice(data, canTakeOver) : null;
  // Nothing is offered until the node has answered, and a node without the freeze service offers none.
  const freezable = freezes.data !== undefined && freezes.data.available !== false;
  const covering = data ? coveringFreeze(freezes.data, data.id) : null;
  // The account as a person names it, "Aster Coffee's Instagram", for the freeze and what lifts it.
  const accountTitle = data ? (workspace ? `${workspace.name}'s ${accountName(data)}` : accountName(data)) : '';
  const coveringName = covering?.scope === 'node' || covering?.scope === 'mesh' ? computerName(covering.target) : covering?.scope === 'workspace' ? (workspace?.name ?? 'this workspace') : accountTitle;

  return (
    <>
      <Link to="/$org/accounts" params={{ org }} search={{ workspace: ws }} className="mb-4 inline-flex min-h-8 items-center gap-1 text-13 text-faint hover:text-fg md:min-h-0">
        <ChevronLeft className="size-4" />
        {workspace ? `${workspace.name}'s accounts` : <Skeleton className="h-3 w-20" />}
      </Link>
      <header className="mb-2 flex flex-wrap items-center justify-between gap-4">
        <div className="flex min-w-0 items-center gap-4">
          <SiteMark site={data?.site?.key ?? name} size={32} label={false} />
          <h1 className="truncate text-24 font-semibold">
            {workspace ? <span className="text-faint">{workspace.name} · </span> : null}
            {data ? accountName(data) : laneName(name)}
          </h1>
          {data && covering ? (
            <StateBadge state="stuck" label={FREEZE_WORDS.frozen.word} sentence={frozenSentence(covering)} className="shrink-0" />
          ) : data ? (
            <ConditionBadge condition={accountCondition(data)} />
          ) : null}
        </div>
        {data ? (
          <div className="flex w-full flex-wrap items-center gap-2 sm:w-auto">
            <Button icon={<PanelRight className="size-4" />} onClick={() => setInspect(true)}>
              Technical details
            </Button>
            {access.can('freeze.account') && freezable && !covering ? <FreezeButton org={org} scope="lane" target={data.id} name={accountTitle} /> : null}
            {canOperate && awake ? (
              <Button icon={<Power className="size-4" />} loading={action.isPending} onClick={() => action.mutate('close')}>
                Put to sleep
              </Button>
            ) : null}
          </div>
        ) : null}
      </header>
      {data ? (
        <p className="mb-6 text-13 text-faint">
          {presetLabel[data.preset]?.name ?? data.preset} · Runs on {computerName(data.node)} · {data.last_used ? `Last used ${timeAgo(data.last_used)}` : 'Not used yet'}
        </p>
      ) : null}
      {lane.isPending ? (
        <LaneSkeleton />
      ) : lane.isError ? (
        <Card>
          <ErrorState error={lane.error} onRetry={() => void lane.refetch()} />
        </Card>
      ) : data ? (
        <div className="grid min-w-0 max-w-3xl gap-8">
          <MissingLane org={org} lane={data} />
          {covering ? <FrozenNotice org={org} record={covering} name={coveringName} canLift={access.can('freeze.lift')} /> : null}
          {help ? (
            <div role="status" className="flex items-start gap-2 rounded-xl border border-line px-4 py-3">
              <StatusDot tone="warn" className="mt-2" />
              <p className="min-w-0 text-14">
                <span className="font-medium">{help.title}.</span> <span className="text-muted">{help.body}</span>
              </p>
            </div>
          ) : null}
          <SafeLine org={org} lane={data} />
          <NeedsFirst org={org} lane={data} />
          {awake || data.control.state !== 'agent' ? (
            <ControlFrame org={org} lane={data} canControl={canTakeOver && data.kind !== 'service'} expanded={expanded && Boolean(awake)}>
              <LiveView
                org={org}
                lane={data}
                opening={action.isPending}
                canOperate={canOperate}
                onOpen={() => action.mutate('open')}
                expanded={expanded && Boolean(awake)}
                onExpandedChange={setExpanded}
              />
            </ControlFrame>
          ) : (
            <Card className="overflow-hidden">
              <LiveView org={org} lane={data} opening={action.isPending} canOperate={canOperate} onOpen={() => action.mutate('open')} />
            </Card>
          )}
          <EngageOnAccount org={org} ws={data.workspace} name={data.name} />
          <WaitingSection org={org} lane={data} />
          <WorkerOnAccount org={org} lane={data.id} />
          <LaneActivity org={org} lane={data} />
          <AccountSettings org={org} lane={data} />
        </div>
      ) : null}
      {data ? <Inspect lane={data} open={inspect} onOpenChange={setInspect} /> : null}
    </>
  );
}
