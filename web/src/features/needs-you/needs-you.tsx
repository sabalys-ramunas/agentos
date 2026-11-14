/**
 * Needs you (Mono): a queue on the left and the open request on the right, as a card that says what
 * happens if you say yes, with a preview fitted to its kind and the two answers at its foot. On a phone
 * the queue is one screen and the open request another, with the answers pinned at the bottom.
 *
 * Keys: J and K move, A approves, D denies, and the next request opens after a decision. Brand changes
 * (the owner's) sit in the same queue and are decided through the brand hooks, never the approvals ones.
 *
 * The rules that keep a person safe (walkthrough.md):
 * - Nothing moves under the finger: a decided card shows it is done where it was, and the next
 *   item's buttons wait a moment (`decision-flow.ts`). New arrivals wait while the pointer is in
 *   the list.
 * - Never "Approved": an Undo while the cloud holds the decision, then the real result.
 * - A broken account is said before deciding, with its fix.
 * - Money and the irreversible are decided one at a time, with a second step.
 */
import { Link, useParams, useSearch } from '@tanstack/react-router';
import { useCallback, useEffect, useState } from 'react';
import { toast } from 'sonner';
import { useAnswerQuestion, useDecideBrand } from '@/api/hooks';
import { isCancelled } from '@/api/http';
import type { Approval, BrandProposal, Question } from '@/api/types';
import { InstallPrompt, markInstallMoment } from '@/components/install-prompt';
import { Button } from '@/components/ui/button';
import { PageHeader } from '@/components/ui/misc';
import { QueryView } from '@/components/ui/states';
import { cn } from '@/lib/cn';
import { describeError, isContentChanged } from '@/lib/errors';
import { useKeys } from '@/lib/keyboard';
import { useTechnical } from '@/lib/technical';
import { usePrincipalName } from '@/lib/use-principal';
import { useRunsAlone } from '@/features/nodes/add-node';
import { canAllowAgain } from '@/features/standing/describe';
import { StandingDialog } from '@/features/standing/standing-dialog';
import { ConfirmBox, isHeavy } from './confirm-approve';
import { decisionError, needsReading } from './decision-error';
import { useArmed, useDecisionFlow } from './decision-flow';
import { DecisionReports, reportHeadline } from './decision-report';
import { partsWords } from './count';
import type { useNeedsYouCount } from './count';
import { StoppedRuns } from './stopped-runs';
import { DecidedList, FailedStrip, OutcomeBar } from './outcome';
import { useNarrow } from './use-narrow';
import { doingOf, rowTitleOf, whereOf } from './words';
import { GroupHeader, HandCard, ListSkeleton, batchWords, batchable } from './queue-rows';
import { BatchBar, QueueCard } from './queue-card';
import { OpenRequest, RequestColumn } from './request-pane';
import { useQueue } from './use-queue';

/** The header's sentence: the count (count.ts) with its parts, so it agrees with the rail and the status pill. */
function headerWords(count: ReturnType<typeof useNeedsYouCount>): string {
  // It says nothing about what waits until every list has answered: never "nothing" while loading, never on an error.
  if (!count.loaded) return count.failedToLoad ? 'Could not read what waits for you.' : 'Reading what waits for you.';
  const total = count.total;
  if (total === 0) return 'Nothing is waiting for you. Your routines run on their own.';
  const parts = partsWords(count.parts);
  return `${total === 1 ? 'One thing needs' : `${total} things need`} you${parts.length > 1 ? `: ${parts.join(', ')}` : ''}. Only what you asked to see, plus money and things that can't be undone. The rest runs on its own.`;
}

/** The next id to focus once `ids` leave the list: the one that took their place, or the last. */
function nextAfter(order: string[], ids: string[], focusIndex: number): string | null {
  const after = order.filter((id) => !ids.includes(id));
  return after[Math.min(Math.max(focusIndex, 0), after.length - 1)] ?? null;
}

/** What is open, by kind. A focused approval wins, then a brand change, a question and a code. */
function openOf(queue: ReturnType<typeof useQueue>, focusId: string | null) {
  const { order, flat, brands, questions, openCodes } = queue;
  const focusedId = focusId && order.includes(focusId) ? focusId : (order[0] ?? null);
  const focused = flat.find((item) => item.id === focusedId) ?? null;
  const brand = focused ? null : (brands.find((item) => item.id === focusedId) ?? null);
  const question = focused || brand ? null : (questions.find((item) => item.id === focusedId) ?? null);
  const code = focused || brand || question ? null : (openCodes.find((item) => item.id === focusedId) ?? null);
  return { focusedId, focused, brand, question, code, focusIndex: focusedId ? order.indexOf(focusedId) : -1 };
}

/** What is on screen. A card just decided stays there, done, until the next one is due. */
function shownOf(open: ReturnType<typeof openOf>, done: { approval: Approval } | null) {
  const approval = done?.approval ?? open.focused;
  return {
    approval,
    id: approval?.id ?? open.brand?.id ?? open.question?.id ?? open.code?.id ?? null,
    any: Boolean(approval || open.brand || open.question || open.code),
    rows: { approval: done?.approval.id ?? open.focused?.id, brand: open.brand?.id, code: open.code?.id, question: open.question?.id },
  };
}

export function NeedsYou() {
  const { org } = useParams({ from: '/$org/needs-you' });
  // A push, an email or a Home card names one item: it opens on that one.
  const { approval: linked } = useSearch({ from: '/$org/needs-you' });
  const whoOf = usePrincipalName(org);
  const [technical] = useTechnical();
  const narrow = useNarrow();
  const [focusId, setFocusId] = useState<string | null>(linked ?? null);
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [selecting, setSelecting] = useState(false);
  const [techOpen, setTechOpen] = useState(technical);
  const [allowing, setAllowing] = useState<Approval | null>(null);
  const [confirming, setConfirming] = useState<Approval | null>(null);
  const [changed, setChanged] = useState<ReadonlySet<string>>(new Set());
  const [hovering, setHovering] = useState(false);
  const [decided, setDecided] = useState<ReadonlySet<string>>(new Set());
  const queue = useQueue(org, { hovering, decided });
  const { access, owner, approvals, count, order, flat, businessOf, colorOf } = queue;
  const canDecide = access.can('approvals.decide');
  const canStand = access.can('standing_approvals.create');
  const alone = useRunsAlone(org);
  const answer = useAnswerQuestion(org);
  const decideBrand = useDecideBrand(org);
  // On a phone the queue and the open request are two screens: a row, or a link to one item, opens it.
  const [opened, setOpened] = useState(Boolean(linked));

  const onUndone = useCallback((ids: string[]) => {
    setDecided((current) => new Set([...current].filter((id) => !ids.includes(id))));
    if (ids[0]) setFocusId(ids[0]);
  }, []);
  const flow = useDecisionFlow(org, { onUndone });

  const opening = openOf(queue, focusId);
  const { focusedId, focused, brand: focusedBrand, question: focusedQuestion, code: focusedCode, focusIndex } = opening;
  const shown = shownOf(opening, flow.done);
  const onScreen = shown.approval;
  const shownId = shown.id;
  const armed = useArmed(shownId, flow.decisions);
  const chosen = flat.filter((item) => selected.has(item.id) && batchable(item));

  useEffect(() => setTechOpen(technical), [shownId, technical]);

  // Focus is held by id. A live arrival at the top of the list must never change what the
  // next keypress decides. Not before approvals load: the first request back must not decide
  // where focus starts.
  useEffect(() => {
    if (!approvals.isPending && focusedId && focusedId !== focusId) setFocusId(focusedId);
  }, [approvals.isPending, focusedId, focusId]);

  // What was decided here leaves at once, and a later answer about it changes nothing.
  const settle = (ids: string[]) => setDecided((current) => new Set([...current, ...ids]));

  const run = (items: Approval[], decision: 'approve' | 'deny') => {
    if (items.length === 0 || !canDecide) return;
    const ids = items.map((item) => item.id);
    setFocusId(nextAfter(order, ids, focusIndex));
    setSelected(new Set());
    setSelecting(false);
    settle(ids);
    const [first] = items;
    const words = first ? doingOf(first, whereOf(first, businessOf(first.lane))) : null;
    const many = batchWords(items);
    const label =
      items.length > 1
        ? decision === 'approve'
          ? many.doing
          : many.notDoing
        : decision === 'approve'
          ? (words?.doing ?? 'On its way')
          : (words?.notDoing ?? 'Not doing it');
    flow.run(items, decision, {
      label,
      onSuccess: ({ decided: done, refused }) => {
        if (done.length) markInstallMoment();
        if (refused.length === 0) return;
        // Never a success: the report stays on the page, and this only points at it.
        toast.error(reportHeadline(decision, done.length, refused.length));
        const refusedIds = new Set(refused.map((item) => item.id));
        // A refused row has to come back, even while the pointer is holding the list still.
        setDecided((current) => new Set([...current].filter((id) => !refusedIds.has(id))));
        const again = refused.filter(needsReading).map((item) => item.id);
        setChanged((current) => new Set([...current, ...again]));
        if (again[0]) setFocusId(again[0]);
      },
      onError: (error) => {
        if (isCancelled(error)) return;
        toast.error(decisionError(error));
        setDecided((current) => new Set([...current].filter((id) => !ids.includes(id))));
        if (isContentChanged(error)) {
          // Back to the item, marked, so it has to be read again before deciding.
          setFocusId(ids[0] ?? null);
          setChanged((current) => new Set([...current, ...ids]));
        }
      },
    });
  };

  // Heavy effects, and anything that changed after it was shown, take a second deliberate step,
  // as on the phone, and never go through in a batch (security review 12).
  const needsConfirm = (item: Approval) => isHeavy(item) || changed.has(item.id);

  const approve = (items: Approval[]) => {
    if (!canDecide || items.length === 0) return;
    if (items.length === 1 && items[0] && needsConfirm(items[0])) {
      setConfirming(items[0]);
      return;
    }
    if (items.some(needsConfirm)) {
      toast.error('Payments, things that cannot be undone and changed items are decided one at a time.');
      return;
    }
    run(items, 'approve');
  };

  /** The keyboard and the buttons both go through here, so both respect the done state and the arming. */
  const ready = () => canDecide && armed && !flow.done;
  const targets = () => (chosen.length ? chosen : focused ? [focused] : []);

  /** A code card answered or declined: the focus moves on to the next thing, as after an answer. */
  const leaveCode = (id: string) => setFocusId(nextAfter(order, [id], focusIndex));

  const reply = (question: Question, text: string) => {
    if (!canDecide || !armed) return;
    setFocusId(nextAfter(order, [question.id], focusIndex));
    settle([question.id]);
    answer.mutate(
      { id: question.id, answer: text },
      {
        onSuccess: () => toast('Answer sent'),
        onError: (error) => {
          toast.error(describeError(error));
          setFocusId(question.id);
        },
      },
    );
  };

  /** A brand change, decided on its profile for the version shown. There is no undo for it on the node. */
  const decideBrandNow = (proposal: BrandProposal, approve: boolean) => {
    if (!owner || !armed || decideBrand.isPending || flow.done) return;
    decideBrand.mutate(
      { ref: proposal.ref, version: proposal.version, approve },
      {
        onSuccess: () => {
          toast(approve ? `Version ${proposal.version} of ${proposal.display_name} is approved` : `Version ${proposal.version} of ${proposal.display_name} is declined`);
          setFocusId(nextAfter(order, [proposal.id], focusIndex));
          settle([proposal.id]);
        },
        onError: (error) => toast.error(describeError(error)),
      },
    );
  };

  const move = (delta: number) => {
    const next = order[Math.max(0, Math.min(order.length - 1, focusIndex + delta))];
    if (next) setFocusId(next);
  };

  const toggle = (id: string, checked: boolean) => {
    setSelected((current) => {
      const next = new Set(current);
      if (checked) next.add(id);
      else next.delete(id);
      return next;
    });
  };

  // Number keys pick an option, so a question takes one keystroke, like an approval does.
  const digitKeys = Object.fromEntries(
    (focusedQuestion?.options ?? []).slice(0, 9).map((option, index) => [
      String(index + 1),
      (event: KeyboardEvent) => {
        if (!event.repeat && focusedQuestion) reply(focusedQuestion, option);
      },
    ]),
  );

  useKeys({
    j: () => move(1),
    k: () => move(-1),
    // A held key must not run down the queue deciding things.
    a: (event) => {
      if (event.repeat) return;
      if (focusedBrand && !chosen.length) decideBrandNow(focusedBrand, true);
      else if (ready()) approve(targets());
    },
    d: (event) => {
      if (event.repeat) return;
      if (focusedBrand && !chosen.length) decideBrandNow(focusedBrand, false);
      else if (ready()) run(targets(), 'deny');
    },
    x: () => {
      if (!focused || !batchable(focused)) return;
      setSelecting(true);
      toggle(focused.id, !selected.has(focused.id));
    },
    h: () => setTechOpen((open) => !open),
    ...digitKeys,
  });

  const allowAgain = (approval: Approval) =>
    canStand && canAllowAgain(approval, alone) ? (
      <Button size="sm" className="justify-self-start" onClick={() => setAllowing(approval)}>
        Always allow posts like this
      </Button>
    ) : null;

  // With nothing in the list, All clear takes the whole width.
  const hasList = queue.toDecide > 0;
  const failedStrip = (
    <FailedStrip org={org} items={queue.failed} businessOf={(lane) => businessOf(lane) || null} colorOf={colorOf} />
  );

  const open = (id: string) => {
    setFocusId(id);
    setOpened(true);
  };
  const somethingOpen = shown.any;
  // On a phone: the queue, or the open request as its own screen. On a desk: both.
  const showQueue = !narrow || !opened || !somethingOpen;
  const showRequest = !narrow || (opened && somethingOpen);
  const requestAlone = narrow && opened && somethingOpen;

  const request = somethingOpen ? (
    <OpenRequest
      org={org}
      queue={queue}
      approval={onScreen}
      brand={focusedBrand}
      code={focusedCode}
      question={focusedQuestion}
      done={flow.done}
      armed={armed}
      changed={changed}
      canDecide={canDecide}
      owner={owner}
      techOpen={techOpen}
      onToggleTech={() => setTechOpen((open) => !open)}
      allowAgain={allowAgain}
      onYes={(approval) => ready() && approve([approval])}
      onNo={(approval) => ready() && run([approval], 'deny')}
      brandPending={decideBrand.isPending}
      onBrand={decideBrandNow}
      onLeaveCode={leaveCode}
      onAnswer={reply}
    />
  ) : null;

  return (
    <>
      <PageHeader
        title="Needs you"
        description={headerWords(count)}
        actions={
          <div className="flex gap-2">
            {canDecide && flat.length + queue.questions.length > 1 ? (
              <Button asChild>
                <Link to="/$org/approve" params={{ org }}>
                  Go through them one by one
                </Link>
              </Button>
            ) : null}
          </div>
        }
      />
      <InstallPrompt />
      {/* Outside the queue: "all clear" must never replace news that something was refused. */}
      <DecisionReports
        org={org}
        inQueue={(id) => flat.some((item) => item.id === id)}
        onReview={setFocusId}
        titleOf={(id) => {
          const item = (approvals.data ?? []).find((row) => row.id === id);
          return item ? rowTitleOf(item, whereOf(item, businessOf(item.lane))) : null;
        }}
      />
      <QueryView query={approvals} skeleton={<ListSkeleton />}>
        {() => (
          <div className="grid gap-8">
            {requestAlone ? null : failedStrip}
            <div className={cn('grid items-start gap-6', hasList && 'lg:grid-cols-[minmax(300px,380px)_minmax(0,1fr)]')}>
              {hasList && showQueue ? (
                <QueueCard
                  queue={queue}
                  open={shown.rows}
                  whoOf={whoOf}
                  canDecide={canDecide}
                  changed={changed}
                  selecting={selecting}
                  selected={selected}
                  onHover={setHovering}
                  onChooseSeveral={() => {
                    setSelecting((on) => !on);
                    setSelected(new Set());
                  }}
                  onOpen={open}
                  onSelect={toggle}
                />
              ) : null}
              {showRequest ? (
                <RequestColumn
                  org={org}
                  queue={queue}
                  request={request}
                  narrow={narrow}
                  alone={requestAlone}
                  canDecide={canDecide}
                  failedStrip={failedStrip}
                  onBack={() => setOpened(false)}
                />
              ) : null}
            </div>
            {showQueue ? (
              <DecidedList
                org={org}
                items={queue.decidedHere}
                nameOf={(lane) => ({ name: businessOf(lane) || null, color: colorOf(lane) })}
              />
            ) : null}
            <StoppedRuns org={org} runs={queue.stopped} businessOf={businessOf} />
            <Hands org={org} queue={queue} whoOf={whoOf} />
          </div>
        )}
      </QueryView>
      <BatchBar chosen={chosen} onClear={() => setSelected(new Set())} onNo={() => run(chosen, 'deny')} onYes={() => approve(chosen)} />
      <OutcomeBar org={org} last={flow.last} onUndo={flow.takeBack} onDismiss={flow.dismiss} undoing={flow.undoing} />
      {allowing ? (
        <StandingDialog
          key={allowing.id}
          org={org}
          open
          from={allowing}
          onOpenChange={(open) => !open && setAllowing(null)}
          onCreated={() => toast('Saved. No computer applies it yet, so you will still be asked each time.')}
        />
      ) : null}
      <ConfirmBox
        org={org}
        approval={confirming}
        business={confirming ? businessOf(confirming.lane) || null : null}
        changed={confirming ? changed.has(confirming.id) : false}
        onClose={() => setConfirming(null)}
        onConfirm={(item) => {
          setConfirming(null);
          run([item], 'approve');
        }}
      />
    </>
  );
}

/** Accounts that need a person's hand (a login, a check), with a link to every sign-in that waits. */
function Hands({ org, queue, whoOf }: { org: string; queue: ReturnType<typeof useQueue>; whoOf: ReturnType<typeof usePrincipalName> }) {
  const { hands, loginQueue, businessOf } = queue;
  if (!hands.length) return null;
  return (
    <section aria-label="Accounts that need a hand">
      <GroupHeader name="Accounts that need a hand" why="No rush. The agent cannot do these for you." count={hands.length} />
      <ul className="mt-2 grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
        {hands.map((lane) => (
          <li key={lane.id}>
            <HandCard org={org} lane={lane} business={businessOf(lane.id) || null} nameOf={whoOf} />
          </li>
        ))}
      </ul>
      {loginQueue.data && loginQueue.data.lanes.length > 0 ? (
        <Link to="/$org/logins" params={{ org }} className="mt-2 flex min-h-8 items-center px-4 text-13 text-muted hover:text-fg md:min-h-0">
          All sign-ins: {loginQueue.data.summary}
        </Link>
      ) : null}
    </section>
  );
}
