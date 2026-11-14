/**
 * The shape of a day: every line is one thing done, and the few that stopped at a person drop out
 * of the stream as far as their yes is strong. It is the same day whatever the rows say, so moving
 * a row re-draws it at once, and pointing at a stop before choosing it shows what that would mean.
 */
import { useMemo, useState } from 'react';
import type { ConfirmDay } from '@/api/types';
import { SiteMark } from '@/components/brand/site-mark';
import { cn } from '@/lib/cn';
import type { DayShape, PlacedStop, Rung } from '@/lib/confirm';
import { RUNG_WORDS, RUNGS, yesSentence } from '@/lib/confirm';
import { RungIcon } from './marks';

const HEIGHT = 216;
const STREAM = 36;
/** Where each rung sits. The stronger the yes, the further it falls. */
const LANE: Record<Rung, number> = { auto: STREAM, chat: 96, signed_in: 140, present: 184 };
const DAY = 1440;

const clockOf = (minute: number) => `${String(Math.floor(minute / 60)).padStart(2, '0')}:${String(minute % 60).padStart(2, '0')}`;

const things = (count: number) => (count === 1 ? '1 thing' : `${count} things`);

/** Every thing done as a line. What goes out is taller than what only looked. */
function Ticks({ shape }: { shape: DayShape }) {
  const looks = shape.flowing.filter((tick) => tick.kind === 'look');
  const out = useMemo(() => [...shape.flowing.filter((tick) => tick.kind === 'commit'), ...shape.held], [shape.flowing, shape.held]);
  const drop = shape.heldRung ? LANE[shape.heldRung] - STREAM : 0;
  return (
    <svg aria-hidden="true" className="absolute inset-0 size-full" viewBox={`0 0 ${DAY} ${HEIGHT}`} preserveAspectRatio="none">
      <g className="yes-ticks-look">
        {looks.map((tick, index) => (
          <line key={index} x1={tick.minute} x2={tick.minute} y1={STREAM - 9} y2={STREAM + 9} vectorEffect="non-scaling-stroke" />
        ))}
      </g>
      <g className="yes-ticks-out" style={{ transform: `translateY(${drop}px) scaleY(${drop ? 0.5 : 1})` }} data-held={drop > 0 || undefined}>
        {out.map((tick, index) => (
          <line key={index} x1={tick.minute} x2={tick.minute} y1={STREAM - 18} y2={STREAM + 18} vectorEffect="non-scaling-stroke" />
        ))}
      </g>
    </svg>
  );
}

function Stop({ stop, chosen, dim, onChoose }: { stop: PlacedStop; chosen: boolean; dim: boolean; onChoose: () => void }) {
  const y = LANE[stop.rung];
  return (
    <button
      type="button"
      className="yes-stop-pin"
      data-chosen={chosen || undefined}
      data-dim={dim || undefined}
      data-no={stop.said === 'no' || undefined}
      style={{ left: `${(stop.minute / DAY) * 100}%`, '--y': `${y}px`, '--stem': (y - STREAM - 18) / HEIGHT } as React.CSSProperties}
      aria-label={`${clockOf(stop.minute)}: ${stop.title}. ${RUNG_WORDS[stop.rung].word}.`}
      aria-pressed={chosen}
      onClick={onChoose}
      onPointerEnter={(event) => event.pointerType === 'mouse' && onChoose()}
      onFocus={onChoose}
    >
      <span className="yes-stop-stem" aria-hidden="true" />
      <span className="yes-stop-face">
        <SiteMark site={stop.site} size={14} label={false} />
      </span>
    </button>
  );
}

/** One line, always there, so pointing at a stop never moves the page. */
function StopLine({ stop }: { stop: PlacedStop | null }) {
  if (!stop) return <p className="min-h-12 pt-1 text-13 text-muted">Nothing stopped for a person on this day.</p>;
  const verdict = stop.said === 'yes' ? yesSentence(stop.who, stop.method, stop.where) : `${stop.who} said no, so it didn't go out`;
  return (
    <div key={stop.id} className="flex min-h-12 animate-fade-in items-start gap-3">
      <span className="tabular w-11 shrink-0 pt-px text-13 text-faint">{clockOf(stop.minute)}</span>
      <div className="min-w-0 flex-1">
        <p className="text-14 font-medium text-pretty">{stop.title}</p>
        <p className="text-13 text-pretty text-muted">{verdict}.</p>
      </div>
      <span className="hidden shrink-0 items-center gap-2 text-13 whitespace-nowrap text-muted sm:flex">
        <RungIcon rung={stop.rung} className="size-4 text-faint" />
        {RUNG_WORDS[stop.rung].word}
      </span>
    </div>
  );
}

interface Props {
  day: Pick<ConfirmDay, 'label' | 'date'>;
  shape: DayShape;
  /** Beside the sentence: what a change would do to a day like this one. */
  note: React.ReactNode;
  /** The rung being pointed at in the rows below. */
  lit: Rung | null;
}

export function DayShapeView({ day, shape, note, lit }: Props) {
  const [chosenId, setChosenId] = useState<string | null>(null);
  const first = shape.stops.find((stop) => stop.rung === 'present') ?? shape.stops[0] ?? null;
  const chosen = shape.stops.find((stop) => stop.id === chosenId) ?? first;
  const date = new Date(`${day.date}T12:00:00`).toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long' });
  return (
    <section aria-label={`The shape of ${day.label.toLowerCase()}`} className="board-surface registered rounded-xl border border-board-line">
      <div className="p-6 pb-4">
        <p className="silk text-faint">
          {day.label} · {date}
        </p>
        <h3 className="tabular mt-2 max-w-[34em] text-24 font-semibold text-balance">
          {things(shape.total)} got done. {shape.own} went on their own, and {shape.stopped} stopped for a person.
        </h3>
        <p aria-live="polite" className="mt-1 min-h-5 text-13 text-muted">
          {note}
        </p>
      </div>
      <div className="grid grid-cols-[minmax(0,1fr)] gap-x-4 px-6 sm:grid-cols-[132px_minmax(0,1fr)_32px]">
        <ol className="relative hidden sm:block" style={{ height: HEIGHT }} aria-hidden="true">
          {RUNGS.map((rung) => (
            <li key={rung} className={cn('absolute inset-x-0 flex -translate-y-1/2 items-center gap-2 text-13 whitespace-nowrap transition-colors duration-(--duration-fast) ease-standard', lit === rung ? 'text-fg' : 'text-muted')} style={{ top: LANE[rung] }}>
              <RungIcon rung={rung} className="size-4 shrink-0 text-faint" />
              {RUNG_WORDS[rung].word}
            </li>
          ))}
        </ol>
        <div className="relative" style={{ height: HEIGHT }}>
          {RUNGS.slice(1).map((rung) => (
            <span key={rung} aria-hidden="true" className="yes-lane" data-lit={lit === rung || undefined} style={{ top: LANE[rung] }} />
          ))}
          <Ticks shape={shape} />
          {shape.stops.map((stop) => (
            <Stop key={stop.id} stop={stop} chosen={stop.id === chosen?.id} dim={lit !== null && lit !== 'auto' && lit !== stop.rung} onChoose={() => setChosenId(stop.id)} />
          ))}
        </div>
        <ol className="relative hidden sm:block" style={{ height: HEIGHT }} aria-hidden="true">
          {RUNGS.map((rung) => (
            <li key={rung} className="tabular absolute inset-x-0 -translate-y-1/2 text-right text-13 text-muted" style={{ top: LANE[rung] }}>
              {shape.lanes[rung]}
            </li>
          ))}
        </ol>
        <div aria-hidden="true" className="tabular relative col-start-1 h-4 text-12 text-faint sm:col-start-2">
          {[0, 6, 12, 18].map((hour) => (
            <span key={hour} className="absolute" style={{ left: `${(hour / 24) * 100}%` }}>
              {hour === 0 ? 'Midnight' : `${String(hour).padStart(2, '0')}:00`}
            </span>
          ))}
        </div>
      </div>
      <div className="mt-4 border-t border-board-line px-6 py-4">
        <StopLine stop={chosen} />
      </div>
    </section>
  );
}
