/** An account's live screen: the browser or terminal, how it opens, and what it says while it cannot show. */
import { Cog, Eye, Moon, SquareTerminal, TriangleAlert } from 'lucide-react';
import { lazy, Suspense } from 'react';
import { useAccess, useLane, useNodes } from '@/api/hooks';
import { ApiError } from '@/api/http';
import type { Lane } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Spinner } from '@/components/ui/spinner';
import { cn } from '@/lib/cn';
import { STATE_WORDS } from '@/lib/states';
import { useHolder } from './control';

function Stage({ children, framed = false }: { children: React.ReactNode; framed?: boolean }) {
  return (
    <div className={cn('flex aspect-[16/10] flex-col items-center justify-center bg-inset p-6 text-center', framed && 'rounded-lg border border-line')}>
      {children}
    </div>
  );
}

const VncScreen = lazy(() => import('./live/vnc-screen'));
const TerminalScreen = lazy(() => import('./live/terminal-screen'));

/** Shown while the viewer's code loads, shaped like the connecting state so nothing jumps. */
function ScreenLoading() {
  return (
    <div className="flex flex-col">
      <Stage>
        <Spinner className="mb-4 size-6 text-faint" />
        <h2 className="text-label">Connecting</h2>
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

/**
 * What shows instead of the screen while the lane is asleep, starting or switched off; null when the screen
 * shows. An environment's desktop outlives its browser: its screen shows while the browser is cold or starting.
 */
function resting({
  lane,
  stage,
  opening,
  canOperate,
  onOpen,
}: {
  lane: Lane;
  stage: boolean;
  opening: boolean;
  canOperate: boolean;
  onOpen: () => void;
}) {
  const screenUp = stage && Boolean(lane.viewer) && lane.state !== 'quarantined';
  if (lane.state === 'cold' && !screenUp) {
    return (
      <Stage framed={stage}>
        <Moon className="mb-4 size-6 text-faint" />
        <h2 className="text-label">This account is {STATE_WORDS.sleeping.toLowerCase()}</h2>
        <p className="mt-1 max-w-xs text-body text-muted">
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
  if (lane.state === 'warming' && !screenUp) {
    return (
      <Stage framed={stage}>
        <Spinner className="mb-4 size-6 text-faint" />
        <h2 className="text-label">Warming up</h2>
        <p className="mt-1 text-body text-muted">Starting the display and running the self-check.</p>
      </Stage>
    );
  }
  if (lane.state === 'quarantined') {
    return (
      <Stage framed={stage}>
        <TriangleAlert className="mb-4 size-6 text-faint" />
        <h2 className="text-label">This account is switched off for safety</h2>
        <p className="mt-1 max-w-sm text-body text-muted">{lane.quarantine_reason ?? 'It failed its own safety check, and does nothing until someone turns it back on.'}</p>
        {canOperate ? (
          <Button className="mt-4" loading={opening} onClick={onOpen}>
            Turn it back on
          </Button>
        ) : null}
      </Stage>
    );
  }
  return null;
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
  stage = false,
  agent = null,
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
  /** The screen as a picture on a stage, as an environment's browser shows it. */
  stage?: boolean;
  /** The agent's name, for its pointer over the picture. */
  agent?: string | null;
}) {
  const holder = useHolder(lane.control);
  const probe = useProbe(org, lane);
  const access = useAccess(org);
  const canWatch = access.can('lanes.watch');
  // Take over, hand back and stop an agent are the owner's alone: an operator watches and works the account.
  const canControl = (canTake ?? access.can('lanes.control')) && lane.kind !== 'service';
  const person = lane.control.state === 'human';
  const restingStage = resting({ lane, stage, opening, canOperate, onOpen });
  if (restingStage) return restingStage;
  if (lane.kind === 'service') {
    return (
      <Stage framed={stage}>
        <Cog className="mb-4 size-6 text-faint" />
        <h2 className="text-label">This tool runs a service</h2>
        <p className="mt-1 max-w-sm text-body text-muted">It has no screen or shell to show. What it does appears in History below.</p>
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
    stage,
    probe,
    agent,
  };
  if (lane.kind === 'terminal') {
    if (!canControl) {
      return (
        <Stage framed={stage}>
          <SquareTerminal className="mb-4 size-6 text-faint" />
          <h2 className="text-label">A shell on this account</h2>
          <p className="mt-1 max-w-sm text-body text-muted">Only the owner of this computer can open it here.</p>
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
      <Stage framed={stage}>
        <Eye className="mb-4 size-6 text-faint" />
        <h2 className="text-label">Live view is for members</h2>
        <p className="mt-1 max-w-sm text-body text-muted">It shows whatever the account shows (messages, settings, one-time codes), so it is for members who operate this account.</p>
      </Stage>
    );
  }
  return (
    <Suspense fallback={<ScreenLoading />}>
      <VncScreen {...props} />
    </Suspense>
  );
}
