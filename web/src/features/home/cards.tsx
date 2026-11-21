/**
 * An account as a card (spec-09, accounts.png): the site's mark on its tile, the name and the handle, the state as
 * a chip, one line on how it is, what it did last, whether it runs on its own or asks first, and the one thing it
 * needs, when there is one. The whole card opens the account; its one action sits above that link.
 */
import { Link } from '@tanstack/react-router';
import type { Lane, LaneAskFirst, TraceEvent } from '@/api/types';
import { SiteMark } from '@/components/brand/site-mark';
import { Button } from '@/components/ui/button';
import { cardLink } from '@/components/ui/card';
import { StateBadge } from '@/components/ui/state-badge';
import { cn } from '@/lib/cn';
import { dateTime, timeAgo } from '@/lib/format';
import type { PersonState } from '@/lib/states';
import { useMe } from '@/lib/use-principal';
import { AccountAskWord } from '@/features/ask-first/ask-first';
import { accountCondition, accountName, accountTitle, didSentence, nextStep } from './words';

/** A red or amber edge when something is wrong; the plain hairline otherwise. */
const EDGE: Partial<Record<PersonState, string>> = { stuck: 'border-bad-line hover:border-bad-line', waiting: 'border-warn-line hover:border-warn-line' };

/** "Posted a photo on Reddit · 5 h ago", or when it was last used, or that it has not been. */
function LastLine({ last, lane }: { last?: TraceEvent | null; lane: Lane }) {
  if (last) {
    return (
      <p className="flex min-w-0 text-secondary text-muted" title={`${didSentence(last)}, ${dateTime(last.at)}`}>
        <span className="min-w-0 truncate">{didSentence(last)}</span>
        <span className="shrink-0 whitespace-pre"> · {timeAgo(last.at).replace(/^Just now$/, 'just now')}</span>
      </p>
    );
  }
  return <p className="text-secondary text-muted">{lane.last_used ? `Last used ${timeAgo(lane.last_used).toLowerCase()}` : 'Not used yet'}</p>;
}

export function AccountCard({ org, lane, last, ask }: { org: string; lane: Lane; last?: TraceEvent | null; ask?: LaneAskFirst }) {
  const me = useMe();
  const condition = accountCondition(lane, me);
  const step = nextStep(lane);
  const name = accountName(lane);
  const handle = lane.account?.user_id ? accountTitle(lane) : null;
  const params = { org, ws: lane.workspace, name: lane.name };
  return (
    <article aria-label={name} className={cn(cardLink, 'group/card relative flex min-w-0 flex-col p-5', EDGE[condition.state])}>
      <div className="flex min-w-0 items-start gap-3">
        <SiteMark site={lane.site?.key ?? lane.name} size={40} tile label={false} className="rounded-lg bg-card shadow-lift" />
        <div className="min-w-0 flex-1">
          <h3 className="truncate text-card-title" title={name}>
            <Link to="/$org/lanes/$ws/$name" params={params} className="outline-none after:absolute after:inset-0 after:rounded-xl after:content-[''] focus-visible:after:outline-2 focus-visible:after:outline-offset-2 focus-visible:after:outline-(--focus-ring)">
              {name}
            </Link>
          </h3>
          {handle ? (
            <p className={cn('truncate text-muted', /\s/.test(handle) ? 'text-secondary' : 'text-code-caption')} title={handle}>
              {handle}
            </p>
          ) : null}
        </div>
        <StateBadge chip state={condition.state} label={condition.label} sentence={condition.sentence} className="max-w-[45%] shrink-0" />
      </div>
      <div className="mt-4 grid min-w-0 gap-1">
        <p className="text-body [overflow-wrap:anywhere]">{condition.sentence}.</p>
        <LastLine last={last} lane={lane} />
      </div>
      <div className="mt-auto flex min-h-12 items-end justify-between gap-3">
        <span className="flex min-h-8 min-w-0 items-center text-muted">
          <AccountAskWord org={org} ask={ask} />
        </span>
        {step ? (
          <Button asChild size="sm" className="relative z-10 shrink-0">
            <Link to="/$org/lanes/$ws/$name" params={params}>
              {step.label}
            </Link>
          </Button>
        ) : null}
      </div>
    </article>
  );
}
