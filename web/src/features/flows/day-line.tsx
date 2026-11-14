/**
 * The line through now: what ran on the left, what is coming on the right, each as the site it
 * touches. One glance answers "what happened and what is next", and it stays calm at any size
 * because tiles that would collide fold into a count. Status is a glyph on the tile, never colour
 * alone, and every tile is a link with its words in its name.
 */
import { Link } from '@tanstack/react-router';
import { useEffect, useMemo, useState } from 'react';
import { SiteMark } from '@/components/brand/site-mark';
import type { Flow, RoutineDay, RoutineDayMark } from '@/api/types';
import { Tooltip } from '@/components/ui/menu';
import { StateGlyph } from '@/components/ui/state-badge';
import { useNarrow } from '@/features/needs-you/use-narrow';
import { cn } from '@/lib/cn';
import { clock } from '@/lib/format';
import { OUTCOME, routineTitle, whenNext } from './routine-words';

const HOUR = 3_600_000;
const LANES = 2;

interface Placed {
  mark: RoutineDayMark;
  left: number;
  lane: number;
  /** How many more share this spot. */
  more: RoutineDayMark[];
}

function useMinute() {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 60_000);
    return () => clearInterval(timer);
  }, []);
  return now;
}

/** Each tile takes the first free lane; one with no free lane joins the tile beside it as a count. */
export function placeMarks(marks: RoutineDayMark[], from: number, to: number, width: number, tile: number): Placed[] {
  const placed: Placed[] = [];
  const ends = Array.from({ length: LANES }, () => -Infinity);
  const span = to - from;
  for (const mark of marks) {
    const at = Date.parse(mark.at);
    if (at < from || at > to) continue;
    const left = ((at - from) / span) * 100;
    const x = (left / 100) * width;
    const lane = ends.findIndex((end) => x >= end + tile + 4);
    if (lane >= 0) {
      ends[lane] = x;
      placed.push({ mark, left, lane, more: [] });
    } else {
      placed.at(-1)?.more.push(mark);
    }
  }
  return placed;
}

function words(mark: RoutineDayMark, name: string): string {
  const when = mark.status ? clock(mark.at) : whenNext(mark.at);
  if (!mark.status) return `${name}, next at ${when}`;
  if (mark.status === 'waiting_approval') return `${name}, ${when}, waiting for you`;
  if (mark.status === 'running') return `${name}, working now`;
  return `${name}, ${when}, ${(OUTCOME[mark.status]?.word ?? 'finished').toLowerCase()}`;
}

function Mark({ org, placed, flows, size }: { org: string; placed: Placed; flows: Map<string, Flow>; size: number }) {
  const { mark, left, lane, more } = placed;
  const flow = flows.get(mark.flow);
  const name = flow ? routineTitle(flow) : mark.flow;
  const label = words(mark, name) + (more.length ? `, and ${more.length} more` : '');
  const coming = mark.status === null;
  const hurt = mark.status === 'failed' || mark.status === 'partial';
  return (
    <Tooltip label={label}>
      <Link
        to="/$org/flows/$flow"
        params={{ org, flow: mark.flow }}
        aria-label={label}
        className="absolute -translate-x-1/2 rounded-[9px] transition-transform duration-(--duration-base) ease-standard hover:-translate-y-px hover:z-10"
        style={{ left: `${left}%`, bottom: 28 + lane * (size + 6) }}
      >
        <span className={cn('relative block', coming && 'rounded-[9px] outline-1 -outline-offset-1 outline-line-strong outline-dashed')}>
          <SiteMark site={mark.site} size={size as 24 | 32} tile label={false} className={cn('bg-bg', coming && 'opacity-55 grayscale')} />
          {hurt ? (
            <span className="absolute -top-1 -right-1 rounded-full bg-bg p-px">
              <StateGlyph state="stuck" size={14} />
            </span>
          ) : mark.status === 'waiting_approval' ? (
            <span className="absolute -top-1 -right-1 rounded-full bg-bg p-px">
              <StateGlyph state="waiting" size={14} />
            </span>
          ) : null}
          {more.length ? <span className="tabular absolute -right-1 -bottom-1 rounded-full bg-fg px-1 text-11 text-bg">+{more.length}</span> : null}
        </span>
      </Link>
    </Tooltip>
  );
}

export function DayLine({ org, day, flows }: { org: string; day: RoutineDay; flows: Flow[] }) {
  const narrow = useNarrow();
  const now = useMinute();
  // More ahead than behind: an evening still shows tomorrow morning.
  const from = now - (narrow ? 5 : 9) * HOUR;
  const to = now + (narrow ? 13 : 18) * HOUR;
  const needle = `${((now - from) / (to - from)) * 100}%`;
  const width = narrow ? 340 : 1040;
  const tile = narrow ? 24 : 32;
  const byId = useMemo(() => new Map(flows.map((flow) => [flow.id, flow])), [flows]);
  const placed = useMemo(() => placeMarks(day.marks, from, to, width, tile), [day.marks, from, to, width, tile]);
  const ticks = useMemo(() => {
    const step = (narrow ? 6 : 3) * HOUR;
    const first = Math.ceil(from / step) * step;
    return Array.from({ length: Math.floor((to - first) / step) + 1 }, (_, index) => first + index * step).filter((at) => Math.abs(at - now) > step / 2);
  }, [from, to, now, narrow]);
  const height = 28 + LANES * (tile + 6) + 16;

  return (
    <section aria-label="What ran, and what comes next" className="relative rounded-(--radius-card) border border-line bg-bg px-4 pt-4 pb-2 md:px-6">
      <div className="relative h-4 text-12 text-faint">
        <span>Earlier</span>
        <span className="absolute pl-2" style={{ left: needle }}>Coming up</span>
      </div>
      <div className="relative" style={{ height }}>
        {/* Only the past is solid: what is to come is a promise. */}
        <div aria-hidden="true" className="absolute inset-x-0 bottom-6 h-px bg-line" />
        <div aria-hidden="true" className="absolute right-0 bottom-6 h-px bg-bg [background-image:repeating-linear-gradient(90deg,var(--border-strong)_0_4px,transparent_4px_8px)]" style={{ left: needle }} />
        {ticks.map((at) => (
          <span key={at} aria-hidden="true" className="mono tabular absolute bottom-0 -translate-x-1/2 text-11 text-faint" style={{ left: `${((at - from) / (to - from)) * 100}%` }}>
            {clock(new Date(at).toISOString())}
          </span>
        ))}
        <div aria-hidden="true" className="absolute top-0 bottom-6 w-px bg-fg" style={{ left: needle }} />
        <span className="tabular absolute bottom-0 -translate-x-1/2 rounded-sm bg-fg px-1 text-11 font-medium text-bg" style={{ left: needle }}>Now</span>
        {placed.map((item) => (
          <Mark key={`${item.mark.flow}-${item.mark.at}`} org={org} placed={item} flows={byId} size={tile} />
        ))}
      </div>
    </section>
  );
}
