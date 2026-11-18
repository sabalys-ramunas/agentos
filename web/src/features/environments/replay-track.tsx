/**
 * The replay's track (concept): one time axis with four rows under it. Pages as hairline segments, the
 * agent's calls as ticks (a tall one changed the page, a red one failed), a person's take-overs as bands
 * with the hand, and the proofs as small squares. The playhead is ink. The whole track is one slider:
 * press or drag to seek, arrows step a frame, Shift and an arrow jump to the next thing done, Home and End.
 * Pointing shows the frame under the pointer above the track, so a person can look before they jump.
 */
import { Hand, Image as ImageIcon } from 'lucide-react';
import { useRef, useState } from 'react';
import type { KeyboardEvent, PointerEvent } from 'react';
import { cn } from '@/lib/cn';
import { along, causeWords, frameAt, jump, offset, ticks } from './replay-model';
import type { ReplayModel } from './replay-model';

const pct = (value: number) => `${(value * 100).toFixed(3)}%`;
const ROWS = ['Page', 'Agent', 'Person', 'Proofs'] as const;

export function ReplayTrack({ model, index, onSeek, onToggle }: { model: ReplayModel; index: number; onSeek: (index: number) => void; onToggle: () => void }) {
  const box = useRef<HTMLDivElement>(null);
  const [hover, setHover] = useState<{ x: number; index: number } | null>(null);
  const [dragging, setDragging] = useState(false);
  const frame = model.frames[index];
  const at = (event: PointerEvent) => {
    const rect = box.current?.getBoundingClientRect();
    if (!rect || !rect.width) return null;
    const x = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
    return { x, index: frameAt(model, model.start + x * (model.end - model.start)) };
  };
  const onKey = (event: KeyboardEvent) => {
    const last = model.frames.length - 1;
    const moves: Record<string, () => number> = {
      ArrowRight: () => (event.shiftKey ? jump(model, index, 1) : Math.min(last, index + 1)),
      ArrowLeft: () => (event.shiftKey ? jump(model, index, -1) : Math.max(0, index - 1)),
      Home: () => 0,
      End: () => last,
      PageUp: () => jump(model, index, -1),
      PageDown: () => jump(model, index, 1),
    };
    if (event.key === ' ' || event.key === 'k') {
      event.preventDefault();
      onToggle();
      return;
    }
    const move = moves[event.key];
    if (!move) return;
    event.preventDefault();
    onSeek(move());
  };
  const hovered = hover ? model.frames[hover.index] : undefined;
  return (
    <div className="grid grid-cols-[56px_minmax(0,1fr)] gap-x-3 max-sm:grid-cols-[minmax(0,1fr)]">
      <div aria-hidden="true" className="grid grid-rows-[20px_24px_28px_24px] pt-5 text-caption text-faint max-sm:hidden">
        {ROWS.map((row) => (
          <span key={row} className="flex items-center">
            {row}
          </span>
        ))}
      </div>
      <div className="relative min-w-0">
        {hovered && hover && !dragging ? <Peek model={model} frame={hovered} x={hover.x} /> : null}
        <div
          ref={box}
          role="slider"
          tabIndex={0}
          aria-label="Replay position"
          aria-valuemin={0}
          aria-valuemax={Math.max(0, model.frames.length - 1)}
          aria-valuenow={index}
          aria-valuetext={frame ? `Frame ${index + 1} of ${model.frames.length}, ${offset(model, frame.t)}: ${causeWords(frame)}` : 'No frames'}
          aria-keyshortcuts="ArrowLeft ArrowRight Shift+ArrowLeft Shift+ArrowRight Home End Space"
          onKeyDown={onKey}
          onPointerDown={(event) => {
            event.currentTarget.setPointerCapture(event.pointerId);
            setDragging(true);
            const point = at(event);
            if (point) onSeek(point.index);
          }}
          onPointerMove={(event) => {
            const point = at(event);
            setHover(point);
            if (dragging && point) onSeek(point.index);
          }}
          onPointerUp={() => setDragging(false)}
          onPointerLeave={() => setHover(null)}
          className="replay-track relative grid cursor-pointer grid-rows-[20px_20px_24px_28px_24px] rounded-md"
        >
          <Axis model={model} />
          <Pages model={model} current={frame?.t ?? model.start} />
          <Calls model={model} />
          <Takeovers model={model} />
          <Proofs model={model} />
          {hover && !dragging ? <span aria-hidden="true" className="pointer-events-none absolute top-5 bottom-0 w-px bg-line-strong" style={{ left: pct(hover.x) }} /> : null}
          {frame ? <Playhead x={along(model, frame.t)} label={offset(model, frame.t)} /> : null}
        </div>
      </div>
    </div>
  );
}

function Axis({ model }: { model: ReplayModel }) {
  return (
    <div aria-hidden="true" className="relative border-b border-line">
      {ticks(model).map((tick) => (
        <span key={tick} className="absolute top-0 flex h-full -translate-x-px flex-col justify-between" style={{ left: pct(along(model, tick)) }}>
          <span className="pl-1 font-mono text-code-caption text-faint tabular">{offset(model, tick)}</span>
        </span>
      ))}
    </div>
  );
}

function Pages({ model, current }: { model: ReplayModel; current: number }) {
  return (
    <div aria-hidden="true" className="relative">
      {model.pages.map((page) => {
        const on = current >= page.from && current < page.to;
        return (
          <span
            key={`${page.from}`}
            className={cn('absolute top-1 bottom-1 truncate border-l px-1 text-caption leading-3', on ? 'border-fg text-fg' : 'border-line-strong text-faint')}
            style={{ left: pct(along(model, page.from)), width: pct(along(model, page.to) - along(model, page.from)) }}
          >
            {page.label}
          </span>
        );
      })}
    </div>
  );
}

function Calls({ model }: { model: ReplayModel }) {
  return (
    <div aria-hidden="true" className="relative border-b border-line">
      {model.calls.map((call, index) => (
        <span
          key={`${call.t}:${index}`}
          className={cn('absolute bottom-0 w-px', call.ok ? 'bg-chart-bar' : 'bg-bad-dot')}
          style={{ left: pct(along(model, call.t)), height: call.ok ? 8 : 14 }}
        />
      ))}
      {model.frames
        .filter((frame) => frame.cause === 'agent')
        .map((frame) => (
          <span key={frame.id} className="absolute bottom-0 h-[18px] w-[2px] rounded-t-sm bg-chart-bar-strong" style={{ left: pct(along(model, frame.t)) }} />
        ))}
    </div>
  );
}

function Takeovers({ model }: { model: ReplayModel }) {
  return (
    <div aria-hidden="true" className="relative border-b border-line">
      {model.takeovers.map((span) => (
        <span
          key={span.from}
          className="absolute top-1 bottom-1 flex min-w-6 items-center justify-center rounded-sm border border-fg bg-card"
          style={{ left: pct(along(model, span.from)), width: pct(along(model, span.to) - along(model, span.from)) }}
          title={`A person had control for ${Math.round((span.to - span.from) / 1000)} s`}
        >
          <Hand className="size-3 text-fg" />
        </span>
      ))}
    </div>
  );
}

function Proofs({ model }: { model: ReplayModel }) {
  return (
    <div aria-hidden="true" className="relative">
      {model.proofs.map((proof) => (
        <span key={proof.id} className="absolute top-1 flex size-4 -translate-x-1/2 items-center justify-center rounded-sm border border-line-strong bg-card" style={{ left: pct(along(model, proof.t)) }} title={proof.title}>
          <ImageIcon className="size-2.5 text-fg" />
        </span>
      ))}
    </div>
  );
}

function Playhead({ x, label }: { x: number; label: string }) {
  return (
    <span aria-hidden="true" className="pointer-events-none absolute top-0 bottom-0 z-10 flex -translate-x-1/2 flex-col items-center transition-[left] duration-(--duration-fast) ease-(--ease-standard)" style={{ left: pct(x) }}>
      <span className="rounded-sm bg-inverse px-1 font-mono text-code-caption leading-4 text-on-inverse tabular">{label}</span>
      <span className="w-[2px] flex-1 bg-fg" />
    </span>
  );
}

/** The frame under the pointer, small, above the track. */
function Peek({ model, frame, x }: { model: ReplayModel; frame: ReplayModel['frames'][number]; x: number }) {
  return (
    <div aria-hidden="true" className="pointer-events-none absolute bottom-full z-20 mb-2 w-48 -translate-x-1/2 overflow-hidden rounded-lg border border-line bg-raised shadow-popover max-sm:hidden" style={{ left: `clamp(96px, ${pct(x)}, calc(100% - 96px))` }}>
      {frame.src ? <img src={frame.src} alt="" width={192} height={120} className="aspect-[16/10] w-full bg-inset object-cover object-top" /> : null}
      <div className="grid gap-px px-2 py-1">
        <span className="truncate text-caption text-fg">{causeWords(frame)}</span>
        <span className="font-mono text-code-caption text-faint tabular">{offset(model, frame.t)}</span>
      </div>
    </div>
  );
}
