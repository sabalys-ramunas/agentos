import { Link, useParams } from '@tanstack/react-router';
import { ChevronLeft, PanelRight, Power } from 'lucide-react';
import { useState } from 'react';
import { WorkerOnAccount } from '@/features/workers/in-place';
import { useAccess, useFreezeStatus, useLane, useLaneAction, useWorkspaces } from '@/api/hooks';
import type { FreezeRecord, FreezeStatus, Lane, Workspace } from '@/api/types';
import { SiteMark } from '@/components/brand/site-mark';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { ConditionBadge, StateBadge } from '@/components/ui/state-badge';
import { StatusDot } from '@/components/ui/status';
import { ErrorState, Skeleton } from '@/components/ui/states';
import { clock, timeAgo, timeUntil } from '@/lib/format';
import { laneName, presetLabel } from '@/lib/labels';
import { computerName } from '@/lib/computers';
import { coveringFreeze, frozenSentence } from '@/lib/freeze';
import { accountCondition, accountName } from '@/features/home/words';
import { FREEZE_WORDS } from '@/lib/vocabulary';
import { ControlFrame } from './control';
import { useFullWindow } from './live/full-window';
import { MissingLane } from './missing';
import { FreezeButton, FrozenNotice } from '@/features/freeze/freeze';
import { SafeLine } from './account-health';
import { WaitingSection } from './account-people';
import { AccountSettings, Inspect, LaneActivity, LaneSkeleton, NeedsFirst } from './lane-parts';
import { LiveView } from './live-view';

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

/** The words for the freeze over this account, if any: what is frozen, and who it is named after. */
function freezeOf(lane: Lane | undefined, workspace: Workspace | undefined, freezes: FreezeStatus | undefined) {
  // Nothing is offered until the node has answered, and a node without the freeze service offers none.
  const freezable = freezes !== undefined && freezes.available !== false;
  const covering = lane ? coveringFreeze(freezes, lane.id) : null;
  // The account as a person names it, "Aster Coffee's Instagram", for the freeze and what lifts it.
  const accountTitle = lane ? (workspace ? `${workspace.name}'s ${accountName(lane)}` : accountName(lane)) : '';
  const coveringName = covering?.scope === 'node' || covering?.scope === 'mesh' ? computerName(covering.target) : covering?.scope === 'workspace' ? (workspace?.name ?? 'this workspace') : accountTitle;
  return { freezable, covering, accountTitle, coveringName };
}

export function LaneWindow() {
  const { org, ws, name } = useParams({ from: '/$org/lanes/$ws/$name' });
  const lane = useLane(org, ws, name);
  const workspace = useWorkspaces(org).data?.find((item) => item.id === ws);
  const action = useLaneAction(org, ws, name);
  const [inspect, setInspect] = useState(false);
  const full = useFullWindow();
  const freezes = useFreezeStatus(org);
  const data = lane.data;
  const awake = Boolean(data && data.state !== 'cold' && data.state !== 'warming' && data.state !== 'quarantined');
  const freeze = freezeOf(data, workspace, freezes.data);

  return (
    <>
      <Link to="/$org/accounts" params={{ org }} search={{ workspace: ws }} className="mb-4 inline-flex min-h-8 items-center gap-1 text-13 text-faint hover:text-fg md:min-h-0">
        <ChevronLeft className="size-4" />
        {workspace ? `${workspace.name}'s accounts` : <Skeleton className="h-3 w-20" />}
      </Link>
      <LaneHeader org={org} name={name} lane={data} workspace={workspace} awake={awake} action={action} freeze={freeze} onInspect={() => setInspect(true)} />
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
        <LaneBody org={org} lane={data} awake={awake} action={action} full={full} covering={freeze.covering} coveringName={freeze.coveringName} />
      ) : null}
      {data ? <Inspect lane={data} open={inspect} onOpenChange={setInspect} /> : null}
    </>
  );
}

interface LaneHeaderProps {
  org: string;
  name: string;
  lane: Lane | undefined;
  workspace: Workspace | undefined;
  awake: boolean;
  action: ReturnType<typeof useLaneAction>;
  freeze: ReturnType<typeof freezeOf>;
  onInspect: () => void;
}

/** The account's mark, name and state, with its technical details, freeze and sleep buttons. */
function LaneHeader({ org, name, lane: data, workspace, awake, action, freeze, onInspect }: LaneHeaderProps) {
  const access = useAccess(org);
  return (
    <header className="mb-2 flex flex-wrap items-center justify-between gap-4">
      <div className="flex min-w-0 items-center gap-4">
        <SiteMark site={data?.site?.key ?? name} size={32} label={false} />
        <h1 className="truncate text-24 font-semibold">
          {workspace ? <span className="text-faint">{workspace.name} · </span> : null}
          {data ? accountName(data) : laneName(name)}
        </h1>
        {data && freeze.covering ? (
          <StateBadge state="stuck" label={FREEZE_WORDS.frozen.word} sentence={frozenSentence(freeze.covering)} className="shrink-0" />
        ) : data ? (
          <ConditionBadge condition={accountCondition(data)} />
        ) : null}
      </div>
      {data ? (
        <div className="flex w-full flex-wrap items-center gap-2 sm:w-auto">
          <Button icon={<PanelRight className="size-4" />} onClick={onInspect}>
            Technical details
          </Button>
          {access.can('freeze.account') && freeze.freezable && !freeze.covering ? <FreezeButton org={org} scope="lane" target={data.id} name={freeze.accountTitle} /> : null}
          {access.can('lanes.operate') && awake ? (
            <Button icon={<Power className="size-4" />} loading={action.isPending} onClick={() => action.mutate('close')}>
              Put to sleep
            </Button>
          ) : null}
        </div>
      ) : null}
    </header>
  );
}

interface LaneBodyProps {
  org: string;
  lane: Lane;
  awake: boolean;
  action: ReturnType<typeof useLaneAction>;
  full: ReturnType<typeof useFullWindow>;
  covering: FreezeRecord | null;
  coveringName: string;
}

/** Everything under the header: the notices, the live view and the sections about the account. */
function LaneBody({ org, lane: data, awake, action, full, covering, coveringName }: LaneBodyProps) {
  const [expanded, setExpanded] = full;
  const access = useAccess(org);
  const canOperate = access.can('lanes.operate');
  const canTakeOver = access.can('lanes.control');
  const help = holdNotice(data, canTakeOver);
  return (
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
      <WaitingSection org={org} lane={data} />
      <WorkerOnAccount org={org} lane={data.id} />
      <LaneActivity org={org} lane={data} />
      <AccountSettings org={org} lane={data} />
    </div>
  );
}
