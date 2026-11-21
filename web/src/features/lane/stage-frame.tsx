/**
 * An environment's live screen on the night band (web/DESIGN.md, "The lane live view", "The night band"): the
 * screen as a picture, and under it one row: the control pill, which says who has control by name with the one
 * thing to do (Take over, Hand back), then the screen's own tools, full window, and the ⋯ menu that holds the
 * rare and final Stop agent (which asks first). The frame never says "you have control" before the node agrees.
 */
import { Expand, Minimize, MoreHorizontal } from 'lucide-react';
import { useState } from 'react';
import type { ReactNode } from 'react';
import { Button, IconButton } from '@/components/ui/button';
import { Menu, MenuContent, MenuItem, MenuTrigger } from '@/components/ui/menu';
import { cn } from '@/lib/cn';
import { StageToolsSlot } from './live/stage-slot';

export interface StageFrameProps {
  name: string;
  state: 'agent' | 'human' | 'paused';
  /** The agent's name, or "Agent". */
  agent: string;
  /** Who holds it while a person does: "You" or their name. */
  holder: string;
  mine: boolean;
  /** "19:22", when a person took control or stopped the agent. */
  since: string | null;
  /** Held for a check (a sign-in, a robot check): agents stay off until it is done. */
  held: boolean;
  /** The words that need saying under the name, if any ("Returns to agents in 0:45"). */
  note: string | null;
  noteTone?: 'warn';
  canControl: boolean;
  /** The main button (Take over, Hand back, or the two buttons of a check), or null. */
  action: ReactNode;
  onStop: (() => void) | null;
  expanded: boolean;
  onExpandedChange?: (expanded: boolean) => void;
  /** The banner over the screen (an agent asks for control back) and a failed request. */
  notices: ReactNode;
  dialogs: ReactNode;
  children: ReactNode;
}

/** The agent's violet dot; white while a person has control; amber while the agent is stopped or held. */
function Dot({ state }: { state: StageFrameProps['state'] }) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        'size-2 shrink-0 rounded-full ring-3',
        state === 'human' ? 'bg-side ring-night-ring' : state === 'paused' ? 'bg-warn ring-warn/25' : 'bg-agent ring-agent/30',
      )}
    />
  );
}

function PillWords({ props }: { props: StageFrameProps }) {
  const { state, agent, holder, mine, since, held, note, noteTone, canControl } = props;
  if (held) return <span className="font-semibold text-fg">Held for a check</span>;
  const who = state === 'human' ? holder : agent;
  const verb = state === 'paused' ? ' stopped' : state === 'human' && mine ? ' have control' : ' has control';
  return (
    <>
      <span className="font-semibold text-fg">{who}</span>
      {verb}
      {note ? (
        <span className={cn('tabular', noteTone === 'warn' ? 'text-warn' : 'text-faint')}> · {note}</span>
      ) : since && state !== 'agent' ? (
        // on a phone the pill keeps "You have control" whole, and leaves the time out
        <span className="tabular text-faint max-sm:hidden"> since {since}</span>
      ) : !canControl && state === 'agent' ? (
        <span className="text-faint"> · you can watch</span>
      ) : null}
    </>
  );
}

export function StageFrame(props: StageFrameProps) {
  const { name, state, action, onStop, expanded, onExpandedChange, notices, dialogs, children } = props;
  const [slot, setSlot] = useState<HTMLElement | null>(null);
  return (
    <section
      aria-label={`Control of ${name}`}
      data-control={state}
      data-stage=""
      className={cn('flex flex-col gap-4', expanded && 'safe-bottom on-night fixed inset-0 z-40 bg-stage p-3 sm:p-4')}
    >
      {notices}
      <StageToolsSlot.Provider value={slot}>
        <div className={cn('min-h-0', expanded && 'flex flex-1 flex-col')}>{children}</div>
      </StageToolsSlot.Provider>
      <div className="flex items-center justify-center gap-2">
        <div className="control-pill flex h-12 min-w-0 items-center gap-3 rounded-full py-1 pr-1 pl-5 max-sm:flex-1 max-sm:gap-2 max-sm:pl-4">
          <Dot state={state} />
          {/* Who has control is said once, here, and read out when it changes. */}
          <p role="status" className="min-w-0 flex-1 truncate text-body text-muted">
            <PillWords props={props} />
          </p>
          {action ? <div className="flex shrink-0 items-center gap-2">{action}</div> : null}
        </div>
        <span ref={setSlot} className="contents" />
        {onExpandedChange ? (
          // On a phone, where the pill needs the room, it joins Stop agent in the ⋯ menu when there is one.
          <IconButton
            variant="glass"
            size="lg"
            label={expanded ? 'Leave full window' : 'Full window'}
            className={cn(onStop && 'max-sm:hidden')}
            onClick={() => onExpandedChange(!expanded)}
          >
            {expanded ? <Minimize className="size-4" /> : <Expand className="size-4" />}
          </IconButton>
        ) : null}
        {onStop ? (
          <Menu>
            <MenuTrigger asChild>
              <IconButton variant="glass" size="lg" label="More for this screen">
                <MoreHorizontal className="size-4" />
              </IconButton>
            </MenuTrigger>
            <MenuContent align="end" side="top" aria-label="More for this screen">
              {onExpandedChange ? (
                <MenuItem onSelect={() => onExpandedChange(!expanded)} className="sm:hidden">
                  {expanded ? 'Leave full window' : 'Full window'}
                </MenuItem>
              ) : null}
              <MenuItem onSelect={onStop} className="text-bad">
                Stop agent
              </MenuItem>
            </MenuContent>
          </Menu>
        ) : null}
      </div>
      {dialogs}
    </section>
  );
}

/** The pill's own button: white on the night, and busy while its request runs, so focus stays on it. */
export function PillButton({ children, pending, disabled, onClick, shortcut }: { children: ReactNode; pending: boolean; disabled?: boolean; onClick: () => void; shortcut?: string }) {
  return (
    <Button variant="light" className="max-sm:h-10" loading={pending} disabled={disabled} onClick={onClick} shortcut={shortcut}>
      {children}
    </Button>
  );
}

