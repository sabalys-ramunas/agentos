/**
 * A shell on a terminal lane, through the cloud relay, drawn by xterm.js. Loaded on its own.
 *
 * It opens when this person takes over, and typing reaches it only while they hold control.
 * Handing back leaves it open and read-only, so what ran stays readable. Closing the
 * connection detaches and leaves the shell running on the node (docs/PROTOCOL.md); the node
 * gives no session id yet, so a reconnect starts a new shell, and the screen says so.
 */
import { FitAddon } from '@xterm/addon-fit';
import { Terminal } from '@xterm/xterm';
import '@xterm/xterm/css/xterm.css';
import { ClipboardPaste, Expand, Keyboard, Minimize, X } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Button, IconButton } from '@/components/ui/button';
import { cn } from '@/lib/cn';
import { LANE_INPUT } from '@/lib/keyboard';
import { laneName } from '@/lib/labels';
import { TERMINAL_KEYS } from './keys';
import { StreamBadge, StreamNotice, useLaneFocus, useLeaveKey } from './parts';
import type { ScreenProps } from './parts';
import { streamUrl } from './stream';
import { useStream } from './use-stream';
import { useRefreshLane } from '@/api/hooks';

/** Colours from the panel's own tokens, resolved, since xterm cannot read `light-dark()`. */
function themeFromTokens(host: HTMLElement) {
  const probe = document.createElement('span');
  host.append(probe);
  const read = (token: string) => {
    probe.style.color = `var(${token})`;
    return getComputedStyle(probe).color;
  };
  const theme = {
    background: read('--background-subtle'),
    foreground: read('--foreground'),
    cursor: read('--foreground'),
    cursorAccent: read('--background-subtle'),
    selectionBackground: read('--border-strong'),
    black: read('--foreground'),
    brightBlack: read('--foreground-subtle'),
    red: read('--red'),
    green: read('--green'),
    yellow: read('--amber'),
    blue: read('--blue'),
  };
  probe.remove();
  return theme;
}

function useTheme(host: React.RefObject<HTMLElement | null>, apply: (theme: ReturnType<typeof themeFromTokens>) => void) {
  useEffect(() => {
    const element = host.current;
    if (!element) return;
    const update = () => apply(themeFromTokens(element));
    update();
    const scheme = window.matchMedia('(prefers-color-scheme: dark)');
    scheme.addEventListener('change', update);
    const observer = new MutationObserver(update);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    return () => {
      scheme.removeEventListener('change', update);
      observer.disconnect();
    };
  }, [host, apply]);
}

export default function TerminalScreen({ org, lane, interactive, expanded, onExpandedChange, probe }: ScreenProps) {
  const root = useRef<HTMLDivElement>(null);
  const host = useRef<HTMLDivElement>(null);
  const terminal = useRef<Terminal | null>(null);
  const fitter = useRef<FitAddon | null>(null);
  const socket = useRef<WebSocket | null>(null);
  const interactiveNow = useRef(interactive);
  const sessions = useRef(0);
  /** The connection whose output is on screen. */
  const [shown, setShown] = useState<string | null>(null);
  const [open, setOpen] = useState(interactive);
  const [keys, setKeys] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const reduced = useMemo(() => window.matchMedia('(prefers-reduced-motion: reduce)').matches, []);
  const name = laneName(lane.name);

  // Taking over opens it. Handing back leaves it open, read-only.
  useEffect(() => {
    interactiveNow.current = interactive;
    if (interactive) setOpen(true);
    if (terminal.current) terminal.current.options.disableStdin = !interactive;
    if (!interactive) setKeys(false);
  }, [interactive]);

  useEffect(() => {
    const element = host.current;
    if (!element) return;
    const term = new Terminal({
      fontFamily: getComputedStyle(element).getPropertyValue('--font-mono') || 'monospace',
      fontSize: 13,
      lineHeight: 1.25,
      cursorBlink: !reduced,
      disableStdin: !interactiveNow.current,
      scrollback: 5_000,
      allowProposedApi: false,
      screenReaderMode: false,
      // Node output can carry OSC 8 links whose text says one thing and points at another.
      // Nothing in a lane's shell opens a tab from here: copy the address if it is wanted.
      linkHandler: { activate: () => {}, allowNonHttpProtocols: false },
    });
    const fitAddon = new FitAddon();
    term.loadAddon(fitAddon);
    term.open(element);
    term.textarea?.setAttribute('aria-label', `Shell input for ${name}`);
    fitAddon.fit();
    terminal.current = term;
    fitter.current = fitAddon;
    const observer = new ResizeObserver(() => fitAddon.fit());
    observer.observe(element);
    const input = term.onData((data) => {
      if (interactiveNow.current && socket.current?.readyState === WebSocket.OPEN) socket.current.send(data);
    });
    const resize = term.onResize(({ cols, rows }) => {
      if (socket.current?.readyState === WebSocket.OPEN) socket.current.send(JSON.stringify({ type: 'resize', cols, rows }));
    });
    return () => {
      observer.disconnect();
      input.dispose();
      resize.dispose();
      term.dispose();
      terminal.current = null;
    };
    // The terminal lives as long as the screen; the name only labels it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [reduced]);

  useTheme(
    host,
    useCallback((theme) => {
      if (terminal.current) terminal.current.options.theme = theme;
    }, []),
  );

  // The real size goes as a resize message once open, so the URL stays put and never reconnects.
  // Driving needs `mode=control` on the terminal too; without it the relay drops every byte typed.
  // Taking over or handing back opens a new connection, and so a new shell (see the notice below).
  const url = open ? streamUrl(org, lane.workspace, lane.name, 'terminal', { mode: interactive ? 'control' : 'view', cols: 80, rows: 24 }) : null;

  const attach = useCallback((ws: WebSocket, live: () => void) => {
    socket.current = ws;
    const term = terminal.current;
    sessions.current += 1;
    if (sessions.current > 1) {
      term?.write('\r\n\x1b[2m-- reconnected: this is a new shell, the last one keeps running on the node --\x1b[0m\r\n');
    }
    const onOpen = () => {
      if (term) ws.send(JSON.stringify({ type: 'resize', cols: term.cols, rows: term.rows }));
    };
    const onMessage = (event: MessageEvent) => {
      if (typeof event.data === 'string') term?.write(event.data);
      else term?.write(new Uint8Array(event.data as ArrayBuffer));
      setShown(ws.url);
      live();
    };
    ws.addEventListener('open', onOpen);
    ws.addEventListener('message', onMessage);
    return () => {
      ws.removeEventListener('open', onOpen);
      ws.removeEventListener('message', onMessage);
      if (socket.current === ws) socket.current = null;
      if (ws.readyState === WebSocket.CONNECTING || ws.readyState === WebSocket.OPEN) ws.close(1000);
    };
  }, []);

  const refreshLane = useRefreshLane(org, lane.workspace, lane.name);
  const { state, retry } = useStream(url, attach, probe, refreshLane);
  const live = state.phase === 'live';
  // Taking over opens a control connection behind the read-only one. Until it shows output,
  // typing would go nowhere, so nothing says "yours" before then.
  const driving = interactive && live && !state.switching && shown === url;

  useLaneFocus(interactive, driving, () => terminal.current?.focus(), root);
  useLeaveKey(root, () => root.current?.querySelector<HTMLElement>('[data-screen-toolbar] button')?.focus());

  useEffect(() => {
    const frame = requestAnimationFrame(() => fitter.current?.fit());
    return () => cancelAnimationFrame(frame);
  }, [expanded]);

  const send = (bytes: string) => {
    if (interactive && socket.current?.readyState === WebSocket.OPEN) socket.current.send(bytes);
    terminal.current?.focus();
  };

  const paste = async () => {
    setNote(null);
    try {
      // Read only on this tap, and handed to the shell as a paste.
      const text = await navigator.clipboard.readText();
      terminal.current?.paste(text);
    } catch {
      setNote('The browser did not allow reading the clipboard. Use your usual paste keys in the terminal.');
    }
  };

  const close = () => setOpen(false);

  const line = !live ? null : driving ? 'Your typing goes to the shell' : interactive ? 'Switching to your shell' : 'Read only. Take over to type';

  return (
    <div ref={root} role="group" aria-label={`Terminal of ${name}`} className={cn('flex min-h-0 flex-col', expanded && 'flex-1')}>
      <div className={cn('relative min-h-0 bg-subtle', expanded ? 'flex-1' : 'aspect-[16/10]')}>
        <div
          {...{ [LANE_INPUT]: '' }}
          className={cn('absolute inset-0 p-2', driving && 'ring-2 ring-fg ring-inset')}
        >
          <div ref={host} className="size-full" />
        </div>
        {open ? (
          <StreamNotice state={state} kind="terminal" org={org} node={lane.node} onRetry={retry} />
        ) : (
          <div role="status" className="absolute inset-0 z-10 flex flex-col items-center justify-center bg-subtle p-6 text-center">
            <h3 className="text-14 font-medium">The terminal is closed</h3>
            <p className="mt-1 max-w-sm text-14 text-muted">
              Take over to open a shell on this lane. Agents are held off while you type.
            </p>
          </div>
        )}
      </div>
      <div data-screen-toolbar className="flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-line px-4 py-2">
        <div className="flex min-w-0 flex-1 basis-48 items-center gap-2">
          <StreamBadge state={open ? state : { ...state, phase: 'idle' }} kind="terminal" node={lane.node} />
          {line ? <span className={cn('min-w-0 truncate text-13', driving ? 'font-medium text-fg' : 'text-faint')}>{line}</span> : null}
        </div>
        <div className="flex items-center gap-1">
          {interactive ? (
            <IconButton label="Keys a phone keyboard lacks" aria-pressed={keys} disabled={!live} onClick={() => setKeys((value) => !value)}>
              <Keyboard className="size-4" />
            </IconButton>
          ) : null}
          {open && !interactive ? (
            <IconButton label="Close the terminal" onClick={close}>
              <X className="size-4" />
            </IconButton>
          ) : null}
          <IconButton label={expanded ? 'Leave full window' : 'Full window'} onClick={() => onExpandedChange(!expanded)}>
            {expanded ? <Minimize className="size-4" /> : <Expand className="size-4" />}
          </IconButton>
        </div>
      </div>
      {interactive && keys && live ? (
        <div className="grid gap-2 border-t border-line px-4 py-3">
          <div className="flex flex-wrap gap-2">
            {TERMINAL_KEYS.map((key) => (
              <Button key={key.label} size="sm" className="max-sm:h-10" onClick={() => send(key.bytes)}>
                {key.label}
              </Button>
            ))}
            <Button size="sm" className="max-sm:h-10" icon={<ClipboardPaste className="size-4" />} onClick={() => void paste()}>
              Paste
            </Button>
          </div>
          {note ? <p className="text-12 text-muted">{note}</p> : null}
        </div>
      ) : null}
    </div>
  );
}
