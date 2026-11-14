/**
 * Needs you on Home (Mono): the first three things that wait, heaviest first, each with what a yes does
 * ("IF YES"), who asked and how long ago, and Look, which opens it on Needs you. Deciding happens there,
 * where the arming delay, the second step for money and the irreversible, step-up and Undo live: Home
 * never decides on one click.
 */
import { Link } from '@tanstack/react-router';
import { ArrowRight, Check, MessageCircleQuestion } from 'lucide-react';
import type { Approval, BrandProposal, Question, Workspace } from '@/api/types';
import { AgentMark } from '@/components/brand/identity';
import { Count } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardHeader } from '@/components/ui/card';
import { IfYes } from '@/components/ui/if-yes';
import { KindTile } from '@/components/ui/kind';
import { EmptyState, Skeleton } from '@/components/ui/states';
import { principalName } from '@/lib/principal';
import { IfYesValue, brandIfYes, ifYesWords, tileOf } from '@/features/needs-you/if-yes';
import { STAKE_ORDER, stakeOf } from '@/features/needs-you/kinds';
import { waitedShort } from '@/features/needs-you/queue-words';
import { rowTitleOf, whereOf } from '@/features/needs-you/words';

export type Waiting =
  | { type: 'approval'; id: string; at: string; approval: Approval }
  | { type: 'brand'; id: string; at: string; proposal: BrandProposal }
  | { type: 'question'; id: string; at: string; question: Question };

/** Heaviest first, as Needs you orders them: money, the irreversible, pushes ... then brand changes, then questions. */
export function waitingOrder(items: Waiting[]): Waiting[] {
  const weight = (item: Waiting) =>
    item.type === 'approval' ? STAKE_ORDER.indexOf(stakeOf(item.approval).key) : item.type === 'brand' ? STAKE_ORDER.length : STAKE_ORDER.length + 1;
  return [...items].sort((a, b) => weight(a) - weight(b) || a.at.localeCompare(b.at));
}

function Look({ org, id, label }: { org: string; id: string; label: string }) {
  return (
    <Button asChild size="sm" aria-label={`Look: ${label}`}>
      <Link to="/$org/needs-you" params={{ org }} search={{ approval: id }}>
        Look
        <ArrowRight aria-hidden="true" className="size-3.5" />
      </Link>
    </Button>
  );
}

function Item({ org, item, workspaces }: { org: string; item: Waiting; workspaces: Workspace[] }) {
  const business = (lane: string) => workspaces.find((workspace) => workspace.id === lane.split('/')[0])?.name ?? null;
  if (item.type === 'approval') {
    const { approval } = item;
    const where = whereOf(approval, business(approval.lane));
    const title = rowTitleOf(approval, where);
    return (
      <li className="flex items-start gap-3 px-5 py-4">
        <KindTile kind={tileOf(approval)} size="md" />
        <div className="grid min-w-0 flex-1 gap-1">
          <p title={title} className="truncate text-label">
            {title}
          </p>
          <IfYes title={ifYesWords(approval, where)}>
            <IfYesValue approval={approval} where={where} />
          </IfYes>
          <p className="flex min-w-0 items-center gap-2 text-caption text-faint">
            <AgentMark principal={approval.requested_by} size={16} />
            <span className="truncate" title={`${principalName(approval.requested_by)}${where.business ? ` · ${where.business}` : ''} · ${waitedShort(approval.created_at)} ago`}>
              {principalName(approval.requested_by)}
              {where.business ? ` · ${where.business}` : ''} · {waitedShort(approval.created_at)} ago
            </span>
          </p>
        </div>
        <Look org={org} id={approval.id} label={title} />
      </li>
    );
  }
  if (item.type === 'brand') {
    const { proposal } = item;
    const line = brandIfYes(proposal);
    return (
      <li className="flex items-start gap-3 px-5 py-4">
        <KindTile kind="design" size="md" />
        <div className="grid min-w-0 flex-1 gap-1">
          <p className="truncate text-label">Brand change: {proposal.display_name}</p>
          <IfYes title={line}>
            <span title={line} className="truncate">
              {line}
            </span>
          </IfYes>
          <p className="flex min-w-0 items-center gap-2 text-caption text-faint">
            {proposal.created_by ? <AgentMark principal={proposal.created_by} size={16} /> : null}
            <span className="truncate">
              {proposal.created_by ? principalName(proposal.created_by) : 'An agent'} · {waitedShort(proposal.created_at)} ago
            </span>
          </p>
        </div>
        <Look org={org} id={proposal.id} label={`Brand change: ${proposal.display_name}`} />
      </li>
    );
  }
  const { question } = item;
  return (
    <li className="flex items-start gap-3 px-5 py-4">
      <span aria-hidden="true" className="grid size-7 shrink-0 place-items-center rounded-md border border-line bg-card text-muted shadow-lift">
        <MessageCircleQuestion className="size-4" />
      </span>
      <div className="grid min-w-0 flex-1 gap-1">
        <p className="truncate text-label" title={question.question}>
          {question.question}
        </p>
        <p className="flex min-w-0 items-center gap-2 text-caption text-faint">
          <AgentMark principal={question.asked_by} size={16} />
          <span className="truncate">
            {principalName(question.asked_by)} asks · {waitedShort(question.created_at)} ago
          </span>
        </p>
      </div>
      <Look org={org} id={question.id} label={question.question} />
    </li>
  );
}

const SHOWN = 3;

interface HomeNeedsProps {
  org: string;
  items: Waiting[];
  /** The one count, the same as the rail's; null while it is read. */
  total: number | null;
  oldestAt: string | null;
  workspaces: Workspace[];
}

export function HomeNeeds({ org, items, total, oldestAt, workspaces }: HomeNeedsProps) {
  const shown = waitingOrder(items).slice(0, SHOWN);
  return (
    <Card role="region" aria-labelledby="home-needs" className="min-w-0">
      <CardHeader
        title={
          <>
            <span id="home-needs">Needs you</span>
            {total ? <Count value={total} tone="warn" /> : null}
          </>
        }
        meta={total === null ? null : total ? (oldestAt ? <span className="hidden sm:inline">the oldest for {waitedShort(oldestAt)}</span> : null) : 'nothing waiting'}
        action={
          total ? (
            <Link to="/$org/needs-you" params={{ org }} className="link inline-flex min-h-8 items-center gap-1 text-secondary whitespace-nowrap">
              {total > 1 ? `See all ${total}` : 'Open Needs you'}
              <ArrowRight aria-hidden="true" className="size-3.5" />
            </Link>
          ) : null
        }
      />
      {total === null ? (
        <div className="grid gap-3 p-5" aria-busy="true">
          <Skeleton className="w-2/3" />
          <Skeleton className="w-1/2" />
        </div>
      ) : shown.length ? (
        <ul className="divide-y divide-line">
          {shown.map((item) => (
            <Item key={item.id} org={org} item={item} workspaces={workspaces} />
          ))}
        </ul>
      ) : total > 0 ? (
        <p className="px-5 py-6 text-secondary text-muted">
          Nothing to decide here: what waits is an account that needs a hand, a run that stopped or a plan. Open Needs you to see it.
        </p>
      ) : (
        <EmptyState icon={<Check />} title="Nothing needs you" description="When an agent asks for something it may not do alone, it waits here and on your phone." className="py-10" />
      )}
    </Card>
  );
}
