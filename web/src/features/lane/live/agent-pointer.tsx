/**
 * The agent's pointer over the live view: where the agent acts in its browser, with its name, as the node's own
 * live view page draws it. The node sends the points (an environment's desktop knows where its browser window
 * is) on the event stream while this view is open; the pointer glides between them with a transform only, fades
 * after a few quiet seconds, and hides while a person has control, the agent is stopped, or it works elsewhere
 * (its terminal). Placed by the picture's own size, so it stays right when the picture is scaled or zoomed, and
 * drawn at one size whatever that scale.
 */
import { useEffect, useRef, useState } from 'react';
import { watchPointer } from '@/api/pointer';
import type { AgentPointerEvent, LaneControl } from '@/api/types';

export const POINTER_FADE_MS = 4500;
/** The pointer and its name tag stay one size whatever the picture's scale or zoom (a 13 px name), as a pointer does. */
export const POINTER_SCALE = 0.8;
const ARROW = 'M3 2.5 V31 L10.4 24.2 L15.4 35.6 L20.6 33.4 L15.7 22.2 H26 Z';

interface Spot {
  x: number;
  y: number;
  w: number;
  h: number;
}

function spotOf(event: AgentPointerEvent): Spot | null {
  const { x, y, w, h } = event;
  if (typeof x !== 'number' || typeof y !== 'number' || typeof w !== 'number' || typeof h !== 'number') return null;
  if (![x, y, w, h].every(Number.isFinite) || w <= 0 || h <= 0 || x < 0 || y < 0 || x > w || y > h) return null;
  return { x, y, w, h };
}

export function AgentPointer({ lane, name, width, live, control }: {
  lane: string;
  /** The agent's name, on the tag beside the arrow ("Agent" when it gave none). */
  name: string | null | undefined;
  /** The picture's width on this page, in CSS px. */
  width: number;
  /** The picture is live (the pointer hides while it connects). */
  live: boolean;
  /** Who has control: the pointer shows only while the agent has it. */
  control: LaneControl['state'];
}) {
  const hidden = !live || control !== 'agent';
  const [spot, setSpot] = useState<Spot | null>(null);
  const [shown, setShown] = useState(false);
  /** A pointer that was gone appears where it is, without a glide from where it was. */
  const [jump, setJump] = useState(true);
  const [presses, setPresses] = useState(0);
  const shownNow = useRef(false);

  useEffect(() => {
    let fade: ReturnType<typeof setTimeout> | undefined;
    let settle = 0;
    const hide = () => {
      clearTimeout(fade);
      shownNow.current = false;
      setShown(false);
    };
    const stop = watchPointer(lane, (event) => {
      const next = event.hide ? null : spotOf(event);
      if (!next) {
        if (event.hide) hide();
        return;
      }
      if (!shownNow.current) {
        setJump(true);
        cancelAnimationFrame(settle);
        settle = requestAnimationFrame(() => {
          settle = requestAnimationFrame(() => setJump(false));
        });
      }
      shownNow.current = true;
      setSpot(next);
      setShown(true);
      if (event.kind === 'press') setPresses((count) => count + 1);
      clearTimeout(fade);
      fade = setTimeout(hide, POINTER_FADE_MS);
    });
    return () => {
      stop();
      clearTimeout(fade);
      cancelAnimationFrame(settle);
    };
  }, [lane]);

  useEffect(() => {
    if (hidden) {
      shownNow.current = false;
      setShown(false);
    }
  }, [hidden]);

  if (!spot || width <= 0) return null;
  const k = width / spot.w;
  return (
    <div
      aria-hidden="true"
      data-testid="agent-pointer"
      className="agent-pointer"
      data-shown={shown && !hidden}
      data-jump={jump}
      style={{ transform: `translate3d(${Math.round(spot.x * k)}px, ${Math.round(spot.y * k)}px, 0)` }}
    >
      <div className="agent-pointer-body" style={{ transform: `scale(${POINTER_SCALE})` }}>
        <svg key={presses} className="agent-pointer-arrow" data-press={presses > 0} viewBox="0 0 34 40">
          <path d={ARROW} />
        </svg>
        <span className="agent-pointer-tag">{name || 'Agent'}</span>
      </div>
    </div>
  );
}
