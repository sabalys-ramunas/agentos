/** The rows of the Needs you queue, how they group by what is at stake, and the words for a batch. */
import { Link } from '@tanstack/react-router';
import { Fingerprint } from 'lucide-react';
import type { Approval, Lane, Question } from '@/api/types';
import { AgentMark, BusinessMark } from '@/components/brand/identity';
import { SiteMark } from '@/components/brand/site-mark';
import { Count } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { IfYes } from '@/components/ui/if-yes';
import { StateGlyph } from '@/components/ui/state-badge';
import { Skeleton } from '@/components/ui/states';
import { Checkbox } from '@/components/ui/toggle';
import { cn } from '@/lib/cn';
import { needsProof } from '@/lib/confirm';
import { plural, timeAgo } from '@/lib/format';
import type { Motion } from '@/lib/row-motion';
import { laneCondition } from '@/lib/states';
import type { usePrincipalName } from '@/lib/use-principal';
import { useMe } from '@/lib/use-principal';
import { STAKE_ORDER, batchKind, kindOf, stakeOf } from './kinds';
import { handOf, rowTitleOf, whereOf } from './words';
import { IfYesValue, ifYesWords } from './if-yes';
import { queueRow, waitedShort } from './queue-words';

interface Group {
  name: string;
  why: string;
  items: Motion<Approval>[];
}

/** What is at stake and what kind it is, heaviest first: money, the irreversible, then emails, replies, forms, posts... */
export function groupByStake(approvals: Motion<Approval>[]): Group[] {
  return STAKE_ORDER.map((key) => {
    const items = approvals.filter((item) => stakeOf(item).key === key);
    const stake = items[0] ? stakeOf(items[0]) : null;
    return { name: stake?.name ?? '', why: stake?.why ?? '', items };
  }).filter((group) => group.items.length > 0);
}

/** Only routine kinds go in a batch: posts and replies. Never money, the irreversible, email or a blocked account. */
export const batchable = (approval: Approval) => batchKind(approval) && !approval.lane_state?.blocks;

/** The batch's words: "Post 3", "Send 2 replies", or plain yes and no for a mix. */
export function batchWords(items: Approval[]) {
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

export function ListSkeleton() {
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
export function Row({ approval, business, color, focused, changed, selecting, selected, onFocus, onSelect }: RowProps) {
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

export function QuestionRow({ question, business, askedBy, focused, onFocus }: { question: Question; business: string; askedBy: string; focused: boolean; onFocus: () => void }) {
  return (
    <button type="button" onClick={onFocus} aria-current={focused || undefined} className={queueRow(focused)}>
      <span className="grid min-w-0 flex-1 gap-1">
        <span className="flex min-w-0 items-center gap-2">
          <span title={question.question} className="truncate text-label">
            {question.repo_connect ? `Connect ${question.repo_connect.repo}` : question.question}
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
export function HandCard({ org, lane, business, nameOf }: { org: string; lane: Lane; business: string | null; nameOf: ReturnType<typeof usePrincipalName> }) {
  const hand = handOf(lane, business, (principal) => nameOf(principal));
  const me = useMe();
  const condition = laneCondition(lane, { me });
  return (
    <Card className="flex h-full items-start gap-3 p-4">
      <span className="relative shrink-0">
        <span className={cn('flex size-10 items-center justify-center rounded-lg', condition.state === 'stuck' ? 'bg-(--stuck-soft)' : 'bg-(--wait-soft)')}>
          <StateGlyph state={condition.state === 'stuck' ? 'stuck' : 'waiting'} size={20} />
        </span>
        <span className="absolute -right-1 -bottom-1 flex size-5 items-center justify-center rounded-full border border-line bg-card">
          <SiteMark site={lane.site?.key ?? lane.name} size={14} label={false} />
        </span>
      </span>
      <span className="grid min-w-0 flex-1 justify-items-start gap-2">
        <span className="min-w-0">
          <span className="block text-label break-words">{hand.title}</span>
          <span className="block text-secondary break-words text-faint">{hand.action === 'Log in' && lane.login_request?.why ? lane.login_request.why : condition.sentence}</span>
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

export function GroupHeader({ name, why, count, action }: { name: string; why?: string; count: number; action?: React.ReactNode }) {
  return (
    <div className="px-5 pt-5 pb-1">
      <div className="flex min-h-7 items-center gap-2">
        <h2 className="text-label font-semibold text-fg">{name}</h2>
        <Count value={count} />
        {action ? <span className="ml-auto">{action}</span> : null}
      </div>
      {why ? <p className="text-caption text-faint">{why}</p> : null}
    </div>
  );
}
