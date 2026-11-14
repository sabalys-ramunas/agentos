/**
 * One decision at a time, with both answers at the thumb. No shell, no navigation: where a push
 * and "Go through them one by one" land. The buttons never move, so the rules in
 * `decision-flow.ts` keep a second tap from deciding the next item.
 */
import { Link, useParams } from '@tanstack/react-router';
import { X } from 'lucide-react';
import { useEffect, useState } from 'react';
import { toast } from 'sonner';
import { useAccess, useAnswerQuestion, useApprovals, useDecidedApprovals, useQuestions, useWorkspaces } from '@/api/hooks';
import { isCancelled } from '@/api/http';
import { useLiveEvents } from '@/api/live';
import type { Approval } from '@/api/types';
import { markInstallMoment } from '@/components/install-prompt';
import { Button } from '@/components/ui/button';
import { Logo, Meter } from '@/components/ui/misc';
import { ErrorState, Skeleton } from '@/components/ui/states';
import { needsProof } from '@/lib/confirm';
import { describeError } from '@/lib/errors';
import { useTechnical } from '@/lib/technical';
import { AllClear } from './all-clear';
import { QuestionDetail } from './approval-detail';
import { ConfirmBox, isHeavy } from './confirm-approve';
import { DecisionButtons, DecisionCard, DoneState, TechnicalDetails, useShown } from './decision-card';
import { decisionError } from './decision-error';
import { useArmed, useDecisionFlow } from './decision-flow';
import { OutcomeBar } from './outcome';
import { doingOf, whereOf } from './words';

function Current({
  org,
  approval,
  business,
  color,
  done,
  armed,
  canDecide,
  onYes,
  onNo,
}: {
  org: string;
  approval: Approval;
  business: string | null;
  color: string | null;
  done: 'approve' | 'deny' | null;
  armed: boolean;
  canDecide: boolean;
  onYes: () => void;
  onNo: () => void;
}) {
  const shown = useShown(org, approval, business);
  const [technical] = useTechnical();
  const [open, setOpen] = useState(technical);
  const words = doingOf(approval, shown.where);
  return (
    <>
      <main className="min-h-0 flex-1 animate-fade-in p-4">
        <DecisionCard
          org={org}
          approval={approval}
          shown={shown}
          color={color}
          layout="stack"
          done={done}
          actions={null}
          technical={<TechnicalDetails org={org} approval={approval} open={open} onToggle={() => setOpen((value) => !value)} />}
        />
      </main>
      <div className="safe-bottom sticky bottom-0 shrink-0 border-t border-line bg-bg/95 p-4 backdrop-blur">
        {done ? (
          <DoneState approval={approval} decision={done} label={done === 'approve' ? words.doing : words.notDoing} />
        ) : canDecide ? (
          <DecisionButtons verbs={shown.verbs} armed={armed} who={shown.who} onYes={onYes} onNo={onNo} keys={false} proof={needsProof(approval)} />
        ) : (
          <p className="text-center text-13 text-faint">You can see this, but your role cannot answer it.</p>
        )}
      </div>
    </>
  );
}

export function Approve() {
  const { org } = useParams({ from: '/$org/approve' });
  useLiveEvents(org);
  const approvals = useApprovals(org);
  const decided = useDecidedApprovals(org).data ?? [];
  const workspaces = useWorkspaces(org).data ?? [];
  const [count, setCount] = useState(0);
  const [pinned, setPinned] = useState<string | null>(null);
  const [confirming, setConfirming] = useState<Approval | null>(null);
  const questions = useQuestions(org).data ?? [];
  const answer = useAnswerQuestion(org);
  const canDecide = useAccess(org).can('approvals.decide');
  const flow = useDecisionFlow(org, { onUndone: (ids) => setPinned(ids[0] ?? null) });
  const items = (approvals.data ?? []).filter((item) => item.kind !== 'question');
  const left = items.length + questions.length;
  // The card stays put until it is decided. New approvals arrive at the top of the list,
  // and a thumb on the yes button must never land on one that appeared a moment ago.
  const current = items.find((item) => item.id === pinned) ?? (questions.some((item) => item.id === pinned) ? undefined : items[0]);
  // Nothing is pinned until approvals have loaded, or whichever request lands first would
  // decide the order of the queue.
  const question = current || approvals.isPending ? undefined : (questions.find((item) => item.id === pinned) ?? questions[0]);
  const onScreen = flow.done?.approval ?? current;
  const shownId = onScreen?.id ?? question?.id;
  const armed = useArmed(shownId, flow.decisions);

  useEffect(() => {
    const id = current?.id ?? question?.id;
    if (id && id !== pinned && !flow.done) setPinned(id);
  }, [current, question, pinned, flow.done]);

  const workspaceOf = (lane: string) => workspaces.find((item) => item.id === lane.split('/')[0]);

  const run = (approval: Approval, decision: 'approve' | 'deny') => {
    if (!canDecide || !armed || flow.done) return;
    const words = doingOf(approval, whereOf(approval, workspaceOf(approval.lane)?.name ?? null));
    flow.run([approval], decision, {
      label: decision === 'approve' ? words.doing : words.notDoing,
      onSuccess: ({ decided: done }) => {
        if (done.length) markInstallMoment();
      },
      onError: (error) => {
        if (!isCancelled(error)) toast.error(decisionError(error));
      },
    });
    setCount((value) => value + 1);
  };

  const yes = (approval: Approval) => {
    if (!canDecide || !armed || flow.done) return;
    if (isHeavy(approval)) setConfirming(approval);
    else run(approval, 'approve');
  };

  const reply = (text: string) => {
    if (!question || !armed) return;
    answer.mutate({ id: question.id, answer: text }, { onSuccess: () => toast('Answer sent'), onError: (error) => toast.error(describeError(error)) });
    setCount((value) => value + 1);
  };

  return (
    <div className="mx-auto flex min-h-dvh max-w-xl flex-col border-line md:border-x">
      <header className="flex h-14 shrink-0 items-center justify-between gap-2 border-b border-line px-4">
        <Link to="/$org" params={{ org }} aria-label="agentos home" className="rounded-md">
          <Logo />
        </Link>
        <span className="tabular flex-1 text-center text-13 text-faint">{left ? `${left} to decide` : ''}</span>
        <Button variant="ghost" size="sm" asChild icon={<X className="size-4" />}>
          <Link to="/$org/needs-you" params={{ org }}>
            Close
          </Link>
        </Button>
      </header>
      {count + left > 0 ? <Meter value={count} max={count + left} label="Decided so far" className="rounded-none" /> : null}
      {approvals.isPending ? (
        <main className="flex-1 space-y-4 p-4" role="status" aria-label="Loading">
          <Skeleton className="w-1/3" />
          <Skeleton className="h-6" />
          <Skeleton className="aspect-square h-auto rounded-lg" />
        </main>
      ) : approvals.isError ? (
        <main className="flex-1">
          <ErrorState error={approvals.error} onRetry={() => void approvals.refetch()} />
        </main>
      ) : onScreen ? (
        <Current
          key={onScreen.id}
          org={org}
          approval={onScreen}
          business={workspaceOf(onScreen.lane)?.name ?? null}
          color={workspaceOf(onScreen.lane)?.color ?? null}
          done={flow.done?.approval.id === onScreen.id ? flow.done.decision : null}
          armed={armed}
          canDecide={canDecide}
          onYes={() => yes(onScreen)}
          onNo={() => run(onScreen, 'deny')}
        />
      ) : question ? (
        <main key={question.id} className="min-h-0 flex-1 animate-fade-in overflow-y-auto p-4">
          <QuestionDetail org={org} question={question} canAnswer={canDecide} onAnswer={reply} />
        </main>
      ) : (
        <main className="flex flex-1 flex-col justify-center gap-4 p-4">
          {count > 0 ? <p className="text-center text-14 text-muted">That was the last one.</p> : null}
          <AllClear org={org} decided={decided} canDecide={canDecide} />
          <Button asChild className="justify-self-center">
            <Link to="/$org" params={{ org }}>
              Back to Home
            </Link>
          </Button>
        </main>
      )}
      <OutcomeBar org={org} last={flow.last} onUndo={flow.takeBack} onDismiss={flow.dismiss} undoing={flow.undoing} top />
      <ConfirmBox
        org={org}
        large
        approval={confirming}
        business={confirming ? (workspaceOf(confirming.lane)?.name ?? null) : null}
        changed={false}
        onClose={() => setConfirming(null)}
        onConfirm={(item) => {
          setConfirming(null);
          run(item, 'approve');
        }}
      />
    </div>
  );
}
