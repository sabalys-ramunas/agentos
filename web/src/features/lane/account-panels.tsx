/**
 * The panels beside an account's live screen (spec-09, account.png): who is using it and what waits for a yes, its
 * last few things done, and whether it is safe to use. On the night band they are dark glass; under 1024 px, where
 * the band holds only the screen, they are plain cards under it. Each shows only when it has something to say.
 */
import { Link } from '@tanstack/react-router';
import { Inbox } from 'lucide-react';
import type { ReactNode } from 'react';
import { useActivity, useApprovals, useLaneQueue } from '@/api/hooks';
import type { Lane, TraceEvent } from '@/api/types';
import { Card } from '@/components/ui/card';
import { StatusDot } from '@/components/ui/status';
import { Skeleton } from '@/components/ui/states';
import { cn } from '@/lib/cn';
import { dateTime, timeAgo } from '@/lib/format';
import { runStatus } from '@/lib/labels';
import { useTechnical } from '@/lib/technical';
import { PLACES, RUN_WORDS } from '@/lib/vocabulary';
import { didSentence, isStory } from '@/features/home/words';
import { ownActionSentence, queueSentence } from './account-words';

/** A panel: dark glass on the band, a card under it. */
export function AccountPanel({ glass, title, id, action, children, className }: { glass: boolean; title: string; id: string; action?: ReactNode; children: ReactNode; className?: string }) {
  const body = (
    <section aria-labelledby={id} className={cn('grid content-start gap-3 px-5 py-4', className)}>
      <div className="flex min-h-7 items-center justify-between gap-3">
        <h2 id={id} className="text-card-title">
          {title}
        </h2>
        {action}
      </div>
      {children}
    </section>
  );
  return glass ? <div className="glass-night rounded-xl">{body}</div> : <Card>{body}</Card>;
}

/** Who holds the account, who waits behind it, and what waits for a person's yes. */
export function WhoPanel({ org, lane, glass }: { org: string; lane: Lane; glass: boolean }) {
  const queue = useLaneQueue(org, lane.workspace, lane.name).data;
  const pending = (useApprovals(org).data ?? []).filter((item) => item.lane === lane.id).length;
  const held = Boolean(queue && (queue.claim || queue.queue.length));
  if (!held && !pending) return null;
  const words = queue && held ? queueSentence(queue) : null;
  return (
    <AccountPanel glass={glass} id="account-who" title={held ? 'Who is using it' : 'Waiting for your yes'}>
      {words ? (
        <div className="grid gap-1 text-body">
          <p className="text-pretty">{words.lead}</p>
          {words.waiting ? <p className="text-secondary text-muted">{words.waiting}</p> : null}
        </div>
      ) : null}
      {pending ? (
        <Link to="/$org/needs-you" params={{ org }} className="link inline-flex items-center gap-2 justify-self-start text-secondary">
          <Inbox aria-hidden="true" className="size-4 shrink-0" />
          {pending === 1 ? 'One thing waits for your yes' : `${pending} things wait for your yes`}
        </Link>
      ) : null}
    </AccountPanel>
  );
}

function Row({ row }: { row: TraceEvent }) {
  const [technical] = useTechnical();
  // A change the person made is not a check on a site, so it carries no "Not checked" word.
  const own = ownActionSentence(row);
  const tone = runStatus[row.status].tone;
  return (
    <li className="grid grid-cols-[64px_minmax(0,1fr)] gap-3 border-t border-line py-3 first:border-t-0 first:pt-0 last:pb-0">
      <span className="tabular pt-px text-secondary text-faint" title={dateTime(row.at)}>
        {timeAgo(row.at)}
      </span>
      <span className="min-w-0">
        <span className={cn('block text-secondary text-pretty', technical && 'mono break-all')}>{technical ? row.summary : (own ?? didSentence(row))}</span>
        {own ? null : (
          <span className="mt-1 flex items-center gap-2 text-caption text-faint">
            <StatusDot tone={tone} />
            {RUN_WORDS[row.status].word}
          </span>
        )}
      </span>
    </li>
  );
}

/** What agents did here lately, and what the person changed (take over, freeze, pace, limits), newest first. */
export function HistoryPanel({ org, lane, glass, rows: count = 5 }: { org: string; lane: Lane; glass: boolean; rows?: number }) {
  const activity = useActivity(org, { lane: lane.id });
  const [technical] = useTechnical();
  const rows = (activity.data?.pages[0]?.items ?? []).filter((item) => technical || isStory(item) || ownActionSentence(item) !== null).slice(0, count);
  return (
    <AccountPanel
      glass={glass}
      id="account-history"
      title={PLACES.history.word}
      action={
        <Link to="/$org/activity" params={{ org }} search={{ lane: lane.id }} className="link text-secondary">
          All of it
        </Link>
      }
    >
      {activity.isPending ? (
        <div className="grid gap-3">
          <Skeleton />
          <Skeleton className="w-2/3" />
        </div>
      ) : rows.length ? (
        <ul>
          {rows.map((row) => (
            <Row key={row.id} row={row} />
          ))}
        </ul>
      ) : (
        <p className="text-secondary text-muted">Nothing done here yet. What agents do on this account appears here as it happens.</p>
      )}
    </AccountPanel>
  );
}
