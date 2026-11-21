import type { Approval } from '@/api/types';
import { Badge } from '@/components/ui/badge';
import { StatusDot } from '@/components/ui/status';
import { needsProof } from '@/lib/confirm';
import { DecisionButtons, DecisionCard, DoneState, TechnicalDetails, useShown } from './decision-card';
import { useNarrow } from './use-narrow';
import { doingOf } from './words';
import { stakeOf } from './kinds';
import { waitedShort } from './queue-words';

/** The item on screen: the card, its buttons or its done state, and the technical details. */
export function Focused({
  org,
  approval,
  business,
  color,
  done,
  armed,
  changed,
  canDecide,
  techOpen,
  onToggleTech,
  allowAgain,
  onYes,
  onNo,
}: {
  org: string;
  approval: Approval;
  business: string | null;
  color: string | null;
  done: 'approve' | 'deny' | null;
  armed: boolean;
  changed: boolean;
  canDecide: boolean;
  techOpen: boolean;
  onToggleTech: () => void;
  allowAgain: React.ReactNode;
  onYes: () => void;
  onNo: () => void;
}) {
  const shown = useShown(org, approval, business);
  const narrow = useNarrow();
  const words = doingOf(approval, shown.where);
  const actions = done ? (
    <DoneState approval={approval} decision={done} label={done === 'approve' ? words.doing : words.notDoing} />
  ) : canDecide ? (
    <DecisionButtons verbs={shown.verbs} armed={armed} who={shown.who} onYes={onYes} onNo={onNo} proof={needsProof(approval)} />
  ) : (
    <p className="text-secondary text-faint sm:text-right">You can see this, but your role cannot answer it.</p>
  );
  // The ask's state, as drawn: money and what can't be undone say so in amber; anything else waits for you.
  const stake = stakeOf(approval);
  const heavy = stake.key === 'money' || stake.key === 'final';
  const status = done ? null : (
    <Badge tone={heavy ? 'warn' : 'live'}>
      <StatusDot tone={heavy ? 'warn' : 'live'} />
      {heavy ? stake.name : 'Waiting for you'}
    </Badge>
  );
  return (
    <div className={narrow ? 'p-4 sm:p-5' : 'p-6'}>
      <DecisionCard
        org={org}
        approval={approval}
        shown={shown}
        color={color}
        layout={narrow ? 'stack' : 'split'}
        stickyActions={narrow}
        band={!narrow}
        status={
          <span className="flex items-center gap-3">
            <span className="text-caption text-faint max-sm:hidden">{waitedShort(approval.created_at)} ago</span>
            {status}
          </span>
        }
        done={done}
        changed={changed}
        actions={actions}
        technical={<TechnicalDetails org={org} approval={approval} open={techOpen} onToggle={onToggleTech} allowAgain={allowAgain} />}
      />
    </div>
  );
}
