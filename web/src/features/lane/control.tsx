/**
 * Who is driving this lane. The bar is the one place that answers it, so it must read at a
 * glance: a hairline bar means the agent, a solid bar and a heavier frame mean a person.
 *
 * Nothing here is optimistic. Saying "you have control" before the server agrees could have
 * someone act while the agent is still running, so the bar only changes once it is true.
 */
import { Bot, Hand, OctagonPause } from 'lucide-react';
import { useState } from 'react';
import { useLaneControl, useSession } from '@/api/hooks';
import type { ControlAction, ControlRequest, Lane, LaneControl } from '@/api/types';
import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/dialog';
import { Kbd } from '@/components/ui/misc';
import { StatusDot } from '@/components/ui/status';
import { cn } from '@/lib/cn';
import { describeError } from '@/lib/errors';
import { timeAgo } from '@/lib/format';
import { useKeys } from '@/lib/keyboard';
import { laneName } from '@/lib/labels';
import { principalName } from '@/lib/principal';
import { useNow } from '@/lib/use-now';
import { InlineError } from '@/components/ui/states';

/** The core's default `control_idle_seconds`. Control goes back to agents after this. */
const IDLE_SECONDS = 600;
const WARN_SECONDS = 60;

function secondsLeft(control: LaneControl, now: number): number {
  const from = control.last_human_input ?? control.since;
  if (!from) return IDLE_SECONDS;
  return Math.min(IDLE_SECONDS, Math.max(0, Math.round(IDLE_SECONDS - (now - new Date(from).getTime()) / 1000)));
}

const clock = (seconds: number) => `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;

/**
 * Is this person the holder? By user id only: a display name can be anyone's (security review 7).
 * The API always sends `by_id`, and the relay admits control by the same id.
 */
export function holdsLane(control: LaneControl, user: { id: string } | undefined): boolean {
  if (!user || control.state === 'agent') return false;
  return control.by_id != null && control.by_id === user.id;
}

export function useHolder(control: LaneControl) {
  const mine = holdsLane(control, useSession().data?.user);
  return { mine, name: mine ? 'You' : (control.by ?? 'Someone') };
}

/** Signals a person clears (a robot check, a sign-in, a checkpoint): handing back does not make them go away. */
const CHALLENGE_SIGNALS: ReadonlySet<Lane['signal']> = new Set(['captcha', 'checkpoint', 'logged_out', 'blocked']);

/** A check still holds the lane for a person: a `needs_human` hold, and the last page shows the check. */
export function challengeHeld(lane: Pick<Lane, 'hold' | 'signal'>): boolean {
  return (lane.hold ?? '').startsWith('needs_human') && CHALLENGE_SIGNALS.has(lane.signal);
}

interface BarProps {
  lane: Lane;
  /** Takes the role and the lane's state into account. A viewer never sees a control. */
  canControl: boolean;
}

/**
 * Hand back. A check that still holds the lane asks first: "Not done yet" gives the lane back to agents
 * but keeps the hold, so they stay off until the check is done. Nothing is cleared before the person says so.
 */
function HandBack({ challenged, pending, onHandBack }: { challenged: boolean; pending: ControlRequest | null; onHandBack: (done?: boolean) => void }) {
  if (!challenged) {
    return (
      <Button size="sm" variant="primary" className="max-sm:h-10 max-sm:flex-1" loading={pending?.action === 'hand_back'} disabled={pending !== null} onClick={() => onHandBack()}>
        Hand back
      </Button>
    );
  }
  return (
    <>
      <Button size="sm" className="max-sm:h-10 max-sm:flex-1" loading={pending?.action === 'hand_back' && pending.done === false} disabled={pending !== null} onClick={() => onHandBack(false)}>
        Not done yet
      </Button>
      <Button size="sm" variant="primary" className="max-sm:h-10 max-sm:flex-1" loading={pending?.action === 'hand_back' && pending.done === true} disabled={pending !== null} onClick={() => onHandBack(true)}>
        Done, hand back
      </Button>
    </>
  );
}

function Takeback({ lane, canControl, pending, onDeny, onHandBack }: BarProps & { pending: ControlRequest | null; onDeny: () => void; onHandBack: (done?: boolean) => void }) {
  const { control } = lane;
  return (
    <div role="status" className="flex animate-fade-in flex-wrap items-center gap-x-4 gap-y-2 border-b border-line bg-inset px-4 py-3">
      <p className="flex min-w-0 flex-1 basis-64 items-baseline gap-2 text-body">
        <StatusDot tone="warn" className="translate-y-px" />
        <span className="min-w-0">
          <span className="font-medium">
            {control.takeback_by ? principalName(control.takeback_by) : 'An agent'} is asking for control back.
          </span>{' '}
          {control.takeback_reason ? <span className="text-muted">{control.takeback_reason}</span> : null}
        </span>
      </p>
      {canControl ? (
        <div className="flex w-full flex-wrap gap-2 sm:w-auto">
          <Button size="sm" className="max-sm:h-10 max-sm:flex-1" loading={pending?.action === 'deny_request'} disabled={pending !== null} onClick={onDeny}>
            Keep control
          </Button>
          <HandBack challenged={challengeHeld(lane)} pending={pending} onHandBack={onHandBack} />
        </div>
      ) : null}
    </div>
  );
}

interface ControlProps extends BarProps {
  org: string;
  /** The live view fills the window: on a phone that is the only way to work in it. */
  expanded?: boolean;
  children: React.ReactNode;
}

/** The live view with its control bar. The frame itself changes with who holds the lane. */
export function ControlFrame({ org, lane, canControl, expanded = false, children }: ControlProps) {
  const mutation = useLaneControl(org, lane.workspace, lane.name);
  const { control } = lane;
  const person = control.state === 'human';
  const paused = control.state === 'paused';
  const holder = useHolder(control);
  const now = useNow(person);
  const left = secondsLeft(control, now);
  const [stopping, setStopping] = useState(false);
  const pending = mutation.isPending ? (mutation.variables ?? null) : null;
  const pendingAction = pending?.action ?? null;
  const name = laneName(lane.name);
  const challenged = challengeHeld(lane);

  const run = (request: ControlRequest) => {
    if (!mutation.isPending) mutation.mutate(request);
  };
  // A paused or held agent is taken over first, to do the check. A person holding the lane hands it back,
  // but a held one answers the question instead (the T key does not answer it for them).
  const primary: ControlAction = person ? 'hand_back' : 'take';

  useKeys({ t: () => run({ action: primary }) }, canControl && !(person && challenged));

  const held = paused && challenged;
  const title = person ? `${holder.name} ${holder.mine ? 'have' : 'has'} control` : held ? 'Held for a check' : paused ? 'Agent stopped' : 'Agent has control';
  const detail = person
    ? challenged
      ? 'Agents stay off until the check is done'
      : left <= WARN_SECONDS
        ? `Returns to agents in ${clock(left)} unless this account is used`
        : `Agents are held off. Returns to them in ${clock(left)} with no input`
    : held
      ? 'Agents stay off until the check is done. Take over to do it'
      : paused
        ? `By ${holder.name === 'You' ? 'you' : holder.name}${control.since ? `, ${timeAgo(control.since).toLowerCase()}` : ''}. It will not act here until you hand back`
        : lane.state === 'active'
          ? 'Working now'
          : 'Waiting for its next step';
  const Icon = person ? Hand : paused ? OctagonPause : Bot;

  return (
    <section
      aria-label={`Control of ${name}`}
      data-control={control.state}
      className={cn(
        // A second pixel of frame from the outline, so nothing moves when it appears.
        'overflow-hidden rounded-xl border bg-card outline outline-transparent transition-[outline-color,border-color] duration-(--duration-slow) ease-standard',
        person ? 'border-fg outline-1 outline-fg' : 'border-line',
        // Above the top bar and the tab bar, below dialogs, so Stop agent still confirms.
        expanded && 'safe-bottom fixed inset-0 z-[35] flex flex-col rounded-none -outline-offset-2',
      )}
    >
      <div
        className={cn(
          'flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-2 transition-colors duration-(--duration-slow) ease-standard sm:min-h-12',
          person ? 'bg-primary text-on-primary' : 'border-b border-line bg-inset',
        )}
      >
        <div key={control.state} className="flex min-w-0 flex-1 basis-56 animate-fade-in items-center gap-2">
          <Icon className={cn('size-4', !person && 'text-faint')} />
          <div role="status" className="min-w-0">
            <p className="truncate text-label">{title}</p>
            <p className={cn('tabular text-caption', person ? 'opacity-80' : 'text-faint')}>{detail}</p>
          </div>
        </div>
        {canControl ? (
          <div className="flex w-full items-center gap-2 sm:w-auto">
            {!person && !paused ? (
              <Button
                size="sm"
                variant="danger"
                className="max-sm:h-10 max-sm:flex-1"
                disabled={pending !== null}
                onClick={() => setStopping(true)}
              >
                Stop agent
              </Button>
            ) : null}
            {/* One button for both, so keyboard focus survives the change of state. The banner
                below carries the same answer when an agent is asking, so it is not shown twice. */}
            {control.takeback_requested ? null : person && challenged ? (
              <HandBack challenged pending={pending} onHandBack={(done) => run({ action: 'hand_back', done })} />
            ) : (
            <Button
              size="sm"
              variant={person ? 'inverse' : 'primary'}
              className="max-sm:h-10 max-sm:flex-1"
              loading={pendingAction === 'take' || pendingAction === 'hand_back'}
              // Its own request makes it busy, which keeps keyboard focus on it. Only another
              // action (Stop agent, answered in its own dialog) disables it outright.
              disabled={pendingAction === 'stop_agent' || pendingAction === 'deny_request'}
              onClick={() => run({ action: primary })}
            >
              {pendingAction === 'take' ? 'Taking control' : pendingAction === 'hand_back' ? 'Handing back' : primary === 'take' ? 'Take over' : 'Hand back'}
              <Kbd aria-hidden="true" className="hidden border-transparent bg-transparent text-current opacity-60 lg:inline-flex">
                T
              </Kbd>
            </Button>
            )}
          </div>
        ) : null}
      </div>
      {control.takeback_requested ? (
        <Takeback lane={lane} canControl={canControl} pending={pending} onDeny={() => run({ action: 'deny_request' })} onHandBack={(done) => run({ action: 'hand_back', done })} />
      ) : null}
      {mutation.isError ? (
        <InlineError className="border-b border-line px-4 py-2">
          {describeError(mutation.error)}
        </InlineError>
      ) : null}
      {children}
      <ConfirmDialog
        open={stopping}
        onOpenChange={setStopping}
        title={`Stop the agent on ${name}?`}
        description="It stops now. Any run or brain task on this account is cancelled, and it will not act here until you hand back. Anything it already published stays published."
        confirmLabel="Stop agent"
        danger
        loading={pendingAction === 'stop_agent'}
        onConfirm={() => mutation.mutate({ action: 'stop_agent' }, { onSettled: () => setStopping(false) })}
      />
    </section>
  );
}
