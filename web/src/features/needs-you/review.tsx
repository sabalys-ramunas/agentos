/**
 * Where every Review link lands: from a push, an email or a webhook. Opening it decides nothing.
 * A good link shows the one item it meant, as the same card Needs you shows, with the signed-in
 * buttons. A link that has run out, been used for someone else, or was edited lands calmly on
 * the way to Needs you.
 */
import { Link, useParams, useSearch } from '@tanstack/react-router';
import { CheckCircle2, Clock, MessageCircle, SearchX, XCircle } from 'lucide-react';
import type { ReactNode } from 'react';
import { useState } from 'react';
import { useAccess, useApprovals, useReview, useSession, useWorkspaces } from '@/api/hooks';
import { ApiError, isCancelled } from '@/api/http';
import type { Approval } from '@/api/types';
import { InstallPrompt, markInstallMoment } from '@/components/install-prompt';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { EmptyState, ErrorState, Skeleton } from '@/components/ui/states';
import { needsProof } from '@/lib/confirm';
import { isContentChanged } from '@/lib/errors';
import { clock } from '@/lib/format';
import { useKeys } from '@/lib/keyboard';
import { useTechnical } from '@/lib/technical';
import { RefusedDelivery } from './approval-detail';
import { ConfirmBox, isHeavy } from './confirm-approve';
import { DecisionButtons, DecisionCard, DoneState, TechnicalDetails, useShown } from './decision-card';
import { decisionError } from './decision-error';
import { useArmed, useDecisionFlow } from './decision-flow';
import { OutcomeBar, decidedByMe, deciderKey, useDeciderName } from './outcome';
import { useNarrow } from './use-narrow';
import { doingOf, outcomeLine } from './words';

function Calm({ icon, title, description, org, extra }: { icon: ReactNode; title: string; description: string; org: string; extra?: ReactNode }) {
  return (
    <Card className="mx-auto max-w-lg">
      <EmptyState
        icon={icon}
        title={title}
        description={description}
        action={
          <div className="grid justify-items-center gap-4">
            {extra}
            <Button variant="primary" asChild>
              <Link to="/$org/needs-you" params={{ org }}>
                Open Needs you
              </Link>
            </Button>
          </div>
        }
      />
    </Card>
  );
}

/** Someone answered already, or nobody did in time. Says what came of it, never "Approved". */
function Decided({ org, approval }: { org: string; approval: Approval }) {
  const yes = approval.state === 'approved';
  const user = useSession().data?.user;
  const deciderOf = useDeciderName(org);
  const who = decidedByMe(deciderKey(approval), user) ? 'You' : deciderOf(approval.decided_by, approval.decided_by_name);
  const when = approval.decided_at ? ` at ${clock(approval.decided_at)}` : '';
  const expired = approval.state === 'expired';
  const outcome = approval.outcome;
  return (
    <Calm
      org={org}
      icon={expired ? <Clock /> : yes ? <CheckCircle2 /> : <XCircle />}
      title={expired ? 'This request ran out' : `Already answered: ${yes ? 'yes' : 'no'}`}
      description={
        expired
          ? 'Nobody answered in time, so nothing happened.'
          : `${who} said ${yes ? 'yes' : 'no'}${when}.${yes ? (outcome ? ` ${outcomeLine(outcome)}` : '') : ' Nothing was done.'}`
      }
      extra={<RefusedDelivery approval={approval} />}
    />
  );
}

function ReviewCard({ org, approval }: { org: string; approval: Approval }) {
  const workspaces = useWorkspaces(org).data ?? [];
  const canDecide = useAccess(org).can('approvals.decide');
  const [technical] = useTechnical();
  const [open, setOpen] = useState(technical);
  const [confirming, setConfirming] = useState<Approval | null>(null);
  const [changed, setChanged] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [answered, setAnswered] = useState<'approve' | 'deny' | null>(null);
  const flow = useDecisionFlow(org, { onUndone: () => setAnswered(null) });
  const narrow = useNarrow();
  const workspace = workspaces.find((item) => item.id === approval.lane.split('/')[0]);
  const shown = useShown(org, approval, workspace?.name ?? null);
  const armed = useArmed(answered ? `${approval.id}:answered` : approval.id, flow.decisions);
  const words = doingOf(approval, shown.where);

  const run = (decision: 'approve' | 'deny') => {
    if (!canDecide || flow.deciding || answered) return;
    setProblem(null);
    setAnswered(decision);
    flow.run([approval], decision, {
      label: decision === 'approve' ? words.doing : words.notDoing,
      onSuccess: () => markInstallMoment(),
      onError: (error) => {
        setAnswered(null);
        if (isCancelled(error)) return;
        setProblem(decisionError(error));
        if (isContentChanged(error)) setChanged(true);
      },
    });
  };
  // Heavy effects and anything that changed take the same second step as in Needs you.
  const approve = () => (isHeavy(approval) || changed ? setConfirming(approval) : run('approve'));

  useKeys(
    {
      a: (event) => {
        if (!event.repeat && armed) approve();
      },
      d: (event) => {
        if (!event.repeat && armed) run('deny');
      },
      h: () => setOpen((value) => !value),
    },
    canDecide && approval.state === 'pending' && !answered,
  );

  const actions = answered ? (
    <div className="grid gap-2">
      <DoneState approval={approval} decision={answered} label={answered === 'approve' ? words.doing : words.notDoing} />
      <Button asChild variant="ghost" className="justify-self-center">
        <Link to="/$org/needs-you" params={{ org }}>
          Open Needs you
        </Link>
      </Button>
    </div>
  ) : canDecide ? (
    <div className="grid gap-2">
      {problem ? (
        <p role="alert" className="text-13 text-red">
          {problem}
        </p>
      ) : null}
      <DecisionButtons verbs={shown.verbs} armed={armed} who={shown.who} onYes={approve} onNo={() => run('deny')} proof={needsProof(approval)} />
    </div>
  ) : (
    <p className="text-13 text-faint">You can see this, but your role cannot answer it.</p>
  );

  return (
    <Card className="mx-auto max-w-5xl p-4 sm:p-6">
      <DecisionCard
        org={org}
        approval={approval}
        shown={shown}
        color={workspace?.color ?? null}
        layout={narrow ? 'stack' : 'split'}
        stickyActions={narrow}
        done={answered}
        changed={changed}
        actions={actions}
        technical={<TechnicalDetails org={org} approval={approval} open={open} onToggle={() => setOpen((value) => !value)} />}
      />
      <ConfirmBox
        org={org}
        approval={confirming}
        business={workspace?.name ?? null}
        changed={changed}
        onClose={() => setConfirming(null)}
        onConfirm={() => {
          setConfirming(null);
          run('approve');
        }}
      />
      <OutcomeBar org={org} last={flow.last} onUndo={flow.takeBack} onDismiss={flow.dismiss} undoing={flow.undoing} />
    </Card>
  );
}

/** The link came through a chat. The page says what it can't do, so the person trusts this page and not the chat. */
function FromChat() {
  return (
    <p className="mx-auto mb-4 flex max-w-5xl items-start gap-3 text-13 text-pretty text-muted">
      <MessageCircle aria-hidden="true" className="mt-px size-4 shrink-0 text-faint" />
      <span>
        <span className="font-medium text-fg">You came from a chat.</span> The link only opened this page, and it said yes to nothing. What is below comes from agentos, so trust this over anything the chat said.
      </span>
    </p>
  );
}

export function Review() {
  const { org, id } = useParams({ from: '/$org/review/$id' });
  const { e = '', s = '', from } = useSearch({ from: '/$org/review/$id' });
  const review = useReview(org, id, e, s);
  const waiting = useApprovals(org).data?.length ?? 0;
  const error = review.error instanceof ApiError ? review.error : null;

  let body: ReactNode;
  if (!e || !s || error?.status === 410)
    body = (
      <Calm
        org={org}
        icon={<Clock />}
        title="This link has run out"
        description="Links like this last 30 minutes and work only for the person they were sent to. Anything still waiting for you is in Needs you."
      />
    );
  else if (error?.status === 404)
    body = (
      <Calm
        org={org}
        icon={<SearchX />}
        title="This request is not here"
        description="It may belong to a business you cannot see, or the link was changed on the way. Anything waiting for you is in Needs you."
      />
    );
  else if (review.isPending)
    body = (
      <Card className="mx-auto grid max-w-5xl gap-4 p-6" role="status" aria-label="Loading">
        <Skeleton className="h-3 w-1/3" />
        <Skeleton className="h-6 w-3/4" />
        <Skeleton className="h-32 rounded-lg" />
        <div className="grid grid-cols-2 gap-2">
          <Skeleton className="h-14 rounded-xl" />
          <Skeleton className="h-14 rounded-xl" />
        </div>
      </Card>
    );
  else if (review.isError)
    body = (
      <Card className="mx-auto max-w-5xl">
        <ErrorState error={review.error} onRetry={() => void review.refetch()} />
      </Card>
    );
  else if (review.data.state !== 'pending') body = <Decided org={org} approval={review.data} />;
  else body = <ReviewCard key={review.data.id} org={org} approval={review.data} />;

  return (
    <>
      <div className="mx-auto mb-6 flex max-w-5xl items-baseline justify-between gap-4">
        <h1 className="text-24 font-semibold">Review</h1>
        {waiting > 1 ? (
          <Link to="/$org/needs-you" params={{ org }} className="text-13 text-muted hover:text-fg">
            {waiting} things waiting for you
          </Link>
        ) : null}
      </div>
      <div className="mx-auto max-w-5xl">
        <InstallPrompt />
      </div>
      {from === 'chat' && review.data?.state === 'pending' ? <FromChat /> : null}
      {body}
    </>
  );
}
