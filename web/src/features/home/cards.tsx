/**
 * The pieces Home and Accounts are built from: an account as its site's mark with a state badge,
 * an account as a card with its picture, and a business as a card with a live picture of what it
 * is doing (visual.md P2 and P3).
 */
import { Link } from '@tanstack/react-router';
import { ChevronRight } from 'lucide-react';
import type { Lane, LaneAskFirst, TraceEvent } from '@/api/types';
import { AgentMark } from '@/components/brand/identity';
import { SiteMark } from '@/components/brand/site-mark';
import { StateBadge } from '@/components/ui/state-badge';
import { cn } from '@/lib/cn';
import { timeAgo } from '@/lib/format';
import type { PersonState } from '@/lib/states';
import { useMe } from '@/lib/use-principal';
import { AccountAskRow } from '@/features/ask-first/ask-first';
import { LiveDot, Picture, PictureChip } from './picture';
import { accountCondition, accountName, accountTitle, didSentence, lastLine, nextStep } from './words';

export const cardClass =
  'group/card relative flex flex-col overflow-hidden rounded-(--radius-card) border bg-bg shadow-(--shadow-card) ' +
  'transition-[box-shadow,border-color,transform] duration-(--duration-settle) ease-(--ease-out) ' +
  'hover:-translate-y-px hover:shadow-(--shadow-hover) motion-reduce:hover:translate-y-0';

/** Card border by state: a red or amber hairline when something is wrong, plain otherwise. */
export const stateBorder = (state: PersonState) =>
  state === 'stuck' ? 'border-(--stuck-line)' : state === 'waiting' ? 'border-(--wait-line)' : 'border-line hover:border-line-strong';

/**
 * The card's last line. Quiet on purpose: with five workspaces, five tinted bars shout. The glyph
 * and the card's hairline carry the state; the words stay in the page's own colours.
 */
const FOOTER: Record<PersonState, string> = {
  stuck: 'border-t border-line text-fg hover:bg-fill-1',
  waiting: 'border-t border-line text-fg hover:bg-fill-1',
  working: 'border-t border-line text-muted',
  good: 'border-t border-line text-muted',
  sleeping: 'border-t border-line text-muted',
};

/** The picture worth showing for an account: its screen while an agent is on it, else its last post. */
export function accountPicture(lane: Lane): { src: string | null; live: boolean; at: string | null } {
  const state = accountCondition(lane).state;
  if (lane.live_picture && (state === 'working' || state === 'waiting' || !lane.picture)) return { src: lane.live_picture, live: true, at: null };
  if (lane.picture) return { src: lane.picture.url, live: false, at: lane.picture.at };
  return { src: lane.live_picture ?? null, live: Boolean(lane.live_picture), at: null };
}

/** One account as a card, for Accounts and a business's desk: its picture, state, last deed and next step. */
export function AccountCard({ org, lane, last, canWatch, index = 0, workspaceName, ask }: { org: string; lane: Lane; last?: TraceEvent | null; canWatch: boolean; index?: number; workspaceName?: string; ask?: LaneAskFirst }) {
  const me = useMe();
  const condition = accountCondition(lane, me);
  const step = nextStep(lane);
  const picture = accountPicture(lane);
  const src = picture.live && !canWatch ? (lane.picture?.url ?? null) : picture.src;
  const live = Boolean(picture.live && canWatch && src === lane.live_picture);
  const name = accountName(lane);
  return (
    <article aria-label={name} className={cn(cardClass, stateBorder(condition.state), 'home-rise')} style={{ animationDelay: `${Math.min(index, 12) * 40}ms` }}>
      <div className="p-2 pb-0">
        <Picture
          src={src}
          site={lane.site?.key ?? lane.name}
          title={accountTitle(lane, workspaceName)}
          line={lastLine(last, lane.last_used, timeAgo)}
          refreshMs={live && condition.state === 'working' ? 20_000 : undefined}
          resting={condition.state === 'sleeping' && !live}
          zoom
          className="aspect-[16/10] rounded-(--radius-thumb) ring-1 ring-line"
        >
          <div className="absolute inset-x-2 bottom-2 flex items-end justify-end gap-2">
            {condition.state === 'working' ? (
              <PictureChip tone="live" className="mr-auto">
                <LiveDot />
                Working now
              </PictureChip>
            ) : null}
            {live ? <PictureChip tone="dark">Live</PictureChip> : picture.at ? <PictureChip tone="dark">{timeAgo(picture.at)}</PictureChip> : null}
          </div>
        </Picture>
      </div>
      <div className="flex items-center gap-3 px-4 pt-3">
        <SiteMark site={lane.site?.key ?? lane.name} size={24} label={false} />
        <h3 className="min-w-0 flex-1 truncate text-14 font-semibold">
          <Link to="/$org/lanes/$ws/$name" params={{ org, ws: lane.workspace, name: lane.name }} className="after:absolute after:inset-0 after:content-['']">
            {name}
          </Link>
        </h3>
        <StateBadge state={condition.state} label={condition.label} className="shrink-0" />
      </div>
      <p className="px-4 pt-1 text-13 text-muted">{condition.sentence}.</p>
      <div className="mt-auto px-4 pt-3 pb-3">
        {last ? (
          <p className="flex min-w-0 items-center gap-2 text-13">
            <AgentMark principal={last.principal} size={16} />
            <span className="min-w-0 flex-1 truncate text-muted">{didSentence(last)}</span>
            <span className="tabular shrink-0 text-12 text-faint">{timeAgo(last.at)}</span>
          </p>
        ) : lane.last_used ? (
          <p className="text-13 text-faint">Last used {timeAgo(lane.last_used)}</p>
        ) : (
          <p className="text-13 text-faint">Not used yet</p>
        )}
      </div>
      <AccountAskRow org={org} lane={lane} ask={ask} />
      {step ? (
        <Link
          to="/$org/lanes/$ws/$name"
          params={{ org, ws: lane.workspace, name: lane.name }}
          className={cn('relative z-10 flex min-h-11 items-center gap-2 px-4 text-13 font-medium transition-colors duration-(--duration-fast)', FOOTER[condition.state])}
        >
          <span className="min-w-0 flex-1 truncate">{step.label}</span>
          <ChevronRight className="size-4 opacity-70 transition-transform duration-(--duration-base) group-hover/card:translate-x-0.5" />
        </Link>
      ) : null}
    </article>
  );
}
