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
import { ArrowLeft, Check, Fingerprint, ListChecks, MessageCircleQuestion, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useState } from 'react';
import { toast } from 'sonner';
import {
  useAccess,
  useAnswerQuestion,
  useApprovals,
  useBrandProposals,
  useCodes,
  useDecideBrand,
  useDecidedApprovals,
  useEnginePlans,
  useFailedApprovals,
  useLoginQueue,
  useQuestions,
  useRunsWaiting,
  useWorkspaces,
} from '@/api/hooks';
import { isCancelled } from '@/api/http';
import type { Approval, BrandProposal, Lane, Question, Workspace } from '@/api/types';
import { AgentMark, BusinessMark } from '@/components/brand/identity';
import { SiteMark } from '@/components/brand/site-mark';
import { InstallPrompt, markInstallMoment } from '@/components/install-prompt';
import { Count } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardHeader } from '@/components/ui/card';
import { IfYes } from '@/components/ui/if-yes';
import { KindEyebrow, KindTile } from '@/components/ui/kind';
import { Kbd, PageHeader } from '@/components/ui/misc';
import { Status } from '@/components/ui/status';
import { StateGlyph } from '@/components/ui/state-badge';
import { QueryView, Skeleton } from '@/components/ui/states';
import { Checkbox } from '@/components/ui/toggle';
import { useActiveWorkspace } from '@/lib/active-workspace';
import { cn } from '@/lib/cn';
import { needsProof } from '@/lib/confirm';
import { describeError, isContentChanged } from '@/lib/errors';
import { plural, timeAgo } from '@/lib/format';
import { useKeys } from '@/lib/keyboard';
import { needsHand } from '@/lib/lanes';
import type { Motion } from '@/lib/row-motion';
import { useRowMotion } from '@/lib/row-motion';
import { laneCondition } from '@/lib/states';
import { useTechnical } from '@/lib/technical';
import { useMe, usePrincipalName } from '@/lib/use-principal';
import { useRunsAlone } from '@/features/nodes/add-node';
import { canAllowAgain } from '@/features/standing/describe';
import { StandingDialog } from '@/features/standing/standing-dialog';
import { QuestionDetail } from './approval-detail';
import { CodeDetail, CodeRow } from './code-card';
import { ConfirmBox, isHeavy } from './confirm-approve';
import { DecisionButtons, DecisionCard, DoneState, TechnicalDetails, useShown } from './decision-card';
import { decisionError, needsReading } from './decision-error';
import { useArmed, useDecisionFlow } from './decision-flow';
import { DecisionReports, reportHeadline } from './decision-report';
import { AllClear } from './all-clear';
import { inWorkspace, partsWords, useNeedsYouCount } from './count';
import { PlansToApprove } from './plans-to-approve';
import { StoppedRuns } from './stopped-runs';
import { DecidedList, FailedStrip, OutcomeBar } from './outcome';
import { STAKE_ORDER, batchKind, kindOf, stakeOf } from './kinds';
import { useNarrow } from './use-narrow';
import { doingOf, handOf, rowTitleOf, whereOf } from './words';
import { BrandDetail, BrandRow } from './brand-proposal';
import { IfYesValue, ifYesWords, kindLabel, tileOf } from './if-yes';
import { queueRow, waitedShort } from './queue-words';

interface Group {
  name: string;
  why: string;
  items: Motion<Approval>[];
}

/** What is at stake and what kind it is, heaviest first: money, the irreversible, then emails, replies, forms, posts... */
function groupByStake(approvals: Motion<Approval>[]): Group[] {
  return STAKE_ORDER.map((key) => {
    const items = approvals.filter((item) => stakeOf(item).key === key);
    const stake = items[0] ? stakeOf(items[0]) : null;
    return { name: stake?.name ?? '', why: stake?.why ?? '', items };
  }).filter((group) => group.items.length > 0);
}

/** Only routine kinds go in a batch: posts and replies. Never money, the irreversible, email or a blocked account. */
const batchable = (approval: Approval) => batchKind(approval) && !approval.lane_state?.blocks;

/** The batch's words: "Post 3", "Send 2 replies", or plain yes and no for a mix. */
function batchWords(items: Approval[]) {
  const kinds = new Set(items.map(kindOf));
  const n = items.length;
  if (kinds.size === 1 && kinds.has('message')) {
    return { chosen: `${plural(n, 'reply', 'replies')} chosen`, yes: `Send ${plural(n, 'reply', 'replies')}`, no: "Don't send", doing: `Sending ${plural(n, 'reply', 'replies')}`, notDoing: `Not sending ${plural(n, 'reply', 'replies')}` };
  }
  if (kinds.size === 1) {
    return { chosen: `${plural(n, 'post')} chosen`, yes: `Post ${n}`, no: "Don't post", doing: `Posting ${plural(n, 'post')}`, notDoing: `Not posting ${plural(n, 'post')}` };
  }
  return { chosen: `${n} chosen`, yes: `Yes to ${n}`, no: 'No to all', doing: `Sending ${n} things`, notDoing: `Not sending ${n} things` };
}

function ListSkeleton() {
  return (
    <div className="grid gap-8 lg:grid-cols-[300px_minmax(0,1fr)]">
      <div className="grid content-start gap-2">
        {Array.from({ length: 5 }, (_, index) => (
          <div key={index} className="flex items-center gap-3 p-2">
            <Skeleton className="size-12 rounded-lg" />
            <div className="grid flex-1 gap-2">
              <Skeleton className="w-3/4" />
              <Skeleton className="h-3 w-1/2" />
            </div>
          </div>
        ))}
      </div>
      <Card className="grid gap-4 p-6">
        <Skeleton className="h-4 w-1/3" />
        <Skeleton className="h-8 w-3/4" />
        <Skeleton className="aspect-[4/3] h-auto rounded-xl" />
      </Card>
    </div>
  );
}

interface RowProps {
  approval: Approval;
  business: string;
  color: string | null;
  focused: boolean;
  changed: boolean;
  selecting: boolean;
  selected: boolean;
  onFocus: () => void;
  onSelect: (checked: boolean) => void;
}

/** A row of the queue: the kind's tile, the request, what a yes does, and how long it has waited. */
function Row({ approval, business, color, focused, changed, selecting, selected, onFocus, onSelect }: RowProps) {
  const where = whereOf(approval, business);
  const blocked = approval.lane_state?.blocks;
  const title = rowTitleOf(approval, where);
  return (
    <div className="flex items-stretch">
      {selecting && batchable(approval) ? (
        <span className="flex items-center pl-4">
          <Checkbox checked={selected} onCheckedChange={(value) => onSelect(value === true)} aria-label={`Choose: ${title}, ${business}`} />
        </span>
      ) : null}
      <button
        type="button"
        onClick={onFocus}
        aria-current={focused || undefined}
        aria-label={[title, business, timeAgo(approval.created_at), needsProof(approval) ? "takes proving it's you" : null, changed ? 'changed, read it again' : null, blocked ? 'the account needs a hand first' : null]
          .filter(Boolean)
          .join(', ')}
        className={queueRow(focused)}
      >
        <KindTile kind={tileOf(approval)} size="sm" />
        <span className="grid min-w-0 flex-1 gap-1">
          <span className="flex min-w-0 items-center gap-2">
            <span title={title} className="truncate text-label">
              {title}
            </span>
            {needsProof(approval) ? <Fingerprint aria-hidden="true" className="size-3 shrink-0 text-faint" /> : null}
            <span className="ml-auto shrink-0 font-mono text-code-caption text-faint tabular">{waitedShort(approval.created_at)}</span>
          </span>
          <IfYes title={ifYesWords(approval, where)}>
            <IfYesValue approval={approval} where={where} />
          </IfYes>
          <span className="flex min-w-0 items-center gap-2 text-caption text-faint">
            <BusinessMark name={business} color={color} size={14} />
            <span className="truncate">{business}</span>
          </span>
          {changed ? <span className="text-caption font-medium text-fg">Changed, read it again</span> : null}
          {blocked ? <span className="truncate text-caption text-warn">The account needs a hand first</span> : null}
        </span>
      </button>
    </div>
  );
}

function QuestionRow({ question, business, askedBy, focused, onFocus }: { question: Question; business: string; askedBy: string; focused: boolean; onFocus: () => void }) {
  return (
    <button type="button" onClick={onFocus} aria-current={focused || undefined} className={queueRow(focused)}>
      <span className="flex size-6 shrink-0 items-center justify-center rounded-sm border border-line bg-card text-muted shadow-lift">
        <MessageCircleQuestion aria-hidden="true" className="size-3.5" />
      </span>
      <span className="grid min-w-0 flex-1 gap-1">
        <span className="flex min-w-0 items-center gap-2">
          <span title={question.question} className="truncate text-label">
            {question.question}
          </span>
          <span className="ml-auto shrink-0 font-mono text-code-caption text-faint tabular">{waitedShort(question.created_at)}</span>
        </span>
        <span className="flex min-w-0 items-center gap-2 text-caption text-faint">
          <AgentMark principal={question.asked_by} size={16} />
          <span className="truncate">{[askedBy, business].filter(Boolean).join(' · ')}</span>
        </span>
      </span>
    </button>
  );
}

/** An account that needs a person, as a card: what to do, why, and the button for it under the words. */
function HandCard({ org, lane, business, nameOf }: { org: string; lane: Lane; business: string | null; nameOf: ReturnType<typeof usePrincipalName> }) {
  const hand = handOf(lane, business, (principal) => nameOf(principal));
  const me = useMe();
  const condition = laneCondition(lane, { me });
  return (
    <Card className="flex h-full items-start gap-3 p-4">
      <span className="relative shrink-0">
        <span className={cn('flex size-10 items-center justify-center rounded-lg', condition.state === 'stuck' ? 'bg-(--stuck-soft)' : 'bg-(--wait-soft)')}>
          <StateGlyph state={condition.state === 'stuck' ? 'stuck' : 'waiting'} size={20} />
        </span>
        <span className="absolute -right-1 -bottom-1 flex size-5 items-center justify-center rounded-full border border-line bg-bg">
          <SiteMark site={lane.site?.key ?? lane.name} size={14} label={false} />
        </span>
      </span>
      <span className="grid min-w-0 flex-1 justify-items-start gap-2">
        <span className="min-w-0">
          <span className="block text-14 font-medium break-words">{hand.title}</span>
          <span className="block text-13 break-words text-faint">{hand.action === 'Log in' && lane.login_request?.why ? lane.login_request.why : condition.sentence}</span>
        </span>
        <Button asChild size="sm">
          <Link to="/$org/lanes/$ws/$name" params={{ org, ws: lane.workspace, name: lane.name }}>
            {hand.action}
          </Link>
        </Button>
      </span>
    </Card>
  );
}

function GroupHeader({ name, why, count, action }: { name: string; why?: string; count: number; action?: React.ReactNode }) {
  return (
    <div className="px-4 pt-4 pb-1">
      <div className="flex min-h-7 items-center gap-2">
        <h2 className="text-secondary font-medium text-fg">{name}</h2>
        <Count value={count} />
        {action ? <span className="ml-auto">{action}</span> : null}
      </div>
      {why ? <p className="text-caption text-faint">{why}</p> : null}
    </div>
  );
}

/** The item on screen: the card, its buttons or its done state, and the technical details. */
function Focused({
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

export function NeedsYou() {
  const { org } = useParams({ from: '/$org/needs-you' });
  // A push, an email or a Home card names one item: it opens on that one.
  const { approval: linked } = useSearch({ from: '/$org/needs-you' });
  const whoOf = usePrincipalName(org);
  const approvals = useApprovals(org);
  const failedQuery = useFailedApprovals(org);
  const decidedQuery = useDecidedApprovals(org);
  const workspaces = useWorkspaces(org).data ?? [];
  const [active] = useActiveWorkspace(org);
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
  const access = useAccess(org);
  const owner = access.role === 'owner';
  const canDecide = access.can('approvals.decide');
  const canStand = access.can('standing_approvals.create');
  const alone = useRunsAlone(org);
  const questionsQuery = useQuestions(org);
  const loginQueue = useLoginQueue(org);
  const answer = useAnswerQuestion(org);
  // Login codes are the owner's alone: no other role is asked for one, so none is read.
  const codesQuery = useCodes(org, owner);
  const stoppedQuery = useRunsWaiting(org, owner);
  const plansQuery = useEnginePlans(org, owner);
  // The same count as the rail, the bar and Home (count.ts), with its parts.
  const count = useNeedsYouCount(org, active);
  // Brand changes are the owner's to decide; anyone else's read is an empty list.
  const brandQuery = useBrandProposals(org, owner);
  const decideBrand = useDecideBrand(org);
  // On a phone the queue and the open request are two screens: a row, or a link to one item, opens it.
  const [opened, setOpened] = useState(Boolean(linked));

  const onUndone = useCallback((ids: string[]) => {
    setDecided((current) => new Set([...current].filter((id) => !ids.includes(id))));
    if (ids[0]) setFocusId(ids[0]);
  }, []);
  const flow = useDecisionFlow(org, { onUndone });

  const inScope = useCallback((lane: string | null | undefined) => inWorkspace(active)(lane), [active]);
  const visible = useMemo(() => (approvals.data ?? []).filter((item) => item.kind !== 'question' && inScope(item.lane)), [approvals.data, inScope]);
  const questions = useMemo(() => (questionsQuery.data ?? []).filter((item) => inScope(item.lane)), [questionsQuery.data, inScope]);
  const openCodes = useMemo(() => (codesQuery.data ?? []).filter((item) => item.state === 'open' && inScope(item.lane)), [codesQuery.data, inScope]);
  const stopped = useMemo(() => (stoppedQuery.data ?? []).filter((run) => inScope(run.lane)), [stoppedQuery.data, inScope]);
  // A run that stopped halfway has its own three answers, so it is not also listed as a failure.
  const stoppedApprovals = useMemo(() => new Set(stopped.map((run) => run.approval_id).filter((id): id is string => Boolean(id))), [stopped]);
  const failed = useMemo(
    () => (failedQuery.data ?? []).filter((item) => inScope(item.lane) && !stoppedApprovals.has(item.id)),
    [failedQuery.data, inScope, stoppedApprovals],
  );
  const plans = useMemo(() => (plansQuery.data ?? []).filter((row) => inScope(row.surface)), [plansQuery.data, inScope]);
  const flat = useMemo(() => groupByStake(visible).flatMap((group) => group.items), [visible]);
  const brands = useMemo(
    () => (brandQuery.data ?? []).filter((item) => (!active || item.workspace === active) && !decided.has(item.id)),
    [brandQuery.data, active, decided],
  );
  const motion = useRowMotion(flat, { hold: hovering, decided });
  const questionMotion = useRowMotion(questions, { hold: hovering, decided });
  const codeMotion = useRowMotion(openCodes, { hold: hovering, decided });
  const groups = useMemo(() => groupByStake(motion.rows), [motion.rows]);
  const waiting = motion.waiting + questionMotion.waiting + codeMotion.waiting;

  const order = useMemo(
    () => [...flat.map((item) => item.id), ...brands.map((item) => item.id), ...questions.map((item) => item.id), ...openCodes.map((item) => item.id)],
    [flat, brands, questions, openCodes],
  );
  const focusedId = focusId && order.includes(focusId) ? focusId : (order[0] ?? null);
  const focused = flat.find((item) => item.id === focusedId) ?? null;
  const focusedBrand = focused ? null : (brands.find((item) => item.id === focusedId) ?? null);
  const focusedQuestion = focused || focusedBrand ? null : (questions.find((item) => item.id === focusedId) ?? null);
  const focusedCode = focused || focusedBrand || focusedQuestion ? null : (openCodes.find((item) => item.id === focusedId) ?? null);
  // A card just decided stays on screen, done, until the next one is due.
  const onScreen = flow.done?.approval ?? focused;
  const shownId = onScreen?.id ?? focusedBrand?.id ?? focusedQuestion?.id ?? focusedCode?.id ?? null;
  const armed = useArmed(shownId, flow.decisions);
  const chosen = useMemo(() => flat.filter((item) => selected.has(item.id) && batchable(item)), [flat, selected]);
  const focusIndex = focusedId ? order.indexOf(focusedId) : -1;
  const hands = workspaces
    .filter((item) => !active || item.id === active)
    .flatMap((item) => item.lanes)
    .filter(needsHand);

  const workspaceOf = (lane: string | null | undefined): Workspace | undefined => workspaces.find((item) => item.id === (lane ?? '').split('/')[0]);
  const businessOf = (lane: string | null | undefined) => workspaceOf(lane)?.name ?? (lane ?? '').split('/')[0] ?? '';
  const colorOf = (lane: string | null | undefined) => workspaceOf(lane)?.color ?? null;

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
    const after = order.filter((id) => !ids.includes(id));
    setFocusId(after[Math.min(Math.max(focusIndex, 0), after.length - 1)] ?? null);
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
  const leaveCode = (id: string) => {
    const after = order.filter((item) => item !== id);
    setFocusId(after[Math.min(Math.max(focusIndex, 0), after.length - 1)] ?? null);
  };

  const reply = (question: Question, text: string) => {
    if (!canDecide || !armed) return;
    const after = order.filter((id) => id !== question.id);
    setFocusId(after[Math.min(Math.max(focusIndex, 0), after.length - 1)] ?? null);
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
          const after = order.filter((id) => id !== proposal.id);
          setFocusId(after[Math.min(Math.max(focusIndex, 0), after.length - 1)] ?? null);
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

  const toDecide = flat.length + brands.length + questions.length + openCodes.length;
  const decidedHere = (decidedQuery.data ?? []).filter((item) => inScope(item.lane));
  // With nothing in the list, All clear takes the whole width.
  const hasList = toDecide > 0;
  // The header says the count (count.ts) with its parts, so it agrees with the rail and the status pill. It says
  // nothing about what waits until every list has answered: never "nothing" while loading, never on an error.
  const total = count.total;
  const parts = partsWords(count.parts);
  const description = !count.loaded
    ? count.failedToLoad
      ? 'Could not read what waits for you.'
      : 'Reading what waits for you.'
    : total === 0
      ? 'Nothing is waiting for you. Your routines run on their own.'
      : `${total === 1 ? 'One thing needs' : `${total} things need`} you${parts.length > 1 ? `: ${parts.join(', ')}` : ''}. Only what you asked to see, plus money and things that can't be undone. The rest runs on its own.`;
  const failedStrip = (
    <FailedStrip org={org} items={failed} businessOf={(lane) => businessOf(lane) || null} colorOf={colorOf} />
  );

  const yes = (approval: Approval) => {
    if (ready()) approve([approval]);
  };
  const no = (approval: Approval) => {
    if (ready()) run([approval], 'deny');
  };

  // The oldest thing in the queue, for the queue's head: "the oldest for 2 h".
  const oldest = [...flat.map((item) => item.created_at), ...brands.map((item) => item.created_at), ...questions.map((item) => item.created_at), ...openCodes.map((item) => item.created_at)].sort()[0] ?? null;
  const open = (id: string) => {
    setFocusId(id);
    setOpened(true);
  };
  const somethingOpen = Boolean(onScreen || focusedBrand || focusedQuestion || focusedCode);
  // On a phone: the queue, or the open request as its own screen. On a desk: both.
  const showQueue = !narrow || !opened || !somethingOpen;
  const showRequest = !narrow || (opened && somethingOpen);

  const request = onScreen ? (
    <Focused
      key={onScreen.id}
      org={org}
      approval={onScreen}
      business={businessOf(onScreen.lane) || null}
      color={colorOf(onScreen.lane)}
      done={flow.done?.approval.id === onScreen.id ? flow.done.decision : null}
      armed={armed}
      changed={changed.has(onScreen.id)}
      canDecide={canDecide}
      techOpen={techOpen}
      onToggleTech={() => setTechOpen((open) => !open)}
      allowAgain={allowAgain(onScreen)}
      onYes={() => yes(onScreen)}
      onNo={() => no(onScreen)}
    />
  ) : focusedBrand ? (
    <BrandDetail
      key={focusedBrand.id}
      org={org}
      proposal={focusedBrand}
      armed={armed && !flow.done}
      pending={decideBrand.isPending}
      done={null}
      canDecide={owner}
      onYes={() => decideBrandNow(focusedBrand, true)}
      onNo={() => decideBrandNow(focusedBrand, false)}
    />
  ) : focusedCode ? (
    <div className="p-5 lg:p-6">
      <CodeDetail key={focusedCode.id} org={org} card={focusedCode} business={businessOf(focusedCode.lane)} onLeave={() => leaveCode(focusedCode.id)} />
    </div>
  ) : focusedQuestion ? (
    <div className="p-5 lg:p-6">
      <QuestionDetail org={org} question={focusedQuestion} canAnswer={canDecide} onAnswer={(text) => reply(focusedQuestion, text)} />
    </div>
  ) : null;

  return (
    <>
      <PageHeader
        title="Needs you"
        description={description}
        actions={
          <div className="flex gap-2">
            {canDecide && flat.length + questions.length > 1 ? (
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
            {narrow && opened && somethingOpen ? null : failedStrip}
            <div className={cn('grid items-start gap-6', hasList && 'lg:grid-cols-[minmax(300px,380px)_minmax(0,1fr)]')}>
              {hasList && showQueue ? (
                <Card
                  className="relative min-w-0 overflow-hidden lg:sticky lg:top-[calc(var(--topbar-height)+16px)]"
                  onPointerEnter={(event) => event.pointerType === 'mouse' && setHovering(true)}
                  onPointerLeave={() => setHovering(false)}
                >
                  <CardHeader
                    title={
                      <>
                        To decide
                        <Count value={toDecide} tone="warn" />
                      </>
                    }
                    meta={oldest ? <span className="hidden sm:inline">the oldest for {waitedShort(oldest)}</span> : null}
                    action={
                      <span className="hidden items-center gap-1 text-caption text-faint lg:inline-flex" aria-hidden="true">
                        <Kbd>J</Kbd>
                        <Kbd>K</Kbd>
                        move
                      </span>
                    }
                  />
                  {waiting > 0 ? (
                    <div className="pointer-events-none sticky top-2 z-10 -mb-6 flex h-0 justify-center">
                      <span className="tabular h-6 animate-pop-in rounded-full border border-line bg-raised px-2 text-12 leading-6 text-muted shadow-popover">
                        {waiting} new when you move away
                      </span>
                    </div>
                  ) : null}
                  <div className="divide-y divide-line pb-2">
                    {groups.map((group) => {
                      const choosable = canDecide && group.items.some(batchable);
                      return (
                        <section key={group.name} aria-label={group.name}>
                          <GroupHeader
                            name={group.name}
                            why={group.why}
                            count={group.items.length}
                            action={
                              choosable && group.items.filter(batchable).length > 1 ? (
                                <Button
                                  size="sm"
                                  variant="ghost"
                                  icon={<ListChecks className="size-4" />}
                                  aria-pressed={selecting}
                                  onClick={() => {
                                    setSelecting((on) => !on);
                                    setSelected(new Set());
                                  }}
                                >
                                  {selecting ? 'Done choosing' : 'Choose several'}
                                </Button>
                              ) : null
                            }
                          />
                          <ul>
                            {group.items.map((item) => (
                              <li key={item.id} className="row-motion" data-motion={item.leaving ? 'out' : item.entering ? 'in' : undefined}>
                                <div>
                                  <Row
                                    approval={item}
                                    business={businessOf(item.lane)}
                                    color={colorOf(item.lane)}
                                    focused={item.id === (flow.done?.approval.id ?? focused?.id)}
                                    changed={changed.has(item.id)}
                                    selecting={selecting}
                                    selected={selected.has(item.id)}
                                    onFocus={() => open(item.id)}
                                    onSelect={(checked) => toggle(item.id, checked)}
                                  />
                                </div>
                              </li>
                            ))}
                          </ul>
                        </section>
                      );
                    })}
                    {brands.length ? (
                      <section aria-label="Brand changes">
                        <GroupHeader name="Brand changes" why="An agent proposes a change to a brand. Only you decide it." count={brands.length} />
                        <ul>
                          {brands.map((item) => (
                            <li key={item.id}>
                              <BrandRow proposal={item} focused={item.id === focusedBrand?.id} onFocus={() => open(item.id)} />
                            </li>
                          ))}
                        </ul>
                      </section>
                    ) : null}
                    {codeMotion.rows.length ? (
                      <section aria-label="Login codes">
                        <GroupHeader name="Login codes" why="A site asked for a code. Only you can type it." count={openCodes.length} />
                        <ul>
                          {codeMotion.rows.map((item) => (
                            <li key={item.id} className="row-motion" data-motion={item.leaving ? 'out' : item.entering ? 'in' : undefined}>
                              <div>
                                <CodeRow card={item} business={businessOf(item.lane)} focused={item.id === focusedCode?.id} onFocus={() => open(item.id)} />
                              </div>
                            </li>
                          ))}
                        </ul>
                      </section>
                    ) : null}
                    {questionMotion.rows.length ? (
                      <section aria-label="Questions">
                        <GroupHeader name="Questions" why="The agent waits for your answer." count={questions.length} />
                        <ul>
                          {questionMotion.rows.map((item) => (
                            <li key={item.id} className="row-motion" data-motion={item.leaving ? 'out' : item.entering ? 'in' : undefined}>
                              <div>
                                <QuestionRow
                                  question={item}
                                  business={item.lane ? businessOf(item.lane) : ''}
                                  askedBy={whoOf(item.asked_by)}
                                  focused={item.id === focusedQuestion?.id}
                                  onFocus={() => open(item.id)}
                                />
                              </div>
                            </li>
                          ))}
                        </ul>
                      </section>
                    ) : null}
                  </div>
                </Card>
              ) : null}
              {showRequest ? (
                <div className="min-w-0">
                  {narrow && hasList ? (
                    <Button variant="ghost" size="sm" icon={<ArrowLeft className="size-4" />} className="mb-3" onClick={() => setOpened(false)}>
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
                  ) : total === 0 ? (
                    <AllClear org={org} decided={decidedHere} canDecide={canDecide} />
                  ) : (
                    <Card>
                      <p className="p-6 text-14 text-muted">Nothing to decide right now. What waits is listed below.</p>
                    </Card>
                  )}
                  {narrow && opened && somethingOpen ? <div className="mt-6">{failedStrip}</div> : null}
                </div>
              ) : null}
            </div>
            {showQueue ? (
              <DecidedList
                org={org}
                items={decidedHere}
                nameOf={(lane) => ({ name: businessOf(lane) || null, color: colorOf(lane) })}
              />
            ) : null}
            <StoppedRuns org={org} runs={stopped} businessOf={businessOf} />
            <PlansToApprove org={org} plans={plans} businessOf={businessOf} />
            {hands.length ? (
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
            ) : null}
          </div>
        )}
      </QueryView>
      {chosen.length > 0 ? (
        <div className="sticky bottom-[calc(var(--tabbar-height)+env(safe-area-inset-bottom)+16px)] z-20 mt-6 flex animate-pop-in items-center gap-2 rounded-xl bg-inverse p-2 pl-4 text-on-inverse shadow-popover md:bottom-6">
          <span className="tabular min-w-0 flex-1 text-14">{batchWords(chosen).chosen}</span>
          <Button size="md" variant="inverse" onClick={() => setSelected(new Set())}>
            Clear
          </Button>
          <Button size="md" variant="inverse" icon={<X className="size-4" />} onClick={() => run(chosen, 'deny')}>
            {batchWords(chosen).no}
          </Button>
          <Button size="md" variant="inverse" icon={<Check className="size-4" />} onClick={() => approve(chosen)}>
            {batchWords(chosen).yes}
          </Button>
        </div>
      ) : null}
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
