/**
 * Home's window (Dawn's Full, web/DESIGN.md "Home"): the Dawn picture with the greeting, the sentence and the
 * line under it in white, and what needs the person as glass cards on its lower edge, one action each.
 *
 * The cards open the item on Needs you. Deciding happens there, where the arming delay, the second step for
 * money and the irreversible, step-up and Undo live: Home never decides on one click.
 */
import { Link } from '@tanstack/react-router';
import { ArrowRight, Check, MessageCircleQuestion } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import type { ReactNode } from 'react';
import type { Workspace } from '@/api/types';
import { Button } from '@/components/ui/button';
import { KINDS } from '@/components/ui/kind';
import type { AnyKind } from '@/components/ui/kind';
import { MarkGlyph } from '@/components/ui/misc';
import { Skeleton } from '@/components/ui/states';
import { AppTile } from '@/components/ui/tile';
import type { TileTone } from '@/components/ui/tile';
import { cn } from '@/lib/cn';
import { plural } from '@/lib/format';
import { principalName } from '@/lib/principal';
import { IfYes } from '@/components/ui/if-yes';
import { IfYesValue, brandIfYes, ifYesWords, tileOf } from '@/features/needs-you/if-yes';
import { waitedShort } from '@/features/needs-you/queue-words';
import { rowTitleOf, whereOf } from '@/features/needs-you/words';
import dawn from '@/assets/dawn/dawn-1600.webp';
import { waitingOrder } from './home-needs';
import type { Waiting } from './home-needs';
import { SentenceLine, inline } from './sentence-line';
import type { homeSentence } from './sentence';

/** "Good morning", "Good afternoon" or "Good evening", by this browser's hour. */
export function greeting(now: number): string {
  const hour = new Date(now).getHours();
  return hour < 5 ? 'Good evening' : hour < 12 ? 'Good morning' : hour < 18 ? 'Good afternoon' : 'Good evening';
}

/** The tile of a kind of ask: its hue and its icon. Things that cost money are amber, what goes out pink. */
const TILE_TONE: Record<AnyKind, TileTone> = {
  push: 'violet',
  post: 'pink',
  brand: 'pink',
  payment: 'amber',
  email: 'blue',
  vm: 'ink',
  lane: 'ink',
  machine: 'ink',
  command: 'ink',
  account: 'blue',
  routine: 'teal',
  test: 'violet',
  design: 'pink',
  build: 'teal',
  server: 'blue',
};

function tileOfWaiting(item: Waiting): { tone: TileTone; icon: LucideIcon } {
  if (item.type === 'question') return { tone: 'blue', icon: MessageCircleQuestion };
  const kind: AnyKind = item.type === 'brand' ? 'brand' : tileOf(item.approval);
  return { tone: TILE_TONE[kind], icon: KINDS[kind].icon };
}

interface AskWords {
  title: string;
  line: string;
  /** What a yes does, in one line: "IF YES" and the effect. */
  ifYes: ReactNode;
}

function wordsOf(item: Waiting, workspaces: Workspace[]): AskWords {
  const business = (lane: string) => workspaces.find((workspace) => workspace.id === lane.split('/')[0])?.name ?? null;
  if (item.type === 'approval') {
    const where = whereOf(item.approval, business(item.approval.lane));
    return {
      title: rowTitleOf(item.approval, where),
      line: `${principalName(item.approval.requested_by)} asked ${waitedShort(item.at)} ago`,
      ifYes: (
        <IfYes title={ifYesWords(item.approval, where)}>
          <IfYesValue approval={item.approval} where={where} />
        </IfYes>
      ),
    };
  }
  if (item.type === 'brand') {
    const who = item.proposal.created_by ? principalName(item.proposal.created_by) : 'An agent';
    const yes = brandIfYes(item.proposal);
    return {
      title: `Brand change: ${item.proposal.display_name}`,
      line: `${who} asked ${waitedShort(item.at)} ago`,
      ifYes: (
        <IfYes title={yes}>
          <span title={yes} className="truncate">
            {yes}
          </span>
        </IfYes>
      ),
    };
  }
  return { title: item.question.question, line: `${principalName(item.question.asked_by)} asks · ${waitedShort(item.at)} ago`, ifYes: null };
}

/** One thing that waits: its tile, what it is, who asked and when, and Look. */
function AskCard({ org, item, workspaces, lead }: { org: string; item: Waiting; workspaces: Workspace[]; lead: boolean }) {
  const { tone, icon } = tileOfWaiting(item);
  const { title, line, ifYes } = wordsOf(item, workspaces);
  return (
    <li className="home-ask flex min-w-0 flex-col rounded-xl p-4">
      <div className="flex min-w-0 items-start gap-3">
        <AppTile tone={tone} icon={icon} />
        <div className="grid min-w-0 gap-1">
          <p title={title} className="line-clamp-2 text-label font-semibold text-fg">
            {title}
          </p>
          {ifYes}
          <p className="truncate text-secondary text-muted" title={line}>
            {line}
          </p>
        </div>
      </div>
      <div className="mt-auto flex justify-end pt-3">
        <Button asChild size="sm" variant={lead ? 'primary' : 'secondary'} aria-label={`Look: ${title}`}>
          <Link to="/$org/needs-you" params={{ org }} search={{ approval: item.id }}>
            Look
          </Link>
        </Button>
      </div>
    </li>
  );
}

const SHOWN = 3;

interface AsksProps {
  org: string;
  items: Waiting[];
  /** The one count, the same as the rail's; null while it is read. */
  total: number | null;
  workspaces: Workspace[];
}

/** What needs the person, on the window's lower edge: the heaviest three, or one calm line when nothing does. */
function Asks({ org, items, total, workspaces }: AsksProps) {
  const shown = waitingOrder(items).slice(0, SHOWN);
  const frame = 'relative z-10 mt-4 md:-mt-16 md:px-6';
  if (total === null) {
    return (
      <div className={cn(frame, 'grid gap-4 md:grid-cols-3')} aria-busy="true" aria-label="Loading what needs you">
        {[0, 1, 2].map((index) => (
          <div key={index} className={cn('home-ask grid gap-3 rounded-xl p-4', index > 0 && 'max-md:hidden')}>
            <Skeleton className="w-3/4" />
            <Skeleton className="w-1/2" />
            <Skeleton className="ml-auto h-8 w-20 rounded-full" />
          </div>
        ))}
      </div>
    );
  }
  if (!shown.length) {
    const nothing = total === 0;
    return (
      <div className={frame}>
        <div className="home-ask flex flex-wrap items-center gap-x-4 gap-y-3 rounded-xl p-4">
          <AppTile tone={nothing ? 'green' : 'violet'} icon={nothing ? Check : ArrowRight} />
          <div className="grid min-w-0 flex-1 gap-1">
            <p className="text-label font-semibold text-fg">{nothing ? 'Nothing needs you' : `${plural(total, 'thing')} ${total === 1 ? 'needs' : 'need'} you`}</p>
            <p className="text-secondary text-muted">
              {nothing
                ? 'When an agent asks for something it may not do alone, it waits here and on your phone.'
                : 'Nothing to decide here: what waits is an account that needs a hand, a run that stopped or a plan. Open Needs you to see it.'}
            </p>
          </div>
          {nothing ? null : (
            <Button asChild size="sm" variant="primary">
              <Link to="/$org/needs-you" params={{ org }}>
                Open Needs you
              </Link>
            </Button>
          )}
        </div>
      </div>
    );
  }
  return (
    <section aria-labelledby="home-needs" className={frame}>
      <h2 id="home-needs" className="sr-only">
        Needs you{total ? `, ${total} waiting` : ''}
      </h2>
      <ul className={cn('grid gap-4', shown.length === 1 ? 'md:grid-cols-2' : shown.length === 2 ? 'md:grid-cols-2' : 'md:grid-cols-3')}>
        {shown.map((item, index) => (
          <AskCard key={item.id} org={org} item={item} workspaces={workspaces} lead={index === 0} />
        ))}
      </ul>
      {total > shown.length ? (
        <p className="mt-2 flex justify-end text-secondary text-muted">
          <Link to="/$org/needs-you" params={{ org }} className="link inline-flex min-h-8 items-center gap-1">
            See all {total}
            <ArrowRight aria-hidden="true" className="size-4" />
          </Link>
        </p>
      ) : null}
    </section>
  );
}

interface HomeHeroProps {
  org: string;
  now: number;
  date: string;
  current: Workspace | null;
  workspaces: Workspace[];
  sentence: ReturnType<typeof homeSentence> | null;
  /** Given for a Home with workspaces; without them there is nothing to ask yet. */
  asks?: Omit<AsksProps, 'org' | 'workspaces'>;
  /** In place of the sentence, for Home before there is a workspace. */
  title?: ReactNode;
  lede?: ReactNode;
}

/** The window's picture and its scrim, behind whatever sits in the window. */
export function WindowPicture() {
  return (
    <>
      <img src={dawn} alt="" aria-hidden="true" draggable={false} fetchPriority="high" className="home-window-picture absolute inset-0 -z-20 size-full object-cover" />
      <div aria-hidden="true" className="home-window-scrim absolute inset-0 -z-10" />
    </>
  );
}

/** The window and what waits on its edge. */
export function HomeHero({ org, now, date, current, workspaces, sentence, asks, title, lede }: HomeHeroProps) {
  return (
    <>
      <header className="home-window relative isolate overflow-hidden rounded-2xl">
        <WindowPicture />
        <div className={cn('on-dawn px-5 pt-6 pb-8 sm:px-10 sm:pt-9', asks && 'md:pb-28')}>
          <p className="flex min-w-0 items-start gap-2 text-secondary">
            <MarkGlyph className="mt-1 size-4 shrink-0" />
            <span className="min-w-0">
              {greeting(now)} · {date}
              {workspaces.length ? (
                <>
                  {' · '}
                  {current ? (
                    <Link to="/$org/workspaces/$ws" params={{ org, ws: current.id }} className={inline}>
                      {current.name}
                    </Link>
                  ) : workspaces.length === 1 ? (
                    workspaces[0]!.name
                  ) : (
                    plural(workspaces.length, 'workspace')
                  )}
                </>
              ) : null}
            </span>
          </p>
          {title ? (
            <>
              <h1 className="mt-3 max-w-3xl text-page-title-compact text-balance sm:text-display">{title}</h1>
              {lede ? <p className="mt-3 max-w-3xl text-lede text-pretty">{lede}</p> : null}
            </>
          ) : sentence ? (
            <>
              <h1 className="mt-3 max-w-3xl text-page-title-compact text-balance sm:text-display">{sentence.lead}</h1>
              {sentence.line.length ? (
                <p className="mt-3 max-w-3xl text-lede text-pretty">
                  <SentenceLine org={org} line={sentence.line} />
                </p>
              ) : null}
            </>
          ) : (
            <div aria-busy="true">
              <h1 className="sr-only">Home</h1>
              <Skeleton className="mt-4 h-10 w-full max-w-2xl" />
              <Skeleton className="mt-4 h-5 w-full max-w-lg" />
            </div>
          )}
        </div>
      </header>
      {asks ? <Asks org={org} workspaces={workspaces} {...asks} /> : null}
    </>
  );
}
