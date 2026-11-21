/**
 * The everyday History list: days as headings, one sentence per thing done with its proof
 * thumbnail and result, problems with what did not happen and what to do, and looking-only runs
 * folded into one line a day, a line per account behind Show.
 */
import { Link } from '@tanstack/react-router';
import { Card } from '@/components/ui/card';
import { Check, ChevronDown, CircleDashed, Eye, X } from 'lucide-react';
import type { KeyboardEvent } from 'react';
import { useMemo, useState } from 'react';
import type { HistoryDay, HistoryStory } from '@/api/types';
import { AgentMark } from '@/components/brand/identity';
import { SiteMark } from '@/components/brand/site-mark';
import { Button } from '@/components/ui/button';
import { StateGlyph } from '@/components/ui/state-badge';
import { StatusDot } from '@/components/ui/status';
import { cn } from '@/lib/cn';
import { yesShort } from '@/lib/confirm';
import { clock, plural, usd } from '@/lib/format';
import { useTechnical } from '@/lib/technical';
import { ArtifactGlyph } from './artifact';
import type { Entry } from './feed';
import { entriesOf } from './feed';
import { laneParams } from './run-story';
import type { Result } from './story';
import type { StoryView } from './story-view';
import { fromHistory, subjectOf } from './story-view';

const PILL: Record<Result, string> = {
  checked: 'bg-(--ok-soft) text-ok',
  unchecked: 'bg-hover text-muted',
  failed: 'bg-(--stuck-soft) text-bad',
  working: 'bg-live-soft text-live',
  waiting: 'bg-(--wait-soft) text-warn',
};

export function ResultPill({ view, className }: { view: Pick<StoryView, 'result' | 'label' | 'status'>; className?: string }) {
  // Stopped before it ran is not a problem to fix, so it reads quiet.
  const stopped = view.status === 'cancelled';
  return (
    <span className={cn('inline-flex h-6 shrink-0 items-center gap-1 rounded-full px-2 text-caption font-medium whitespace-nowrap', stopped ? PILL.unchecked : PILL[view.result], className)}>
      {stopped ? (
        <CircleDashed aria-hidden="true" className="size-3" />
      ) : view.result === 'checked' ? (
        <Check aria-hidden="true" className="size-3" strokeWidth={2.5} />
      ) : view.result === 'unchecked' ? (
        <CircleDashed aria-hidden="true" className="size-3" />
      ) : view.result === 'failed' ? (
        <X aria-hidden="true" className="size-3" strokeWidth={2.5} />
      ) : view.result === 'working' ? (
        <StatusDot tone="live" />
      ) : (
        <StateGlyph state="waiting" size={14} />
      )}
      {view.label}
    </span>
  );
}

/** Up and down walk the list, and the story beside it follows. */
function walk(event: KeyboardEvent<HTMLButtonElement>) {
  if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
  const feed = event.currentTarget.closest('[data-feed]');
  if (!feed) return;
  const buttons = [...feed.querySelectorAll<HTMLButtonElement>('button[data-story]')];
  const next = buttons[buttons.indexOf(event.currentTarget) + (event.key === 'ArrowDown' ? 1 : -1)];
  if (!next) return;
  event.preventDefault();
  next.focus();
  next.click();
}

/** What went out, as a small picture, or the site's own mark when there is no picture. */
export function Thumb({ view }: { view: StoryView }) {
  const [broken, setBroken] = useState(false);
  const src = view.result === 'checked' && view.proof?.screenshot?.startsWith('/api/') ? view.proof.screenshot : null;
  if (view.artifact) {
    return (
      <span className="relative flex size-14 shrink-0 items-center justify-center rounded-lg border border-line bg-inset">
        <ArtifactGlyph artifact={view.artifact} />
        <span className="absolute -right-1 -bottom-1 flex size-5 items-center justify-center rounded-md border border-line bg-card">
          <SiteMark site={view.siteKey} size={14} label={false} />
        </span>
      </span>
    );
  }
  if (!src || broken) {
    return (
      <span className="flex size-14 shrink-0 items-center justify-center rounded-lg border border-line bg-inset">
        <SiteMark site={view.siteKey} size={24} label={false} />
      </span>
    );
  }
  return (
    <span className="relative block size-14 shrink-0">
      <img src={src} alt="" loading="lazy" decoding="async" onError={() => setBroken(true)} className="size-full rounded-lg border border-line bg-inset object-cover object-top" />
      <span className="absolute -right-1 -bottom-1 flex size-5 items-center justify-center rounded-md border border-line bg-card">
        <SiteMark site={view.siteKey} size={14} label={false} />
      </span>
    </span>
  );
}

function WhoLine({ view }: { view: StoryView }) {
  return (
    <span className="flex min-w-0 items-center gap-2 text-secondary text-muted">
      <AgentMark principal={view.who.principal} name={view.who.name} size={14} />
      <span className="truncate">
        {view.who.name}
        {view.yes ? ` · ${yesShort(view.yes.by, view.yes.method)}` : view.why && /^(Asked|Approved)/.test(view.why) ? ` · ${view.why.charAt(0).toLowerCase()}${view.why.slice(1)}` : ''}
        {view.onItsOwn && !view.lookOnly ? ' · on its own' : ''}
        {view.worker ? ' · with the browser worker' : ''}
      </span>
    </span>
  );
}

interface StoryRowProps {
  org: string;
  entry: Entry;
  selected: boolean;
  onSelect: (id: string) => void;
}

export function StoryRow({ org, entry, selected, onSelect }: StoryRowProps) {
  const [technical] = useTechnical();
  const { view, times } = entry;
  const account = laneParams(org, view.lane);
  return (
    <li
      data-selected={selected || undefined}
      className={cn(
        'relative flex gap-3 border-b border-l-2 border-b-line px-4 py-3 [contain-intrinsic-size:auto_88px] [content-visibility:auto] last:border-b-0 md:gap-4',
        // The open story is selected: the violet tint with a 2 px violet edge, as a selected row everywhere.
        selected ? 'border-l-accent bg-accent-soft [&_.text-faint]:text-muted' : 'border-l-transparent hover:bg-hover',
      )}
    >
      <span className="tabular hidden w-11 shrink-0 pt-4 text-secondary text-faint md:block">{clock(view.at)}</span>
      <Thumb view={view} />
      <div className="min-w-0 flex-1">
        <div className="flex items-start gap-3">
          <div className="min-w-0 flex-1">
            <button
              type="button"
              data-story=""
              aria-label={`What happened: ${view.sentence}`}
              aria-current={selected || undefined}
              onClick={() => onSelect(view.id)}
              onKeyDown={walk}
              className="block text-left text-label after:absolute after:inset-0 after:content-['']"
            >
              {view.sentence}
            </button>
            <div className="mt-1 flex min-w-0 items-center gap-2">
              <WhoLine view={view} />
              <span className="tabular shrink-0 text-secondary text-faint md:hidden">· {clock(view.at)}</span>
            </div>
          </div>
          <ResultPill view={view} className="hidden sm:inline-flex" />
        </div>
        {view.note ? (
          <p className="mt-1 text-secondary text-muted">
            {view.note}
            {times > 1 ? ` This happened ${times} times, the last at ${clock(view.at)}.` : ''}
          </p>
        ) : null}
        {technical ? (
          <p className="mono mt-1 truncate text-caption text-faint">
            {view.lane} · {view.technical.what ?? '–'} · {usd(view.technical.cost.usd)} · {view.runId ?? view.id}
          </p>
        ) : null}
        <div className="mt-2 sm:hidden">
          <ResultPill view={view} />
        </div>
        {(view.result === 'failed' && view.status !== 'cancelled') || view.result === 'unchecked' ? (
          <div className="relative z-10 mt-2 flex flex-wrap gap-2">
            <Button size="sm" asChild>
              <Link to="/$org/lanes/$ws/$name" params={account}>
                {view.result === 'unchecked' ? 'Check it myself' : view.accountProblem ? 'Fix the account' : 'Watch the account'}
              </Link>
            </Button>
          </div>
        ) : null}
      </div>
    </li>
  );
}

const READS_SHOWN = 3;

/** The day's looking, in one line, with a line per account behind Show. */
export function ReadsBlock({ day }: { day: HistoryDay }) {
  const [open, setOpen] = useState(false);
  const [all, setAll] = useState(false);
  const reads = day.reads;
  if (!reads || reads.times === 0) return null;
  const shown = all ? reads.by_account : reads.by_account.slice(0, READS_SHOWN);
  const hidden = reads.by_account.length - shown.length;
  return (
    <section aria-label={`Looked only, ${day.label}`} className="border-t border-line bg-inset">
      <div className="flex items-center gap-3 px-4 py-2">
        <span aria-hidden="true" className="flex shrink-0 -space-x-1">
          {reads.by_account.slice(0, 3).map((item) => (
            <span key={item.lane} className="flex size-6 items-center justify-center rounded-full border border-line bg-card">
              <SiteMark site={item.site.key} size={14} label={false} />
            </span>
          ))}
        </span>
        <p className="min-w-0 flex-1 text-secondary text-muted">{reads.sentence.replace(/\.$/, '')}, and changed nothing.</p>
        <Button
          size="sm"
          variant="ghost"
          aria-expanded={open}
          onClick={() => setOpen(!open)}
          icon={<ChevronDown className={cn('size-4 transition-transform duration-(--duration-fast)', open && 'rotate-180')} />}
        >
          {open ? 'Hide' : 'Show'}
        </Button>
      </div>
      {open ? (
        <div className="pb-2">
          <ul aria-label={`Each account, ${day.label}`}>
            {shown.map((item) => (
              <li key={item.lane} className="flex items-center gap-3 px-4 py-1 text-secondary">
                <SiteMark site={item.site.key} size={16} label={false} className="shrink-0" />
                <span className="min-w-0 flex-1 truncate">
                  Checked {item.place} {item.times === 1 ? 'once' : item.times === 2 ? 'twice' : `${item.times} times`}
                </span>
                <span className="tabular shrink-0 text-caption text-faint">last at {clock(item.last_at)}</span>
              </li>
            ))}
          </ul>
          {hidden > 0 || all ? (
            <div className="px-4 pt-1">
              <Button size="sm" variant="ghost" onClick={() => setAll(!all)}>
                {all ? 'Show fewer' : `Show ${plural(hidden, 'more account')}`}
              </Button>
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

interface DaySectionProps {
  org: string;
  day: HistoryDay;
  selected: string | null;
  onSelect: (id: string) => void;
  /** Said when the day has stories but none of this kind. */
  emptyNote: string;
  /** Only one kind is shown, so the day's totals would not add up. */
  filtered?: boolean;
}

/** A busy day holds hundreds of stories; a page shows them a screenful at a time. */
export const PAGE_OF_STORIES = 25;

export function DaySection({ org, day, selected, onSelect, emptyNote, filtered = false }: DaySectionProps) {
  const entries = useMemo(() => entriesOf(day), [day]);
  const [limit, setLimit] = useState(PAGE_OF_STORIES);
  // The chosen story always stays in view, even when it sits further down the day.
  const chosen = selected ? entries.findIndex((entry) => entry.view.id === selected) : -1;
  const count = Math.max(limit, chosen + 1);
  const shown = entries.slice(0, count);
  const left = entries.length - shown.length;
  // The day's own totals are about everything; under a filter, say how many of this kind.
  const summary = filtered ? plural(day.stories.length, 'thing') : day.done > 0 ? day.summary : null;
  return (
    <Card role="region" aria-label={day.label} className="overflow-hidden">
      {/* Each day is one card, its name and totals at its head (as drawn). */}
      <h2 className="flex flex-wrap items-baseline gap-x-3 border-b border-line px-5 py-4 text-section-title">
        {day.label}
        {summary ? <span className="text-secondary font-normal text-faint">{summary}</span> : null}
      </h2>
      <div>
        {entries.length > 0 ? (
          <ul>
            {shown.map((entry) => (
              <StoryRow key={entry.view.id} org={org} entry={entry} selected={selected === entry.view.id} onSelect={onSelect} />
            ))}
          </ul>
        ) : (
          <p className="px-4 py-3 text-secondary text-muted">{emptyNote}</p>
        )}
        {left > 0 ? (
          <div className="flex items-center justify-between gap-3 border-t border-line px-4 py-2">
            <span className="text-secondary text-faint">{plural(left, 'more thing')} this day</span>
            <Button size="sm" variant="ghost" icon={<ChevronDown className="size-4" />} onClick={() => setLimit(count + PAGE_OF_STORIES)}>
              Show {Math.min(PAGE_OF_STORIES, left)} more
            </Button>
          </div>
        ) : null}
        <ReadsBlock day={day} />
      </div>
    </Card>
  );
}

/** Pink, on top: what an agent is doing right now, step by step, with a way to watch. */
export function WorkingNow({ org, stories }: { org: string; stories: HistoryStory[] }) {
  const first = stories[0];
  if (!first) return null;
  const view = fromHistory(first);
  return (
    <Card role="region" aria-label="Working now" className="border border-t-2 border-line border-t-accent p-4">
      <div className="flex flex-wrap items-start gap-3">
        <span className="hidden sm:block">
          <Thumb view={view} />
        </span>
        <div className="min-w-0 flex-1 basis-60">
          <p className="flex items-start gap-2 text-label">
            <StatusDot tone="live" className="mt-2 shrink-0" />
            <span>
              {subjectOf(view)} is {view.sentence.charAt(0).toLowerCase()}
              {view.sentence.slice(1)}
            </span>
          </p>
          {view.steps.length > 0 ? (
            <ol aria-label="Steps so far" className="mt-1 flex flex-wrap items-center gap-x-1 gap-y-1 text-secondary text-muted">
              {view.steps.map((step, index) => (
                <li key={step.id} className={cn('flex items-center gap-1', (step.status === 'running' || step.status === 'queued') && 'text-live')}>
                  {index > 0 ? (
                    <span aria-hidden="true" className="text-faint">
                      ›
                    </span>
                  ) : null}
                  {step.label}
                </li>
              ))}
            </ol>
          ) : null}
          {stories.length > 1 ? <p className="mt-1 text-secondary text-muted">and {plural(stories.length - 1, 'more thing')} working now</p> : null}
        </div>
        <Button size="sm" asChild className="shrink-0">
          <Link to="/$org/lanes/$ws/$name" params={laneParams(org, first.lane)}>
            <Eye aria-hidden="true" className="size-4" />
            Watch
          </Link>
        </Button>
      </div>
    </Card>
  );
}
