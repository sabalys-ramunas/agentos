import { Link, useParams } from '@tanstack/react-router';
import { Moon, PanelRight, SearchX } from 'lucide-react';
import { useState } from 'react';
import { WorkerOnAccount } from '@/features/workers/in-place';
import { useAccess, useFreezeStatus, useLane, useLaneAction, useLaneQueue, useWorkspaces } from '@/api/hooks';
import { ApiError } from '@/api/http';
import type { FreezeRecord, FreezeStatus, Lane, Workspace } from '@/api/types';
import { SiteMark } from '@/components/brand/site-mark';
import { usePageBand } from '@/components/shell/page-band';
import { usePageCrumb } from '@/components/shell/crumb';
import { Button, IconButton } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { StateBadge } from '@/components/ui/state-badge';
import { PageHeader } from '@/components/ui/misc';
import { Banner, EmptyState, ErrorState } from '@/components/ui/states';
import { useMinWidth } from '@/lib/use-min-width';
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
import { HistoryPanel, WhoPanel } from './account-panels';
import { TaintBanner } from './account-people';
import { holderName } from './account-words';
import { AccountSettings, Inspect, LaneSkeleton } from './lane-parts';
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

/** The agent that holds the account now, by name ("Claude Code"), for the pill. Never a guess: null when no agent holds it. */
function useHolderAgent(org: string, lane: Lane | undefined): string | null {
  const queue = useLaneQueue(org, lane?.workspace ?? '', lane?.name ?? '').data;
  const claim = queue?.claim;
  if (!lane || !claim || claim.yours || claim.kind !== 'agent') return null;
  return holderName(claim.holder, claim.kind);
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
  usePageCrumb(data ? `${workspace ? `${workspace.name} · ` : ''}${accountName(data)}` : undefined, { mono: false });
  const band = usePageBand<HTMLDivElement>(!lane.isError);

  if (lane.isError) {
    return (
      <Card>
        {lane.error instanceof ApiError && lane.error.status === 404 ? (
          <EmptyState
            heading="h2"
            icon={<SearchX />}
            title="There is no account by that name here"
            description={workspace ? `${workspace.name} has no account called ${laneName(name)}. It may have been renamed or removed.` : 'It may have been renamed or removed. Accounts lists every account on this computer.'}
            action={
              <Button asChild>
                <Link to="/$org/accounts" params={{ org }} search={workspace ? { workspace: ws } : {}}>
                  Back to Accounts
                </Link>
              </Button>
            }
          />
        ) : (
          <ErrorState error={lane.error} onRetry={() => void lane.refetch()} />
        )}
      </Card>
    );
  }
  return (
    <div className="grid gap-6">
      <div ref={band} className="on-night grid min-w-0 gap-6">
        <LaneHeader org={org} name={name} lane={data} workspace={workspace} awake={awake} action={action} freeze={freeze} onInspect={() => setInspect(true)} />
        {data ? <LaneBand org={org} lane={data} awake={awake} action={action} full={full} covering={freeze.covering} coveringName={freeze.coveringName} /> : <LaneSkeleton />}
      </div>
      {data ? <LaneBelow org={org} lane={data} /> : null}
      {data ? <Inspect lane={data} open={inspect} onOpenChange={setInspect} /> : null}
    </div>
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

/** The site's tile, "Aster Coffee · Instagram" and its state, one line of facts, and Put to sleep and Freeze on glass. */
function LaneHeader({ org, name, lane: data, workspace, awake, action, freeze, onInspect }: LaneHeaderProps) {
  const access = useAccess(org);
  const condition = data ? accountCondition(data) : null;
  return (
    <PageHeader
      className="mb-0"
      lead={<SiteMark site={data?.site?.key ?? name} size={40} tile label={false} className="rounded-lg border-transparent bg-card shadow-lift" />}
      title={`${workspace ? `${workspace.name} · ` : ''}${data ? accountName(data) : laneName(name)}`}
      aside={
        data && freeze.covering ? (
          <StateBadge chip state="stuck" label={FREEZE_WORDS.frozen.word} sentence={frozenSentence(freeze.covering)} />
        ) : condition ? (
          <StateBadge chip state={condition.state} label={condition.label} sentence={condition.sentence} />
        ) : null
      }
      description={
        data ? (
          <span className="flex min-w-0 flex-wrap items-baseline">
            <span className="whitespace-nowrap">{presetLabel[data.preset]?.name ?? data.preset}</span>
            <Fact>Runs on {computerName(data.node)}</Fact>
            <Fact>{data.last_used ? `Last used ${timeAgo(data.last_used).toLowerCase()}` : 'Not used yet'}</Fact>
          </span>
        ) : (
          // holds the line while the account is read, so nothing moves when it arrives
          '\u00a0'
        )
      }
      actions={
        data ? (
          <>
            {access.can('lanes.operate') && awake ? (
              <Button variant="glass" icon={<Moon className="size-4" />} loading={action.isPending} onClick={() => action.mutate('close')}>
                Put to sleep
              </Button>
            ) : null}
            {access.can('freeze.account') && freeze.freezable && !freeze.covering ? <FreezeButton org={org} scope="lane" target={data.id} name={freeze.accountTitle} /> : null}
            <IconButton variant="glass" size="lg" label="Technical details" onClick={onInspect}>
              <PanelRight className="size-4" />
            </IconButton>
          </>
        ) : null
      }
    />
  );
}

/** One fact after the first, with its dot, kept on one line. */
function Fact({ children }: { children: React.ReactNode }) {
  return (
    <span className="whitespace-nowrap">
      <span aria-hidden="true" className="px-2 text-faint">
        ·
      </span>
      {children}
    </span>
  );
}

interface LaneBandProps {
  org: string;
  lane: Lane;
  awake: boolean;
  action: ReturnType<typeof useLaneAction>;
  full: ReturnType<typeof useFullWindow>;
  covering: FreezeRecord | null;
  coveringName: string;
}

/** On the band: what holds it (a freeze, a check, a hold, outside text), the live screen with the pill, and the panels beside it. */
function LaneBand({ org, lane: data, awake, action, full, covering, coveringName }: LaneBandProps) {
  const [expanded, setExpanded] = full;
  const access = useAccess(org);
  const canOperate = access.can('lanes.operate');
  const canTakeOver = access.can('lanes.control');
  const help = holdNotice(data, canTakeOver);
  const agent = useHolderAgent(org, data);
  const wide = useMinWidth(1024);
  return (
    <>
      <MissingLane org={org} lane={data} />
      {covering ? <FrozenNotice org={org} record={covering} name={coveringName} canLift={access.can('freeze.lift')} /> : null}
      {help ? (
        <Banner tone={accountCondition(data).state === 'stuck' ? 'bad' : 'warn'} title={`${help.title}.`}>
          {help.body}
        </Banner>
      ) : null}
      <TaintBanner org={org} lane={data} />
      <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_312px]">
        <div className="min-w-0">
          {awake || data.control.state !== 'agent' ? (
            <ControlFrame org={org} lane={data} canControl={canTakeOver && data.kind !== 'service'} expanded={expanded && awake} onExpandedChange={setExpanded} stage agent={agent}>
              <LiveView
                org={org}
                lane={data}
                opening={action.isPending}
                canOperate={canOperate}
                onOpen={() => action.mutate('open')}
                expanded={expanded && awake}
                onExpandedChange={setExpanded}
                stage
                agent={agent}
              />
            </ControlFrame>
          ) : (
            <LiveView org={org} lane={data} opening={action.isPending} canOperate={canOperate} onOpen={() => action.mutate('open')} stage />
          )}
        </div>
        {wide ? (
          <div className="grid min-w-0 gap-3">
            <WhoPanel org={org} lane={data} glass />
            <HistoryPanel org={org} lane={data} glass />
            <SafeLine org={org} lane={data} glass />
          </div>
        ) : null}
      </div>
    </>
  );
}

/** Under the band: the panels as cards on a narrow screen, the worker's strip, and the settings behind one disclosure. */
function LaneBelow({ org, lane }: { org: string; lane: Lane }) {
  const wide = useMinWidth(1024);
  return (
    <>
      {wide ? null : (
        <div className="grid items-start gap-4 sm:grid-cols-2">
          <WhoPanel org={org} lane={lane} glass={false} />
          <HistoryPanel org={org} lane={lane} glass={false} />
          <SafeLine org={org} lane={lane} />
        </div>
      )}
      <WorkerOnAccount org={org} lane={lane.id} />
      <AccountSettings org={org} lane={lane} />
    </>
  );
}
