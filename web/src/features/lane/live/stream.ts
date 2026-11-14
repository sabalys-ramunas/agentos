/**
 * The connection behind the live view and the terminal: one WebSocket to the cloud relay,
 * read honestly. Every way it can end maps to one phase, and only the phases that can heal by
 * themselves retry. Plain TypeScript, so the whole life of a connection is tested without a
 * browser.
 *
 * Close codes are the relay's, as docs/API.md "Lane control" lists them: 4401 signed out, 4403 not allowed, 4404 not found, 4202 waiting for approval,
 * 4410 closed by the node before it was ready, 4503 node offline or refusing. A driving socket
 * closes with 4403 `handed_back` when control moves away from this person, and a control socket
 * opened without holding the lane is refused with 4403 `not_holding`: both mean "watch instead".
 */

export type StreamKind = 'vnc' | 'terminal';

export type Phase =
  | 'idle'
  | 'connecting'
  | 'live'
  | 'reconnecting'
  | 'away'
  | 'waiting'
  | 'signed_out'
  | 'denied'
  | 'missing'
  | 'ended'
  | 'failed';

export interface StreamState {
  phase: Phase;
  /** The relay's or node's reason, when it gave one. Shown only through `streamCopy`. */
  reason: string | null;
  /** When the next try happens, for reconnecting and away. */
  retryAt: number | null;
  /** A new connection is opening behind a live one (view to control). The old one stays on screen. */
  switching: boolean;
}

export const IDLE: StreamState = { phase: 'idle', reason: null, retryAt: null, switching: false };

export const CLOSE = {
  normal: 1000,
  waiting: 4202,
  unauthorized: 4401,
  forbidden: 4403,
  notFound: 4404,
  gone: 4410,
  offline: 4503,
} as const;

/** 4403 reasons that mean control is not, or no longer, this person's. The screen goes back to watching. */
const CONTROL_LOST = new Set(['handed_back', 'not_holding']);

/** Reasons the relay gives with 4503 or 1000 that mean the node is not there. Anything else is a refusal. */
const NODE_AWAY = new Set(['', 'node offline', 'node did not answer', 'lost', 'node_gone']);

export type Verdict =
  | { phase: 'signed_out' | 'missing' }
  | { phase: 'denied' | 'waiting' | 'ended'; reason: string | null }
  | { retry: 'drop' | 'away' | 'limited' | 'switch'; reason: string | null }
  /** Refused before the socket opened: the browser hides the code, so ask the REST API why. */
  | { probe: true };

export interface Closed {
  code: number;
  reason: string;
  /** The socket's open event fired. */
  opened: boolean;
}

export function readClose({ code, reason, opened }: Closed): Verdict {
  const why = reason.trim();
  switch (code) {
    case CLOSE.unauthorized:
      return { phase: 'signed_out' };
    case CLOSE.forbidden:
      if (CONTROL_LOST.has(why)) return { retry: 'switch', reason: why };
      return why === 'rate limited' ? { retry: 'limited', reason: why } : { phase: 'denied', reason: why || null };
    case CLOSE.notFound:
      return { phase: 'missing' };
    case CLOSE.waiting:
      return { phase: 'waiting', reason: why || null };
    case CLOSE.gone:
      return { phase: 'ended', reason: why || 'closed' };
    case CLOSE.offline:
      return NODE_AWAY.has(why) ? { retry: 'away', reason: why || null } : { phase: 'ended', reason: why };
    case CLOSE.normal:
      return NODE_AWAY.has(why) && why ? { retry: 'away', reason: why } : { phase: 'ended', reason: why || null };
    default:
      // 1006 and friends. Before open, a refusal looks exactly like this (see findings).
      return opened ? { retry: 'drop', reason: null } : { probe: true };
  }
}

const DROP_STEPS = [1, 2, 4, 8, 16, 30];
const AWAY_STEPS = [5, 10, 20, 30];
/** A dropped connection gives up after this many tries in a row. A node that is away is waited for. */
export const MAX_DROPS = 6;

/** Long enough for the lane's new control to arrive and turn the screen back to watching. */
const SWITCH_MS = 600;

/** Milliseconds before try number `attempt` (1-based), with a fifth either way so tabs do not stampede. */
export function backoff(attempt: number, kind: 'drop' | 'away' | 'limited' | 'switch', random = Math.random): number {
  if (kind === 'switch') return SWITCH_MS;
  const steps = kind === 'drop' ? DROP_STEPS : AWAY_STEPS;
  const base = kind === 'limited' ? 30 : steps[Math.min(attempt, steps.length) - 1]!;
  return Math.round(base * 1000 * (0.8 + random() * 0.4));
}

export function streamUrl(
  org: string,
  ws: string,
  name: string,
  kind: StreamKind,
  params: Record<string, string | number | undefined>,
  where: Pick<Location, 'protocol' | 'host'> = window.location,
): string {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) if (value !== undefined) query.set(key, String(value));
  const search = query.size ? `?${query}` : '';
  const scheme = where.protocol === 'https:' ? 'wss:' : 'ws:';
  const lane = `${encodeURIComponent(ws)}/${encodeURIComponent(name)}`;
  return `${scheme}//${where.host}/api/orgs/${encodeURIComponent(org)}/lanes/${lane}/${kind}${search}`;
}

export interface Attached {
  /** Undo everything attach did and close the socket. Safe to call twice. */
  (): void;
}

export interface ControllerOptions {
  /** Wires a fresh socket to the screen. Call `live` once real content arrives. */
  attach: (socket: WebSocket, live: () => void) => Attached;
  onChange: (state: StreamState) => void;
  /** After a refusal the browser could not read: what the REST API says, or null to keep trying. */
  probe?: () => Promise<Extract<Phase, 'signed_out' | 'denied' | 'missing' | 'away'> | null>;
  open?: (url: string) => WebSocket;
  /** A connection that opens but shows nothing for this long is treated as a node away. */
  liveTimeoutMs?: number;
  /** Control moved away from this person (or was never theirs). Read the lane's control again. */
  onControlLost?: () => void;
  now?: () => number;
  random?: () => number;
}

interface Connection {
  socket: WebSocket;
  detach: Attached;
  opened: boolean;
  live: boolean;
  done: boolean;
  timer: ReturnType<typeof setTimeout> | null;
}

export class StreamController {
  private url: string | null = null;
  private current: Connection | null = null;
  /** The live connection kept on screen while a new one opens. */
  private previous: Connection | null = null;
  private attempts = 0;
  private refusals = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private waitingForVisible = false;
  private state: StreamState = IDLE;
  private disposed = false;
  /** Control-lost closes in a row with no frame between: the second means this person really is not driving. */
  private switches = 0;
  private readonly options: Required<Omit<ControllerOptions, 'probe' | 'onControlLost'>> & Pick<ControllerOptions, 'probe' | 'onControlLost'>;

  constructor(options: ControllerOptions) {
    this.options = {
      open: (url) => new WebSocket(url),
      liveTimeoutMs: 25_000,
      now: () => Date.now(),
      random: Math.random,
      ...options,
    };
  }

  get snapshot(): StreamState {
    return this.state;
  }

  setUrl(url: string | null) {
    if (this.disposed || url === this.url) return;
    this.url = url;
    this.clearTimer();
    this.attempts = 0;
    this.refusals = 0;
    this.switches = 0;
    if (url === null) {
      this.end(this.current);
      this.end(this.previous);
      this.current = this.previous = null;
      this.emit(IDLE);
      return;
    }
    if (this.current?.live) {
      this.end(this.previous);
      this.previous = this.current;
      this.current = null;
      this.connect(true);
    } else {
      this.end(this.current);
      this.current = null;
      this.connect(false);
    }
  }

  /** Try now, from a button. Starts the count of tries again. */
  retry() {
    if (this.disposed || this.url === null) return;
    this.clearTimer();
    this.attempts = 0;
    this.refusals = 0;
    this.switches = 0;
    this.end(this.current);
    this.current = null;
    this.connect(false);
  }

  dispose() {
    this.disposed = true;
    this.clearTimer();
    this.end(this.current);
    this.end(this.previous);
    this.current = this.previous = null;
    if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', this.onVisible);
  }

  private emit(next: StreamState) {
    this.state = next;
    if (!this.disposed) this.options.onChange(next);
  }

  private clearTimer() {
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
  }

  private end(connection: Connection | null) {
    if (!connection || connection.done) return;
    connection.done = true;
    if (connection.timer) clearTimeout(connection.timer);
    connection.detach();
    const { socket } = connection;
    if (socket.readyState === WebSocket.CONNECTING || socket.readyState === WebSocket.OPEN) socket.close(CLOSE.normal);
  }

  private connect(switching: boolean) {
    const url = this.url;
    if (url === null) return;
    if (switching) this.emit({ ...this.state, switching: true });
    // A retry keeps saying why it is retrying; no time means it is trying now.
    else if (this.attempts) this.emit({ ...this.state, retryAt: null });
    else this.emit({ phase: 'connecting', reason: null, retryAt: null, switching: false });
    let socket: WebSocket;
    try {
      socket = this.options.open(url);
    } catch {
      this.emit({ phase: 'failed', reason: null, retryAt: null, switching: false });
      return;
    }
    socket.binaryType = 'arraybuffer';
    const connection: Connection = { socket, detach: () => {}, opened: false, live: false, done: false, timer: null };
    this.current = connection;
    socket.addEventListener('open', () => {
      connection.opened = true;
    });
    socket.addEventListener('close', (event) => this.onClose(connection, event.code, event.reason));
    connection.detach = this.options.attach(socket, () => this.onLive(connection));
    connection.timer = setTimeout(() => {
      if (connection.live || connection.done || connection !== this.current) return;
      this.lose(connection);
      this.schedule('away', 'node did not answer');
    }, this.options.liveTimeoutMs);
  }

  private onLive(connection: Connection) {
    if (connection !== this.current || connection.live || connection.done) return;
    connection.live = true;
    if (connection.timer) clearTimeout(connection.timer);
    this.attempts = 0;
    this.refusals = 0;
    this.switches = 0;
    this.end(this.previous);
    this.previous = null;
    this.emit({ phase: 'live', reason: null, retryAt: null, switching: false });
  }

  /** Stop using a connection that failed, and anything kept behind it. */
  private lose(connection: Connection) {
    this.end(connection);
    if (this.current === connection) this.current = null;
    this.end(this.previous);
    this.previous = null;
  }

  private onClose(connection: Connection, code: number, reason: string) {
    if (connection.done || connection !== this.current) return;
    const wasLive = connection.live;
    this.lose(connection);
    if (wasLive) this.attempts = 0;
    const verdict = readClose({ code, reason, opened: connection.opened });
    if ('probe' in verdict) {
      void this.probe();
    } else if ('retry' in verdict && verdict.retry === 'switch') {
      this.options.onControlLost?.();
      this.switches += 1;
      // Asked to watch twice running without a frame: the screen still thinks it drives, so stop
      // and say so instead of knocking on the control door forever.
      if (this.switches > 1) this.emit({ phase: 'denied', reason: 'not_holding', retryAt: null, switching: false });
      else this.schedule('switch', verdict.reason);
    } else if ('retry' in verdict) {
      this.refusals = 0;
      this.schedule(verdict.retry, verdict.reason);
    } else {
      this.emit({ phase: verdict.phase, reason: 'reason' in verdict ? verdict.reason : null, retryAt: null, switching: false });
    }
  }

  private async probe() {
    const url = this.url;
    let answer: Awaited<ReturnType<NonNullable<ControllerOptions['probe']>>> = null;
    try {
      answer = (await this.options.probe?.()) ?? null;
    } catch {
      answer = null;
    }
    if (this.disposed || url !== this.url || this.current) return;
    if (answer === 'away') return this.schedule('away', null);
    if (answer) return this.emit({ phase: answer, reason: null, retryAt: null, switching: false });
    // The API says all is well and the relay still refuses: after a few, stop and say so.
    this.refusals += 1;
    if (this.refusals >= 3) return this.emit({ phase: 'failed', reason: 'refused', retryAt: null, switching: false });
    this.schedule('drop', null);
  }

  private schedule(kind: 'drop' | 'away' | 'limited' | 'switch', reason: string | null) {
    if (kind !== 'switch') this.attempts += 1;
    if (kind === 'drop' && this.attempts > MAX_DROPS) {
      this.emit({ phase: 'failed', reason, retryAt: null, switching: false });
      return;
    }
    const delay = backoff(this.attempts, kind, this.options.random);
    const phase: Phase = kind === 'away' ? 'away' : 'reconnecting';
    this.emit({ phase, reason, retryAt: this.options.now() + delay, switching: false });
    this.clearTimer();
    this.timer = setTimeout(() => {
      this.timer = null;
      // A hidden tab waits: nobody is watching, and the node is spared a socket.
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') {
        if (!this.waitingForVisible) {
          this.waitingForVisible = true;
          document.addEventListener('visibilitychange', this.onVisible);
        }
        return;
      }
      this.connect(false);
    }, delay);
  }

  private onVisible = () => {
    if (document.visibilityState === 'hidden') return;
    document.removeEventListener('visibilitychange', this.onVisible);
    this.waitingForVisible = false;
    if (!this.disposed && !this.current && this.url !== null) this.connect(false);
  };
}
