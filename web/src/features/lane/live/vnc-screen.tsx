/**
 * The lane's live view: the node's VNC stream through the cloud relay, drawn by noVNC. Loaded
 * on its own, so noVNC never weighs on the rest of the panel.
 *
 * Watching is read-only. Only a person holding control connects in `control` mode, and only
 * then do keys and pointer leave this page. The node enforces the same (docs/PROTOCOL.md,
 * "v1 clarifications"). Frames are drawn and dropped; nothing on screen is kept.
 */
import RFB from '@novnc/novnc';
import { ClipboardPaste, Expand, Hand, Keyboard, Minimize, Minus, Plus, Scan } from 'lucide-react';
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { Button, IconButton } from '@/components/ui/button';
import { Input } from '@/components/ui/field';
import { cn } from '@/lib/cn';
import { LANE_INPUT } from '@/lib/keyboard';
import { laneName } from '@/lib/labels';
import { anchorScroll, clampZoom, fit, KEYSYM, keysymFor } from './keys';
import { StreamBadge, StreamNotice, useLaneFocus, useLeaveKey, viewOnlyWords } from './parts';
import type { ScreenProps } from './parts';
import { streamUrl } from './stream';
import { useStream } from './use-stream';
import { useRefreshLane } from '@/api/hooks';

/** noVNC fires `connect` after the handshake; the first full frame lands just after. */
const FIRST_FRAME_MS = 200;

function usePinchAndPan(viewport: React.RefObject<HTMLDivElement | null>, zoom: number, setZoom: (next: number, at?: { x: number; y: number }) => void) {
  const pointers = useRef(new Map<number, { x: number; y: number }>());
  const pinch = useRef<{ distance: number; zoom: number } | null>(null);
  const lastTap = useRef(0);
  const moved = useRef(false);

  const distance = () => {
    const [a, b] = [...pointers.current.values()];
    return a && b ? Math.hypot(a.x - b.x, a.y - b.y) : 0;
  };
  const centre = () => {
    const points = [...pointers.current.values()];
    return { x: points.reduce((sum, p) => sum + p.x, 0) / points.length, y: points.reduce((sum, p) => sum + p.y, 0) / points.length };
  };

  return {
    onPointerDown: (event: React.PointerEvent) => {
      event.currentTarget.setPointerCapture?.(event.pointerId);
      pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
      moved.current = false;
      if (pointers.current.size === 2) pinch.current = { distance: distance(), zoom };
    },
    onPointerMove: (event: React.PointerEvent) => {
      const previous = pointers.current.get(event.pointerId);
      if (!previous) return;
      pointers.current.set(event.pointerId, { x: event.clientX, y: event.clientY });
      if (Math.hypot(event.clientX - previous.x, event.clientY - previous.y) > 2) moved.current = true;
      if (pinch.current && pointers.current.size === 2) {
        const next = (pinch.current.zoom * distance()) / (pinch.current.distance || 1);
        setZoom(next, centre());
      } else if (pointers.current.size === 1 && viewport.current) {
        viewport.current.scrollLeft -= event.clientX - previous.x;
        viewport.current.scrollTop -= event.clientY - previous.y;
      }
    },
    onPointerUp: (event: React.PointerEvent) => {
      pointers.current.delete(event.pointerId);
      if (pointers.current.size < 2) pinch.current = null;
      if (event.pointerType !== 'touch' || moved.current) return;
      // A double tap zooms in on that spot, and out again.
      const now = event.timeStamp;
      if (now - lastTap.current < 300) {
        setZoom(zoom > 1 ? 1 : 2, { x: event.clientX, y: event.clientY });
        lastTap.current = 0;
      } else {
        lastTap.current = now;
      }
    },
    onPointerCancel: (event: React.PointerEvent) => {
      pointers.current.delete(event.pointerId);
      pinch.current = null;
    },
  };
}

function TypePanel({ onType, onKey }: { onType: (text: string) => void; onKey: (keysym: number) => void }) {
  const [text, setText] = useState('');
  const [note, setNote] = useState<string | null>(null);
  const field = useRef<HTMLInputElement>(null);
  const paste = async () => {
    setNote(null);
    try {
      // Read only now, on this tap. It goes into the field first, so you see what will be sent.
      const value = await navigator.clipboard.readText();
      setText((current) => current + value);
      field.current?.focus();
    } catch {
      setNote('The browser did not allow reading the clipboard. Paste into the field instead.');
    }
  };
  return (
    <div className="grid gap-2 border-t border-line px-4 py-3">
      <form
        className="flex gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          if (!text) return;
          onType(text);
          setText('');
        }}
      >
        <label htmlFor="lane-type" className="sr-only">
          Text to send to the lane
        </label>
        <Input
          ref={field}
          id="lane-type"
          value={text}
          onChange={(event) => setText(event.target.value)}
          placeholder="Type a code or text, then Send"
          autoComplete="off"
          autoCapitalize="off"
          autoCorrect="off"
          spellCheck={false}
          enterKeyHint="send"
          className="h-10 min-w-0 flex-1 sm:h-8"
        />
        <Button type="submit" className="max-sm:h-10" disabled={!text}>
          Send
        </Button>
      </form>
      <div className="flex flex-wrap gap-2">
        {(['Enter', 'Backspace', 'Tab', 'Escape'] as const).map((key) => (
          <Button key={key} size="sm" className="max-sm:h-10" onClick={() => onKey(KEYSYM[key])}>
            {key === 'Escape' ? 'Esc' : key}
          </Button>
        ))}
        <Button size="sm" className="max-sm:h-10" icon={<ClipboardPaste className="size-4" />} onClick={() => void paste()}>
          Paste
        </Button>
      </div>
      {note ? <p className="text-12 text-muted">{note}</p> : null}
    </div>
  );
}

export default function VncScreen({ org, lane, interactive, holder, canTake, expanded, onExpandedChange, probe }: ScreenProps) {
  const root = useRef<HTMLDivElement>(null);
  const viewport = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLDivElement>(null);
  const rfb = useRef<RFB | null>(null);
  const canvas = useRef<HTMLCanvasElement | null>(null);
  const interactiveNow = useRef(interactive);
  const [frame, setFrame] = useState({ width: 16, height: 10 });
  /** The URL of the connection on screen, which says whether it is the view or the control one. */
  const [shown, setShown] = useState<string | null>(null);
  const [room, setRoom] = useState({ width: 0, height: 0 });
  const [zoom, setZoomState] = useState(1);
  const [moving, setMoving] = useState(false);
  const [typing, setTyping] = useState(false);
  const pendingScroll = useRef<{ left: number; top: number } | null>(null);
  const name = laneName(lane.name);

  useEffect(() => {
    interactiveNow.current = interactive;
  }, [interactive]);

  const url = streamUrl(org, lane.workspace, lane.name, 'vnc', { mode: interactive ? 'control' : 'view' });

  const attach = useCallback(
    (socket: WebSocket, live: () => void) => {
      const target = document.createElement('div');
      target.className = 'absolute inset-0 invisible';
      box.current?.append(target);
      const session = new RFB(target, socket, { shared: true });
      session.scaleViewport = true;
      session.background = 'transparent';
      session.viewOnly = !interactiveNow.current;
      session.focusOnClick = interactiveNow.current;
      let timer: ReturnType<typeof setTimeout> | undefined;
      // The far screen can change size (a browser window made desktop-sized, a phone-sized one): the canvas
      // follows it, and so does the frame, so the picture always fits the box instead of a stale size.
      let resized: MutationObserver | undefined;
      const onConnect = () => {
        timer = setTimeout(() => {
          const drawn = target.querySelector('canvas');
          if (drawn?.width && drawn.height) setFrame({ width: drawn.width, height: drawn.height });
          if (drawn && typeof MutationObserver !== 'undefined') {
            resized = new MutationObserver(() => {
              if (drawn.width && drawn.height) setFrame((now) => (now.width === drawn.width && now.height === drawn.height ? now : { width: drawn.width, height: drawn.height }));
            });
            resized.observe(drawn, { attributes: true, attributeFilter: ['width', 'height'] });
          }
          target.classList.remove('invisible');
          rfb.current = session;
          canvas.current = drawn;
          setShown(socket.url);
          live();
        }, FIRST_FRAME_MS);
      };
      // The lane's clipboard never reaches this one: nothing on screen is kept.
      const ignore = (event: Event) => event.stopImmediatePropagation();
      session.addEventListener('connect', onConnect);
      session.addEventListener('clipboard', ignore);
      return () => {
        clearTimeout(timer);
        resized?.disconnect();
        session.removeEventListener('connect', onConnect);
        session.removeEventListener('clipboard', ignore);
        if (rfb.current === session) {
          rfb.current = null;
          canvas.current = null;
        }
        try {
          session.disconnect();
        } catch {
          // Already closed by the far end.
        }
        target.remove();
      };
    },
    [],
  );

  const refreshLane = useRefreshLane(org, lane.workspace, lane.name);
  const { state, retry } = useStream(url, attach, probe, refreshLane);
  const live = state.phase === 'live';
  // Taking over opens a control connection behind the view one. Until it is live, keys would
  // still go to the view connection, which drops them, so nothing says "yours" before then.
  const driving = interactive && live && !state.switching && shown === url;

  // The canvas takes keys only while this person drives; then it is in the tab order and named.
  useEffect(() => {
    const drawn = canvas.current;
    if (rfb.current) {
      rfb.current.viewOnly = !driving;
      rfb.current.focusOnClick = driving;
    }
    if (!drawn || !live) return;
    drawn.tabIndex = driving ? 0 : -1;
    drawn.setAttribute('aria-label', driving ? `${name}, live. Your keys go to the lane. Shift Escape leaves it.` : `${name}, live view`);
  }, [driving, live, name]);

  useLaneFocus(interactive, driving, () => rfb.current?.focus({ preventScroll: true }), root);
  useLeaveKey(root, () => root.current?.querySelector<HTMLElement>('[data-screen-toolbar] button')?.focus());

  useLayoutEffect(() => {
    const element = viewport.current;
    if (!element) return;
    const measure = () => setRoom({ width: element.clientWidth, height: element.clientHeight });
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const setZoom = useCallback(
    (next: number, at?: { x: number; y: number }) => {
      const element = viewport.current;
      const target = clampZoom(next);
      if (!element || target === zoom) return;
      const rect = element.getBoundingClientRect();
      const x = at ? at.x - rect.left : element.clientWidth / 2;
      const y = at ? at.y - rect.top : element.clientHeight / 2;
      pendingScroll.current = { left: anchorScroll(element.scrollLeft, x, zoom, target), top: anchorScroll(element.scrollTop, y, zoom, target) };
      setZoomState(target);
    },
    [zoom],
  );

  useLayoutEffect(() => {
    const element = viewport.current;
    if (!element || !pendingScroll.current) return;
    element.scrollLeft = pendingScroll.current.left;
    element.scrollTop = pendingScroll.current.top;
    pendingScroll.current = null;
  }, [zoom]);

  // Ctrl and the wheel (a trackpad pinch) zoom the picture, never the lane's page.
  useEffect(() => {
    const element = viewport.current;
    if (!element) return;
    const onWheel = (event: WheelEvent) => {
      if (!event.ctrlKey) return;
      event.preventDefault();
      event.stopPropagation();
      setZoom(zoom * Math.exp(-event.deltaY / 200), { x: event.clientX, y: event.clientY });
    };
    element.addEventListener('wheel', onWheel, { capture: true, passive: false });
    return () => element.removeEventListener('wheel', onWheel, { capture: true });
  }, [setZoom, zoom]);

  useEffect(() => {
    if (!interactive) {
      setTyping(false);
      setMoving(false);
    }
  }, [interactive]);

  const fitted = fit(frame.width, frame.height, room.width, room.height);
  const gestures = usePinchAndPan(viewport, zoom, setZoom);
  const panning = !interactive || moving;

  const type = (text: string) => {
    for (const char of text) rfb.current?.sendKey(keysymFor(char), null);
  };

  const line = !live
    ? null
    : interactive && !driving
      ? 'Handing you the keyboard and mouse'
      : driving
        ? 'Your keyboard and mouse go to the lane'
      : viewOnlyWords(holder, canTake);

  return (
    <div ref={root} role="group" aria-label={`Live view of ${name}`} className={cn('flex min-h-0 flex-col', expanded && 'flex-1')}>
      {/* Not expanded, the box takes the far screen's own shape (16:10 until it is known), no taller than
          most of the window, so a wide desktop and a tall phone screen both fill it rather than float in black. */}
      <div
        className={cn('relative min-h-0 bg-subtle', expanded ? 'flex-1' : 'max-h-[75dvh]')}
        style={expanded ? undefined : { aspectRatio: `${frame.width} / ${frame.height}` }}
      >
        <div
          ref={viewport}
          {...{ [LANE_INPUT]: '' }}
          className={cn(
            'absolute inset-0 overflow-auto overscroll-contain',
            driving && 'ring-2 ring-fg ring-inset',
          )}
        >
          <div className="flex min-h-full min-w-full items-center justify-center" style={{ width: fitted.width * zoom || undefined, height: fitted.height * zoom || undefined }}>
            <div ref={box} className="relative shrink-0" style={{ width: fitted.width * zoom, height: fitted.height * zoom }}>
              {panning ? (
                <div
                  aria-hidden="true"
                  className={cn('absolute inset-0 z-[1] touch-none', zoom > 1 ? 'cursor-grab active:cursor-grabbing' : 'cursor-default')}
                  {...gestures}
                />
              ) : null}
            </div>
          </div>
        </div>
        <StreamNotice state={state} kind="vnc" org={org} node={lane.node} onRetry={retry} />
      </div>
      <div data-screen-toolbar className="flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-line px-4 py-2">
        <div className="flex min-w-0 flex-1 basis-48 items-center gap-2">
          <StreamBadge state={state} kind="vnc" node={lane.node} />
          {line ? <span className={cn('min-w-0 truncate text-13', driving ? 'font-medium text-fg' : 'text-faint')}>{line}</span> : null}
        </div>
        <div className="flex items-center gap-1">
          {interactive ? (
            <>
              <IconButton label="Type into the lane" aria-pressed={typing} disabled={!driving} onClick={() => setTyping((value) => !value)}>
                <Keyboard className="size-4" />
              </IconButton>
              <IconButton label="Move the picture without clicking the lane" aria-pressed={moving} onClick={() => setMoving((value) => !value)}>
                <Hand className="size-4" />
              </IconButton>
            </>
          ) : null}
          <IconButton label="Zoom out" disabled={zoom <= 1} onClick={() => setZoom(zoom / 1.5)}>
            <Minus className="size-4" />
          </IconButton>
          <span className="tabular w-12 text-center text-12 text-faint" aria-live="polite">
            {Math.round(zoom * 100)}%
          </span>
          <IconButton label="Zoom in" disabled={zoom >= 4} onClick={() => setZoom(zoom * 1.5)}>
            <Plus className="size-4" />
          </IconButton>
          <IconButton label="Fit to the window" disabled={zoom === 1} onClick={() => setZoom(1)}>
            <Scan className="size-4" />
          </IconButton>
          <IconButton label={expanded ? 'Leave full window' : 'Full window'} onClick={() => onExpandedChange(!expanded)}>
            {expanded ? <Minimize className="size-4" /> : <Expand className="size-4" />}
          </IconButton>
        </div>
      </div>
      {driving && typing ? (
        <TypePanel onType={type} onKey={(keysym) => rfb.current?.sendKey(keysym, null)} />
      ) : null}
    </div>
  );
}
