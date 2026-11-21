/**
 * The screen of an environment on the night band (web/DESIGN.md, "One environment"). While the machine runs and
 * the node has a view of its screen, the live view with the control pill under it (take over, hand back, full
 * window, and Stop agent in the ⋯ menu). Otherwise the screen's picture drawn from the state, with one line in
 * the pill's place that says what is happening: starting, no view yet, the view could not be read, closed.
 * When the node kept frames of its browser, the replay (openajar.environments.replay): a closed one leads with it
 * in the screen's place, and an open one switches between Live and Replay so far above the screen.
 */
import { useState } from 'react';
import type { ReactNode } from 'react';
import { useAccess, useLane, useLaneAction } from '@/api/hooks';
import { ApiError } from '@/api/http';
import type { EnvDetail, Lane } from '@/api/types';
import { ScreenPicture } from '@/components/screen-picture';
import { Card } from '@/components/ui/card';
import { SegmentedControl } from '@/components/ui/tabs';
import { Skeleton } from '@/components/ui/states';
import { StatusDot } from '@/components/ui/status';
import { timeAgo } from '@/lib/format';
import { ControlFrame } from '@/features/lane/control';
import { LiveView } from '@/features/lane/live-view';
import { agentSaid } from '@/lib/agent-names';
import { PICTURE } from './env-card';
import { ReplayCard } from './replay';
import { closedReason, continueLine, goneWords, isOpen, keepOf } from './words';

/** The browser lane of an environment, read while it has a live view (the page and its header share the read). */
export function useEnvLane(org: string, env: EnvDetail) {
  const [ws = 'env', name = ''] = env.lanes.browser.split('/');
  return useLane(org, ws, name, Boolean(env.viewer) && isOpen(env));
}

/** A drawn picture where the live screen would be, and one line under it in the pill's place. */
function Still({ env, children, tone, after }: { env: EnvDetail; children: ReactNode; tone?: 'bad' | 'idle' | 'live'; after?: ReactNode }) {
  return (
    <div className="grid gap-4">
      <ScreenPicture state={PICTURE[env.state]} title={env.title} className="stage-screen rounded-lg" />
      <div className="flex justify-center">
        <p role="status" className="control-pill flex min-h-12 max-w-full items-center gap-3 rounded-full px-5 py-2 text-body text-muted">
          {tone ? <StatusDot tone={tone} starting={env.state === 'starting'} /> : null}
          <span className="min-w-0">{children}</span>
        </p>
      </div>
      {after ? <p className="mx-auto max-w-[46em] text-center text-body text-pretty text-muted">{after}</p> : null}
    </div>
  );
}

/** The replay of its browser on the band, in a card the size of the screen's place. */
function StageReplay({ org, env }: { org: string; env: EnvDetail }) {
  return (
    <Card className="overflow-hidden">
      <ReplayCard org={org} env={env} embedded />
    </Card>
  );
}

export function EnvStage({ org, env }: { org: string; env: EnvDetail }) {
  const [view, setView] = useState<'live' | 'replay'>('live');
  const replayable = env.state !== 'starting' && Boolean(env.replay?.frames);
  if (!isOpen(env)) {
    const failed = env.state === 'failed';
    const lead = failed ? 'It did not start.' : env.closed_at ? `Closed ${timeAgo(env.closed_at).toLowerCase()}.` : 'Closed.';
    const line = (
      <>
        <span className="font-semibold text-fg">{lead}</span> {closedReason(env.closed_reason, env.state)}
        {keepOf(env) === 'save' && !failed ? (
          <span className="text-faint"> Its work is saved: resume it below, or an agent brings it back when you say "{continueLine(env.title)}".</span>
        ) : null}
      </>
    );
    if (replayable) {
      return (
        <div className="grid gap-4">
          <StageReplay org={org} env={env} />
          <p role="status" className="text-center text-body text-muted">
            {line}
          </p>
        </div>
      );
    }
    return (
      <Still env={env} tone={failed ? 'bad' : 'idle'}>
        {line}
      </Still>
    );
  }
  if (replayable) {
    return (
      <div className="grid gap-3">
        <SegmentedControl<'live' | 'replay'>
          label="Screen view"
          value={view}
          onValueChange={setView}
          options={[
            { value: 'live', label: 'Live' },
            { value: 'replay', label: 'Replay so far' },
          ]}
          className="justify-self-end"
        />
        {view === 'replay' ? <StageReplay org={org} env={env} /> : <OpenStage org={org} env={env} />}
      </div>
    );
  }
  return <OpenStage org={org} env={env} />;
}

function OpenStage({ org, env }: { org: string; env: EnvDetail }) {
  if (env.state === 'gone') {
    const words = goneWords(env);
    return (
      <Still env={env} tone="bad" after={words.then}>
        <span className="font-semibold text-fg">{words.lead}</span>
        {words.why ? ` ${words.why}` : null}
      </Still>
    );
  }
  if (env.state === 'starting') {
    return (
      <Still env={env} tone="live">
        <span className="font-semibold text-fg">Starting its computer.</span> Its screen shows here when it is ready, usually within two minutes.
      </Still>
    );
  }
  if (!env.viewer) {
    return (
      <Still env={env} tone="idle">
        <span className="font-semibold text-fg">No live view right now.</span> The screen shows here once the agent opens its browser.
      </Still>
    );
  }
  return <LiveStage org={org} env={env} />;
}

function LiveStage({ org, env }: { org: string; env: EnvDetail }) {
  const lane = useEnvLane(org, env);
  if (lane.isPending) return <Skeleton className="aspect-video h-auto w-full rounded-lg bg-side-hover" />;
  if (!lane.data) {
    return (
      <Still env={env} tone="idle">
        <span className="font-semibold text-fg">The screen cannot be shown.</span>{' '}
        {lane.error instanceof ApiError && lane.error.status === 404
          ? 'The node does not list it now. It may be closing with the machine.'
          : 'Its live view could not be read. It tries again on its own.'}
      </Still>
    );
  }
  return <LiveScreen org={org} env={env} lane={lane.data} />;
}

function LiveScreen({ org, env, lane }: { org: string; env: EnvDetail; lane: Lane }) {
  const action = useLaneAction(org, lane.workspace, lane.name);
  const access = useAccess(org);
  const [expanded, setExpanded] = useState(false);
  const canTake = access.can('lanes.control');
  const agent = agentSaid(env.agent);
  // the machine's desktop stays up while its browser sleeps, so the screen is awake whenever the node has a view
  const awake = lane.state === 'warm' || lane.state === 'active' || Boolean(lane.viewer);
  return (
    <ControlFrame org={org} lane={lane} canControl={canTake} expanded={expanded && awake} onExpandedChange={setExpanded} stage agent={agent}>
      <LiveView
        org={org}
        lane={lane}
        opening={action.isPending}
        canOperate={access.can('lanes.operate')}
        canTake={canTake}
        onOpen={() => action.mutate('open')}
        expanded={expanded && awake}
        onExpandedChange={setExpanded}
        stage
        agent={agent}
      />
    </ControlFrame>
  );
}
