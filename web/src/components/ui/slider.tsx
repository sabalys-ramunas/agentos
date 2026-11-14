/**
 * A horizontal row of cards that scrolls (Home's running jobs). The next card peeks in, so it reads
 * as more. It moves by the pager buttons, by the arrow and page keys, by dragging with a mouse and by
 * swiping on a phone; it snaps to a card. Under reduced motion it jumps instead of gliding.
 */
import { ChevronLeft, ChevronRight } from 'lucide-react';
import { Children, useCallback, useEffect, useRef, useState } from 'react';
import type { KeyboardEvent, PointerEvent, ReactNode } from 'react';
import { cn } from '@/lib/cn';
import { IconButton } from './button';

interface SliderProps {
  /** What the row is, for screen readers: "Running now". */
  label: string;
  children: ReactNode;
  /** Left of the pager: a title, a count, filter chips. */
  head?: ReactNode;
  className?: string;
}

function reduced() {
  return typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
}

export function Slider({ label, children, head, className }: SliderProps) {
  const items = Children.toArray(children);
  const track = useRef<HTMLDivElement>(null);
  const drag = useRef<{ x: number; left: number; moved: boolean } | null>(null);
  const [view, setView] = useState({ first: 0, shown: 1 });

  const measure = useCallback(() => {
    const el = track.current;
    const card = el?.firstElementChild as HTMLElement | null;
    if (!el || !card) return;
    const gap = parseFloat(getComputedStyle(el).columnGap) || 0;
    const step = card.offsetWidth + gap;
    if (step <= 0) return;
    const first = Math.round(el.scrollLeft / step);
    const shown = Math.max(1, Math.floor((el.clientWidth + gap + 2) / step));
    setView((current) => (current.first === first && current.shown === shown ? current : { first, shown }));
  }, []);

  useEffect(() => {
    measure();
    const el = track.current;
    if (!el) return;
    el.addEventListener('scroll', measure, { passive: true });
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure);
    observer?.observe(el);
    return () => {
      el.removeEventListener('scroll', measure);
      observer?.disconnect();
    };
  }, [measure, items.length]);

  const go = (index: number) => {
    const el = track.current;
    const card = el?.children[Math.max(0, Math.min(items.length - 1, index))] as HTMLElement | undefined;
    if (!el || !card) return;
    el.scrollTo?.({ left: card.offsetLeft - el.offsetLeft - parseFloat(getComputedStyle(el).paddingLeft || '0'), behavior: reduced() ? 'auto' : 'smooth' });
  };

  const last = Math.max(0, items.length - view.shown);
  const pages = Math.max(1, Math.ceil(items.length / view.shown));
  const page = Math.min(pages - 1, Math.round(view.first / view.shown));
  const to = Math.min(items.length, view.first + view.shown);

  const onKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const keys: Record<string, number> = {
      ArrowRight: 1,
      ArrowLeft: -1,
      PageDown: view.shown,
      PageUp: -view.shown,
    };
    if (event.key in keys) {
      event.preventDefault();
      go(view.first + keys[event.key]!);
    } else if (event.key === 'Home') {
      event.preventDefault();
      go(0);
    } else if (event.key === 'End') {
      event.preventDefault();
      go(last);
    }
  };

  // A mouse drags the row; touch scrolls natively with snap.
  const onPointerDown = (event: PointerEvent<HTMLDivElement>) => {
    if (event.pointerType !== 'mouse' || event.button !== 0 || !track.current) return;
    drag.current = { x: event.clientX, left: track.current.scrollLeft, moved: false };
  };
  const onPointerMove = (event: PointerEvent<HTMLDivElement>) => {
    const state = drag.current;
    const el = track.current;
    if (!state || !el) return;
    const dx = event.clientX - state.x;
    if (!state.moved && Math.abs(dx) > 4) {
      state.moved = true;
      el.dataset.dragging = '';
      el.setPointerCapture(event.pointerId);
    }
    if (state.moved) el.scrollLeft = state.left - dx;
  };
  const endDrag = (event: PointerEvent<HTMLDivElement>) => {
    const state = drag.current;
    const el = track.current;
    drag.current = null;
    if (!state?.moved || !el) return;
    delete el.dataset.dragging;
    if (el.hasPointerCapture(event.pointerId)) el.releasePointerCapture(event.pointerId);
    // A drag is not a click on the card it ended over.
    const swallow = (click: Event) => {
      click.stopPropagation();
      click.preventDefault();
    };
    el.addEventListener('click', swallow, { capture: true, once: true });
    setTimeout(() => el.removeEventListener('click', swallow, { capture: true }), 0);
    measure();
    go(Math.round(el.scrollLeft / Math.max(1, (el.firstElementChild as HTMLElement | null)?.offsetWidth ?? 1)));
  };

  return (
    <section aria-roledescription="carousel" aria-label={label} className={cn('ui-slider', className)}>
      <div className="mb-4 flex min-h-8 flex-wrap items-center gap-3">
        <div className="flex min-w-0 flex-1 flex-wrap items-center gap-3">{head}</div>
        {items.length > view.shown ? (
          <div className="flex items-center gap-1">
            <span className="mr-1 font-mono text-code-caption text-muted tabular" aria-live="polite">
              {view.first + 1}–{to} of {items.length}
            </span>
            <IconButton label="Earlier" variant="secondary" size="sm" disabled={view.first <= 0} onClick={() => go(view.first - view.shown)}>
              <ChevronLeft className="size-4" />
            </IconButton>
            <IconButton label="Later" variant="secondary" size="sm" disabled={view.first >= last} onClick={() => go(view.first + view.shown)}>
              <ChevronRight className="size-4" />
            </IconButton>
          </div>
        ) : null}
      </div>
      {/* The row scrolls, so it takes focus for the arrow keys. */}
      {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions */}
      <div
        ref={track}
        // eslint-disable-next-line jsx-a11y/no-noninteractive-tabindex
        tabIndex={0}
        data-count={items.length <= 3 ? items.length : undefined}
        onKeyDown={onKeyDown}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        className="ui-slider-track"
      >
        {items.map((item, index) => (
          <div key={index} role="group" aria-roledescription="slide" aria-label={`${index + 1} of ${items.length}`} className="flex min-w-0 [&>*]:w-full">
            {item}
          </div>
        ))}
      </div>
      {pages > 1 ? (
        <div className="mt-2 flex min-h-6 justify-center">
          {Array.from({ length: pages }, (_, index) => (
            <button
              key={index}
              type="button"
              aria-label={`Page ${index + 1} of ${pages}`}
              aria-current={index === page ? 'true' : undefined}
              onClick={() => go(index * view.shown)}
              className="group/dot grid size-6 place-items-center rounded-full"
            >
              <i
                aria-hidden="true"
                className={cn(
                  'block h-1.5 rounded-full transition-[width,background-color] duration-(--duration-base) ease-standard',
                  index === page ? 'w-4 bg-fg' : 'w-1.5 bg-line-strong group-hover/dot:bg-control',
                )}
              />
            </button>
          ))}
        </div>
      ) : null}
    </section>
  );
}
