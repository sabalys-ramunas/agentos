/**
 * The pieces Home and Accounts are built from: an account as its site's mark with a state badge,
 * an account as a card with its picture, and a business as a card with a live picture of what it
 * is doing (visual.md P2 and P3).
 */
import { Link } from '@tanstack/react-router';
import { ChevronRight } from 'lucide-react';
import type { Lane, LaneAskFirst, TraceEvent, Workspace } from '@/api/types';
import { AgentMark, BusinessMark } from '@/components/brand/identity';
import { SiteMark } from '@/components/brand/site-mark';
import { Tooltip } from '@/components/ui/menu';
import { StateBadge, StateGlyph } from '@/components/ui/state-badge';
import { cn } from '@/lib/cn';
import { computerName } from '@/lib/computers';
import { timeAgo } from '@/lib/format';
import type { PersonState } from '@/lib/states';
import { useMe } from '@/lib/use-principal';
import { AccountAskRow } from '@/features/ask-first/ask-first';
import { LiveDot, Picture, PictureChip } from './picture';
import { accountCondition, accountName, accountTitle, businessSentence, didSentence, lastLine, nextStep } from './words';

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

/** One account as its site's mark, with a badge only when something is going on. */
export function AccountTile({ org, lane, size = 32 }: { org: string; lane: Lane; size?: 32 | 40 }) {
  const me = useMe();
  const condition = accountCondition(lane, me);
  const name = accountName(lane);
  return (
    <Tooltip label={`${name}: ${condition.sentence.charAt(0).toLowerCase()}${condition.sentence.slice(1)}`}>
      <Link
        to="/$org/lanes/$ws/$name"
        params={{ org, ws: lane.workspace, name: lane.name }}
        aria-label={`${name}, ${condition.sentence}`}
        data-state={condition.state}
        className={cn(
          'relative inline-flex shrink-0 rounded-[9px] transition-transform duration-(--duration-base) ease-standard hover:-translate-y-px',
          condition.state === 'working' && 'ring-2 ring-live ring-offset-2 ring-offset-bg',
        )}
      >
        <SiteMark
          site={lane.site?.key ?? lane.name}
          size={size}
          tile
          label={false}
          className={cn('bg-bg', condition.state === 'sleeping' && 'opacity-45 grayscale')}
        />
        {condition.state === 'working' ? (
          <span className="absolute -top-1 -right-1 rounded-full bg-bg p-px">
            <LiveDot />
          </span>
        ) : condition.state === 'waiting' || condition.state === 'stuck' ? (
          <span className="absolute -top-1.5 -right-1.5 rounded-full bg-bg p-px">
            <StateGlyph state={condition.state} size={14} />
          </span>
        ) : null}
      </Link>
    </Tooltip>
  );
}

/** The picture worth showing for an account: its screen while an agent is on it, else its last post. */
export function accountPicture(lane: Lane): { src: string | null; live: boolean; at: string | null } {
  const state = accountCondition(lane).state;
  if (lane.live_picture && (state === 'working' || state === 'waiting' || !lane.picture)) return { src: lane.live_picture, live: true, at: null };
  if (lane.picture) return { src: lane.picture.url, live: false, at: lane.picture.at };
  return { src: lane.live_picture ?? null, live: Boolean(lane.live_picture), at: null };
}

/** The account a business card shows: the one at work, then the one in trouble, then the newest. */
export function featuredLane(workspace: Workspace): Lane | null {
  const lanes = workspace.lanes;
  const by = (state: PersonState) => lanes.find((lane) => accountCondition(lane).state === state);
  const newest = [...lanes].filter((lane) => lane.picture).sort((a, b) => (b.picture?.at ?? '').localeCompare(a.picture?.at ?? ''))[0];
  return by('working') ?? by('stuck') ?? by('waiting') ?? newest ?? lanes.find((lane) => lane.live_picture) ?? lanes[0] ?? null;
}

function badgeFor(workspace: Workspace, state: PersonState, label?: string): { state: PersonState; label?: string } {
  if (state === 'waiting') {
    const count = workspace.needs_you ?? 0;
    return { state, label: count ? `${count} for you` : undefined };
  }
  // Working now is activity, not health: the card says All good and the account carries the pink.
  if (state === 'working') return { state: 'good' };
  return { state, label };
}

export interface BusinessCardProps {
  org: string;
  workspace: Workspace;
  /** The newest thing done in this business, from the feed. */
  last?: TraceEvent | null;
  /** "Instagram post at 19:00". */
  next?: string | null;
  /** May this person see an account's screen right now? Viewers and approvers may not. */
  canWatch: boolean;
  index?: number;
  /** The feed, for what the pictured account did last. */
  stories?: TraceEvent[];
}

export function BusinessCard({ org, workspace, last, next, canWatch, index = 0, stories = [] }: BusinessCardProps) {
  const me = useMe();
  const line = businessSentence(workspace, { asks: Math.max(0, (workspace.needs_you ?? 0) - (workspace.counts?.waiting ?? 0)), next, me });
  const badge = badgeFor(workspace, line.state, line.label);
  const featured = featuredLane(workspace);
  const picture = featured ? accountPicture(featured) : null;
  const src = picture?.live && !canWatch ? (featured?.picture?.url ?? null) : (picture?.src ?? null);
  const live = Boolean(picture?.live && canWatch && src === featured?.live_picture);
  const featuredState = featured ? accountCondition(featured).state : 'sleeping';
  const computers = [...new Set(workspace.lanes.map((lane) => lane.node).filter(Boolean))].map(computerName);
  const accounts = workspace.lanes.length;
  const footerTarget = line.state === 'stuck' || line.state === 'waiting';

  return (
    <article
      aria-label={workspace.name}
      className={cn(cardClass, stateBorder(line.state), 'home-rise')}
      style={{ animationDelay: `${120 + index * 60}ms` }}
    >
      <div className="p-2 pb-0">
        <Picture
          src={src}
          site={featured?.site?.key ?? featured?.name ?? 'web'}
          title={featured ? accountTitle(featured, workspace.name) : workspace.name}
          line={featured ? lastLine(stories.find((item) => item.lane === featured.id), featured.last_used, timeAgo) : null}
          refreshMs={live && featuredState === 'working' ? 20_000 : undefined}
          resting={featuredState === 'sleeping' && !live}
          zoom
          className="aspect-[16/10] rounded-(--radius-thumb) ring-1 ring-line"
        >
          {featured ? (
            <div className="absolute inset-x-2 bottom-2 flex items-end justify-between gap-2">
              {featuredState === 'working' ? (
                <PictureChip tone="live">
                  <LiveDot />
                  Working on {accountName(featured)}
                </PictureChip>
              ) : featuredState === 'waiting' || featuredState === 'stuck' ? (
                <PictureChip>
                  <StateGlyph state={featuredState} size={14} />
                  {accountName(featured)}
                </PictureChip>
              ) : (
                <PictureChip>
                  <SiteMark site={featured.site?.key ?? featured.name} size={14} label={false} />
                  {accountName(featured)}
                </PictureChip>
              )}
              {live ? (
                <PictureChip tone="dark">Live</PictureChip>
              ) : picture?.at ? (
                <PictureChip tone="dark">{timeAgo(picture.at)}</PictureChip>
              ) : null}
            </div>
          ) : null}
        </Picture>
      </div>

      <div className="flex items-start gap-3 px-4 pt-4">
        <BusinessMark name={workspace.name} color={workspace.color} size={40} className="rounded-[10px]" />
        <div className="min-w-0 flex-1">
          <h3 className="truncate text-16 font-semibold">
            <Link to="/$org/accounts" params={{ org }} search={{ workspace: workspace.id }} className="inline-flex min-h-8 max-w-full items-center after:absolute after:inset-0 after:content-['']">
              {workspace.name}
            </Link>
          </h3>
          <p className="truncate text-13 text-faint">
            {accounts === 1 ? '1 account' : `${accounts} accounts`}
            {computers.length ? ` · ${computers.join(', ')}` : ''}
          </p>
        </div>
        <StateBadge state={badge.state} label={badge.label} className="mt-1 shrink-0" />
      </div>

      {/* Above the card's own link, so each account opens itself. */}
      <div className="relative z-10 flex flex-wrap gap-2 px-4 pt-4">
        {workspace.lanes.map((lane) => (
          <AccountTile key={lane.id} org={org} lane={lane} />
        ))}
      </div>

      <div className="mt-auto px-4 pt-4 pb-3">
        {last ? (
          <p className="flex min-w-0 items-center gap-2 text-13">
            <AgentMark principal={last.principal} size={16} />
            <span className="min-w-0 flex-1 truncate text-muted">{didSentence(last)}</span>
            <span className="tabular shrink-0 text-12 text-faint">{timeAgo(last.at)}</span>
          </p>
        ) : (
          <p className="text-13 text-faint">Nothing done yet today.</p>
        )}
      </div>

      {footerTarget ? (
        <Link
          to="/$org/needs-you"
          params={{ org }}
          className={cn('relative z-10 flex min-h-11 items-center gap-2 px-4 text-13 transition-colors duration-(--duration-fast)', FOOTER[line.state])}
        >
          <StateGlyph state={line.state} size={16} />
          <span className="min-w-0 flex-1 truncate">{line.text}</span>
          <ChevronRight className="size-4 opacity-70 transition-transform duration-(--duration-base) group-hover/card:translate-x-0.5" />
        </Link>
      ) : (
        <p className={cn('flex min-h-11 items-center gap-2 px-4 text-13', FOOTER[line.state])}>
          <StateGlyph state={line.state === 'working' ? 'working' : line.state} size={16} />
          <span className="min-w-0 flex-1 truncate">{line.text}</span>
        </p>
      )}
    </article>
  );
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
