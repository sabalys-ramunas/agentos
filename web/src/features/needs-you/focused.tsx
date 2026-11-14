import type { Approval } from '@/api/types';
import { KindEyebrow } from '@/components/ui/kind';
import { Status } from '@/components/ui/status';
import { needsProof } from '@/lib/confirm';
import { DecisionButtons, DecisionCard, DoneState, TechnicalDetails, useShown } from './decision-card';
import { useNarrow } from './use-narrow';
import { doingOf } from './words';
import { kindLabel, tileOf } from './if-yes';
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
  return (
    <div className="grid">
      <header className="flex min-h-13 flex-wrap items-center gap-3 border-b border-line px-5 py-2">
        <KindEyebrow kind={tileOf(approval)} label={kindLabel(approval)} />
        <span className="ml-auto flex items-center gap-3">
          {done ? null : <Status status={{ label: 'Waiting', tone: 'warn' }} />}
          <span className="text-caption text-faint">{waitedShort(approval.created_at)} ago</span>
        </span>
      </header>
      <div className="p-4 sm:p-5 lg:p-6">
        <DecisionCard
          org={org}
          approval={approval}
          shown={shown}
          color={color}
          layout={narrow ? 'stack' : 'split'}
          stickyActions={narrow}
          done={done}
          changed={changed}
          actions={actions}
          technical={<TechnicalDetails org={org} approval={approval} open={techOpen} onToggle={onToggleTech} allowAgain={allowAgain} />}
        />
      </div>
    </div>
  );
}
