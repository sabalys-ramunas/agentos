/** The request half of Needs you: the one item open, or what to say when nothing is. */
import { ArrowLeft } from 'lucide-react';
import type { ReactNode } from 'react';
import type { Approval, BrandProposal, CodeRequest, Question } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { QuestionDetail } from './approval-detail';
import { CodeDetail } from './code-card';
import { AllClear } from './all-clear';
import { BrandDetail } from './brand-proposal';
import { Focused } from './focused';
import type { Queue } from './use-queue';

interface OpenRequestProps {
  org: string;
  queue: Queue;
  /** At most one of these four is set: what is open, by kind. */
  approval: Approval | null;
  brand: BrandProposal | null;
  code: CodeRequest | null;
  question: Question | null;
  /** The decision still on screen, if any: its card shows it is done, and a brand change waits. */
  done: { approval: Approval; decision: 'approve' | 'deny' } | null;
  armed: boolean;
  changed: ReadonlySet<string>;
  canDecide: boolean;
  owner: boolean;
  techOpen: boolean;
  onToggleTech: () => void;
  allowAgain: (approval: Approval) => ReactNode;
  onYes: (approval: Approval) => void;
  onNo: (approval: Approval) => void;
  brandPending: boolean;
  onBrand: (proposal: BrandProposal, approve: boolean) => void;
  onLeaveCode: (id: string) => void;
  onAnswer: (question: Question, text: string) => void;
}

export function OpenRequest(props: OpenRequestProps) {
  const { org, queue, approval, brand, code, question } = props;
  if (approval) {
    return (
      <Focused
        key={approval.id}
        org={org}
        approval={approval}
        business={queue.businessOf(approval.lane) || null}
        color={queue.colorOf(approval.lane)}
        done={props.done?.approval.id === approval.id ? props.done.decision : null}
        armed={props.armed}
        changed={props.changed.has(approval.id)}
        canDecide={props.canDecide}
        techOpen={props.techOpen}
        onToggleTech={props.onToggleTech}
        allowAgain={props.allowAgain(approval)}
        onYes={() => props.onYes(approval)}
        onNo={() => props.onNo(approval)}
      />
    );
  }
  if (brand) {
    return (
      <BrandDetail
        key={brand.id}
        org={org}
        proposal={brand}
        armed={props.armed && !props.done}
        pending={props.brandPending}
        done={null}
        canDecide={props.owner}
        onYes={() => props.onBrand(brand, true)}
        onNo={() => props.onBrand(brand, false)}
      />
    );
  }
  if (code) {
    return (
      <div className="p-5 lg:p-6">
        <CodeDetail key={code.id} org={org} card={code} business={queue.businessOf(code.lane)} onLeave={() => props.onLeaveCode(code.id)} />
      </div>
    );
  }
  if (question) {
    return (
      <div className="p-5 lg:p-6">
        <QuestionDetail org={org} question={question} canAnswer={props.canDecide} onAnswer={(text) => props.onAnswer(question, text)} />
      </div>
    );
  }
  return null;
}

interface RequestColumnProps {
  org: string;
  queue: Queue;
  /** The open request, or null when nothing is open. */
  request: ReactNode;
  narrow: boolean;
  /** On a phone, a request is open as its own screen. */
  alone: boolean;
  canDecide: boolean;
  failedStrip: ReactNode;
  onBack: () => void;
}

export function RequestColumn({ org, queue, request, narrow, alone, canDecide, failedStrip, onBack }: RequestColumnProps) {
  const { count, toDecide } = queue;
  const hasList = toDecide > 0;
  return (
    <div className="min-w-0">
      {narrow && hasList ? (
        <Button variant="ghost" size="sm" icon={<ArrowLeft className="size-4" />} className="mb-3" onClick={onBack}>
          All {toDecide} to decide
        </Button>
      ) : null}
      {request ? (
        <Card>
          <section aria-label="Selected item">{request}</section>
        </Card>
      ) : !count.loaded ? (
        <Card>
          <p role="status" className="p-6 text-14 text-muted">
            {count.failedToLoad ? 'Could not read what waits for you.' : 'Reading what waits for you.'}
          </p>
        </Card>
      ) : count.total === 0 ? (
        <AllClear org={org} decided={queue.decidedHere} canDecide={canDecide} />
      ) : (
        <Card>
          <p className="p-6 text-14 text-muted">Nothing to decide right now. What waits is listed below.</p>
        </Card>
      )}
      {alone ? <div className="mt-6">{failedStrip}</div> : null}
    </div>
  );
}
