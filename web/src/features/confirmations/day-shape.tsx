/**
 * The shape of a day: every line is one thing done, and the few that stopped at a person drop out
 * of the stream as far as their yes is strong. It is the same day whatever the rows say, so moving
 * a row re-draws it at once, and pointing at a stop before choosing it shows what that would mean.
 */
import type { ConfirmDay } from '@/api/types';
import { Card } from '@/components/ui/card';
import { cn } from '@/lib/cn';
import type { DayShape, PlacedStop, Rung } from '@/lib/confirm';
import { RUNG_WORDS, RUNGS, yesSentence } from '@/lib/confirm';
import { RungIcon } from './marks';

const clockOf = (minute: number) => `${String(Math.floor(minute / 60)).padStart(2, '0')}:${String(minute % 60).padStart(2, '0')}`;

const things = (count: number) => (count === 1 ? '1 thing' : `${count} things`);

/** One line, always there, so pointing at a stop never moves the page. */
function StopLine({ stop }: { stop: PlacedStop | null }) {
  if (!stop) return <p className="min-h-12 pt-1 text-secondary text-muted">Nothing stopped for a person on this day.</p>;
  const verdict = stop.said === 'yes' ? yesSentence(stop.who, stop.method, stop.where) : `${stop.who} said no, so it didn't go out`;
  return (
    <div key={stop.id} className="flex min-h-12 items-start gap-3">
      <span className="tabular w-11 shrink-0 text-secondary text-faint">{clockOf(stop.minute)}</span>
      <div className="min-w-0 flex-1">
        <p className="text-label text-pretty text-fg">{stop.title}</p>
        <p className="text-secondary text-pretty text-muted">{verdict}.</p>
      </div>
      <span className="hidden shrink-0 items-center gap-2 text-secondary whitespace-nowrap text-muted sm:flex">
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
  const first = shape.stops.find((stop) => stop.rung === 'present') ?? shape.stops[0] ?? null;
  const date = new Date(`${day.date}T12:00:00`).toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long' });
  return (
    <Card role="region" aria-label={`The shape of ${day.label.toLowerCase()}`} className="shadow-lift">
      <div className="px-6 pt-5">
        <p className="text-eyebrow text-faint">
          {day.label} · {date}
        </p>
        <h3 className="tabular mt-2 text-section-title text-balance">
          {things(shape.total)} got done. {shape.own} went on their own, and {shape.stopped} stopped for a person.
        </h3>
        <dl className="mt-4 grid grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-4">
          {RUNGS.map((rung) => (
            <div key={rung} className={cn('grid gap-1 transition-opacity duration-(--duration-fast) ease-standard', lit && lit !== rung && 'opacity-60')}>
              <dd className="tabular order-1 text-page-title-compact">{shape.lanes[rung]}</dd>
              <dt className="order-2 flex items-center gap-2 text-secondary text-muted">
                <RungIcon rung={rung} className="size-4 shrink-0 text-faint" />
                {RUNG_WORDS[rung].word}
              </dt>
            </div>
          ))}
        </dl>
        <p aria-live="polite" className="mt-3 min-h-5 text-secondary text-faint">
          {note}
        </p>
      </div>
      <div className="mt-4 border-t border-line px-6 py-4">
        <StopLine stop={first} />
      </div>
    </Card>
  );
}
