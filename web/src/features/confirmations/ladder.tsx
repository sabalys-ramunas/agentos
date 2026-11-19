/**
 * The rows of "What needs a yes", on one ladder. A bead sits on the rung each row asks for, and
 * what can never go lower is hatched under a lock, so the floor is something a person sees rather
 * than an error they meet. Pick a stop, press it, drag the bead or use the arrows: all the same.
 * Pointing at a stop shows the day as it would be, before anything changes.
 */
import { Lock } from 'lucide-react';
import { useRef, useState } from 'react';
import type { CSSProperties, KeyboardEvent, PointerEvent } from 'react';
import type { ConfirmRow } from '@/api/types';
import { SiteMark } from '@/components/brand/site-mark';
import { cn } from '@/lib/cn';
import type { Rung, RowWords } from '@/lib/confirm';
import { rungIndex, rungLine, RUNG_WORDS, RUNGS } from '@/lib/confirm';
import { RungIcon } from './marks';

const ROW_GRID = 'grid gap-x-6 gap-y-2 md:grid-cols-[minmax(0,260px)_minmax(0,1fr)]';

/** The ladder's own head: each rung in words, in the same columns as the beads below it. */
export function LadderHead({ chat, lit, onLit }: { chat: boolean; lit: Rung | null; onLit: (rung: Rung | null) => void }) {
  return (
    <div className={cn(ROW_GRID, 'hidden md:grid')}>
      <span />
      <ol className="grid grid-cols-4">
        {RUNGS.map((rung) => (
          <li key={rung} className="px-2 text-center" onPointerEnter={() => onLit(rung)} onPointerLeave={() => onLit(null)}>
            <span className={cn('mx-auto flex size-8 items-center justify-center rounded-full transition-colors duration-(--duration-fast) ease-standard', lit === rung ? 'bg-pressed text-fg' : 'text-muted')}>
              <RungIcon rung={rung} className="size-4" />
            </span>
            <span className="mt-1 block text-secondary font-medium">{RUNG_WORDS[rung].word}</span>
            <span className="mt-1 block text-caption text-pretty text-faint">{rungLine(rung, chat)}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}

interface TrackProps {
  label: string;
  rung: Rung;
  floor: Rung;
  /** What the system starts with, marked under its stop. */
  system?: Rung;
  fixed?: boolean;
  onPeek?: (rung: Rung | null) => void;
  onPick?: (rung: Rung) => void;
}

function Track({ label, rung, floor, system, fixed, onPeek, onPick }: TrackProps) {
  const low = rungIndex(floor);
  const movable = Boolean(onPick) && !fixed;
  const box = useRef<HTMLDivElement>(null);
  const [dragging, setDragging] = useState<Rung | null>(null);
  const shown = dragging ?? rung;
  const at = rungIndex(shown);

  const rungAt = (clientX: number): Rung => {
    const rect = box.current?.getBoundingClientRect();
    if (!rect) return shown;
    const index = Math.floor(((clientX - rect.left) / rect.width) * RUNGS.length);
    return RUNGS[Math.max(low, Math.min(RUNGS.length - 1, index))] ?? shown;
  };

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const step = event.key === 'ArrowRight' || event.key === 'ArrowUp' ? 1 : event.key === 'ArrowLeft' || event.key === 'ArrowDown' ? -1 : 0;
    if (!step || !movable) return;
    event.preventDefault();
    const next = RUNGS[Math.max(low, Math.min(RUNGS.length - 1, at + step))];
    if (next && next !== rung) {
      onPick?.(next);
      box.current?.querySelector<HTMLButtonElement>(`[data-rung="${next}"]`)?.focus();
    }
  };

  const grab = (event: PointerEvent<HTMLSpanElement>) => {
    if (!movable) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    setDragging(rung);
  };
  const drag = (event: PointerEvent<HTMLSpanElement>) => {
    if (dragging === null) return;
    const next = rungAt(event.clientX);
    if (next !== dragging) {
      setDragging(next);
      onPeek?.(next);
    }
  };
  const drop = () => {
    if (dragging === null) return;
    const landed = dragging;
    setDragging(null);
    onPeek?.(null);
    if (landed !== rung) onPick?.(landed);
  };

  return (
    <div>
      <div
        ref={box}
        role="radiogroup"
        tabIndex={-1}
        aria-label={label}
        aria-readonly={!movable || undefined}
        className="yes-track"
        data-movable={movable || undefined}
        data-fixed={!movable || undefined}
        data-dragging={dragging !== null || undefined}
        style={{ '--at': at, '--floor': low } as CSSProperties}
        onKeyDown={onKeyDown}
        onPointerLeave={() => dragging === null && onPeek?.(null)}
      >
        {low > 0 ? (
          <span className="yes-track-floor">
            <Lock aria-hidden="true" className="size-3" />
          </span>
        ) : null}
        <span aria-hidden="true" className="yes-track-rail" />
        <span aria-hidden="true" className="yes-track-reach" />
        {RUNGS.map((item, index) =>
          index < low ? null : (
            <button
              key={item}
              type="button"
              role="radio"
              data-rung={item}
              aria-checked={item === rung}
              aria-label={RUNG_WORDS[item].word}
              tabIndex={item === rung ? 0 : -1}
              disabled={!movable}
              className="yes-track-stop"
              style={{ '--i': index } as CSSProperties}
              onPointerEnter={(event) => event.pointerType === 'mouse' && dragging === null && onPeek?.(item)}
              onClick={() => item !== rung && onPick?.(item)}
            />
          ),
        )}
        <span aria-hidden="true" className="yes-track-bead" onPointerDown={grab} onPointerMove={drag} onPointerUp={drop} onPointerCancel={drop}>
          <RungIcon rung={shown} className="size-4" />
        </span>
      </div>
      <div className="relative hidden h-4 md:block" aria-hidden="true">
        {system && system !== shown ? (
          <span className="absolute -translate-x-1/2 animate-fade-in text-micro whitespace-nowrap text-faint transition-opacity duration-(--duration-base) ease-standard font-normal" style={{ left: `${((rungIndex(system) + 0.5) / RUNGS.length) * 100}%` }}>
            System default
          </span>
        ) : null}
      </div>
    </div>
  );
}

function Sites({ sites }: { sites: string[] }) {
  return (
    <span aria-hidden="true" className="flex shrink-0 gap-1">
      {sites.map((site) => (
        <SiteMark key={site} site={site} size={14} label={false} />
      ))}
    </span>
  );
}

interface RowProps {
  row: RowWords;
  rung: Rung;
  floor: Rung;
  system?: Rung;
  locked: boolean;
  /** Under the rung: what just happened, or what to know about where it sits. */
  note?: string | null;
  warn?: boolean;
  /** What this row really did on the day, in one line. */
  seen: string;
  onPeek?: (row: ConfirmRow, rung: Rung | null) => void;
  onPick?: (row: ConfirmRow, rung: Rung) => void;
  onHover?: (rung: Rung | null) => void;
}

export function LadderRow({ row, rung, floor, system, locked, note, warn, seen, onPeek, onPick, onHover }: RowProps) {
  return (
    <li className={cn(ROW_GRID, 'border-t border-line py-4')} onPointerEnter={() => onHover?.(rung)} onPointerLeave={() => onHover?.(null)}>
      <div className="min-w-0">
        <h4 className="flex items-center gap-2 text-label">
          {locked ? <Lock aria-label="Always asks" className="size-3 shrink-0 text-faint" /> : null}
          <span className="min-w-0">{row.title}</span>
        </h4>
        <p className="mt-1 text-secondary text-pretty text-muted">{row.because ?? row.covers}</p>
        <p className="mt-2 flex items-start gap-2 text-caption text-faint">
          <Sites sites={row.sites} />
          <span className="tabular min-w-0 text-pretty">{seen}</span>
        </p>
      </div>
      <div className="min-w-0 md:pt-1">
        <p className="mb-1 flex items-center gap-2 text-secondary md:hidden">
          <RungIcon rung={rung} className="size-4 text-faint" />
          {RUNG_WORDS[rung].word}
        </p>
        <Track
          label={`How strong a yes: ${row.title}`}
          rung={rung}
          floor={floor}
          system={system}
          fixed={!onPick}
          onPeek={onPeek ? (next) => onPeek(row.key, next) : undefined}
          onPick={onPick ? (next) => onPick(row.key, next) : undefined}
        />
        <p aria-live="polite" className={cn('mt-1 min-h-4 text-caption text-pretty', warn ? 'text-warn' : 'text-faint')}>
          {note}
        </p>
      </div>
    </li>
  );
}

/** Looking and drafting: a fact, drawn on the same ladder, with no control at all. */
export function FixedRow({ seen }: { seen: string }) {
  return (
    <li className={cn(ROW_GRID, 'border-t border-line py-4')}>
      <div className="min-w-0">
        <h4 className="flex items-center gap-2 text-label">
          <Lock aria-label="Nobody's to change" className="size-3 shrink-0 text-faint" />
          Looking and drafting
        </h4>
        <p className="mt-1 text-secondary text-pretty text-muted">Nothing leaves, so there is nothing to ask.</p>
        <p className="tabular mt-2 text-caption text-faint">{seen}</p>
      </div>
      <div className="min-w-0 md:pt-1">
        <div className="yes-track" data-fixed="" role="img" aria-label="On its own, always" style={{ '--at': 0, '--floor': 0 } as CSSProperties}>
          <span className="yes-track-floor" style={{ left: '25%', width: '75%', justifyContent: 'flex-start' }}>
            <Lock aria-hidden="true" className="size-3" />
          </span>
          <span aria-hidden="true" className="yes-track-bead">
            <RungIcon rung="auto" className="size-4" />
          </span>
        </div>
      </div>
    </li>
  );
}
