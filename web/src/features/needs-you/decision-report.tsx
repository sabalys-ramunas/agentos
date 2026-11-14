import { X } from 'lucide-react';
import { useDecisionReports } from '@/api/hooks';
import type { DecisionReport } from '@/api/types';
import { Button, IconButton } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { StatusDot } from '@/components/ui/status';
import { plural } from '@/lib/format';
import { refusalReason } from './decision-error';

/** Exact counts, always both of them. A partial result is never rounded up to a success. */
export function reportHeadline(decision: DecisionReport['decision'], decided: number, refused: number): string {
  if (decided === 0) return `None of the ${plural(refused, 'item')} went through.`;
  return `You said ${decision === 'approve' ? 'yes' : 'no'} to ${decided}. ${refused} did not go through.`;
}

interface ReportProps {
  report: DecisionReport;
  inQueue: (id: string) => boolean;
  onReview: (id: string) => void;
  onDismiss: () => void;
  titleOf: (id: string) => string | null;
}

function Report({ report, inQueue, onReview, onDismiss, titleOf }: ReportProps) {
  return (
    <Card role="alert" className="p-4">
      <div className="flex items-start gap-2">
        <StatusDot tone="warn" className="mt-2" />
        <p className="min-w-0 flex-1 text-14 font-medium">{reportHeadline(report.decision, report.decided, report.refused.length)}</p>
        <IconButton label="Dismiss this report" size="sm" onClick={onDismiss}>
          <X className="size-4" />
        </IconButton>
      </div>
      <ul className="mt-2 divide-y divide-line pl-4">
        {report.refused.map((item) => (
          <li key={item.id} className="flex items-center gap-4 py-2">
            <span className="min-w-0 flex-1">
              {/* A refused item carries no summary of its own: its title is the one the queue showed. */}
              <span className="block text-14 break-words">{item.summary || titleOf(item.id) || 'This item'}</span>
              <span className="block text-13 text-muted">{refusalReason(item)}</span>
            </span>
            {inQueue(item.id) ? (
              <Button size="sm" onClick={() => onReview(item.id)}>
                Read it again
              </Button>
            ) : null}
          </li>
        ))}
      </ul>
    </Card>
  );
}

/** What did not go through stays here until it is dismissed, including across pages. */
export function DecisionReports({
  org,
  inQueue,
  onReview,
  titleOf,
}: {
  org: string;
  inQueue: (id: string) => boolean;
  onReview: (id: string) => void;
  titleOf: (id: string) => string | null;
}) {
  const { reports, dismiss } = useDecisionReports(org);
  if (reports.length === 0) return null;
  return (
    <div className="mb-6 grid gap-2">
      {reports.map((report) => (
        <Report key={report.id} report={report} inQueue={inQueue} onReview={onReview} titleOf={titleOf} onDismiss={() => dismiss(report.id)} />
      ))}
    </div>
  );
}
