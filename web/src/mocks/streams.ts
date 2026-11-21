/**
 * The mock relay for the lane live view and terminal. It answers the same WebSocket URLs as
 * the cloud, with the same close codes, so the panel's code cannot tell the difference.
 *
 * The live view speaks real RFB 3.8 (no auth, raw encoding), the same subset the simulator's
 * `rfb.py` speaks, so noVNC is exercised for real. The picture is the wireframe from art.ts
 * with a clock that ticks, a text field that shows what was typed, and marks where clicks
 * landed. Input reaches it only in `control` mode while a person holds the lane, as on a node.
 *
 * Scenarios (`?mock=still,stream-away`): stream-away (4503 node offline), stream-denied
 * (4403), stream-signed-out (4401), stream-missing (4404), stream-waiting (terminal 4202),
 * stream-flaky (drops a live connection after 4 s), stream-ended (the node closes it after 3 s),
 * stream-slow (connects after 4 s).
 */
import { ws } from 'msw';
import type { WebSocketHandlerConnection } from 'msw';
import { laneScreenshot } from './art';
import { subscribe } from './bus';
import { db } from './handlers';
import { scenario } from './scenario';

const WIDTH = 960;
const HEIGHT = 600;
const FIELD = { x: 248, y: 548, width: 472, height: 36 };
const CLOCK = { x: 776, y: 16, width: 104, height: 24 };

interface Rect {
  x: number;
  y: number;
  width: number;
  height: number;
}

function parse(url: string) {
  const match = /\/api\/orgs\/([^/]+)\/lanes\/([^/]+)\/([^/?]+)\/(vnc|terminal)/.exec(url);
  if (!match) return null;
  const [, org, workspace, name, kind] = match.map((part) => decodeURIComponent(part ?? ''));
  return { org: org!, id: `${workspace}/${name}`, kind: kind as 'vnc' | 'terminal', params: new URL(url).searchParams };
}

function findLane(org: string, id: string) {
  return db.orgs.get(org)?.lanes.find((lane) => lane.id === id);
}

/** The relay's own test: a person holds the lane, and it is this user, by id. */
function holds(org: string, id: string) {
  const control = findLane(org, id)?.control;
  return control?.state === 'human' && control.by_id === db.user.id;
}

/** Refusals first, the way the relay checks: who you are, what you may do, then the node. */
function refusal(org: string, id: string, kind: 'vnc' | 'terminal', control: boolean): [number, string] | null {
  if (scenario.has('stream-signed-out')) return [4401, ''];
  if (scenario.has('stream-denied')) return [4403, ''];
  const lane = findLane(org, id);
  if (scenario.has('stream-missing') || !lane) return [4404, ''];
  // Watching is member and up (security review 30), and driving is for the holder only (review 7).
  const role = db.orgs.get(org)?.role;
  if (role === 'viewer' || role === 'approver') return [4403, ''];
  if (control && !holds(org, id)) return [4403, 'not_holding'];
  if (scenario.has('stream-away')) return [4503, 'node offline'];
  if (kind === 'terminal' && scenario.has('stream-waiting')) return [4202, `A shell on ${id} waits for an approval`];
  if (lane.state !== 'warm' && lane.state !== 'active') return [4503, 'service_unavailable'];
  if (kind === 'vnc' && (lane.kind === 'terminal' || lane.kind === 'service')) return [4503, 'invalid'];
  if (kind === 'terminal' && lane.kind !== 'terminal') return [4404, 'no terminal'];
  return null;
}

class Page {
  private readonly canvas = document.createElement('canvas');
  private readonly context: CanvasRenderingContext2D;
  private base: HTMLImageElement | null = null;
  typed = '';
  clicks: { x: number; y: number }[] = [];

  constructor(private readonly id: string, attention: 'none' | 'captcha' | 'logged_out') {
    this.canvas.width = WIDTH;
    this.canvas.height = HEIGHT;
    this.context = this.canvas.getContext('2d', { willReadFrequently: true })!;
    const image = new Image();
    this.ready = new Promise((resolve) => {
      image.onload = () => {
        this.base = image;
        resolve();
      };
      image.onerror = () => resolve();
    });
    image.src = `data:image/svg+xml;charset=utf-8,${encodeURIComponent(laneScreenshot(id, attention))}`;
  }

  readonly ready: Promise<void>;

  draw() {
    const c = this.context;
    c.fillStyle = '#f7f7f7';
    c.fillRect(0, 0, WIDTH, HEIGHT);
    if (this.base) c.drawImage(this.base, 0, 0, WIDTH, HEIGHT);
    c.fillStyle = '#ffffff';
    c.strokeStyle = '#d4d4d4';
    c.fillRect(FIELD.x, FIELD.y, FIELD.width, FIELD.height);
    c.strokeRect(FIELD.x + 0.5, FIELD.y + 0.5, FIELD.width - 1, FIELD.height - 1);
    c.font = '15px ui-monospace, Menlo, monospace';
    c.textBaseline = 'middle';
    c.fillStyle = this.typed ? '#171717' : '#8f8f8f';
    c.fillText(this.typed ? this.typed.slice(-48) : `${this.id}: take over, click and type`, FIELD.x + 12, FIELD.y + FIELD.height / 2);
    c.fillStyle = '#ffffff';
    c.fillRect(CLOCK.x, CLOCK.y, CLOCK.width, CLOCK.height);
    c.fillStyle = '#525252';
    c.font = '13px ui-monospace, Menlo, monospace';
    c.fillText(new Date().toLocaleTimeString('en-GB'), CLOCK.x + 8, CLOCK.y + CLOCK.height / 2);
    c.fillStyle = '#0a66d6';
    for (const click of this.clicks.slice(-6)) {
      c.beginPath();
      c.arc(click.x, click.y, 6, 0, Math.PI * 2);
      c.fill();
    }
  }

  pixels(rect: Rect): Uint8ClampedArray {
    return this.context.getImageData(rect.x, rect.y, rect.width, rect.height).data;
  }
}

/** Server side of RFB 3.8: version, security None, init, then updates on request. */
class RfbServer {
  private buffer = new Uint8Array(0);
  private step: 'version' | 'security' | 'init' | 'ready' = 'version';
  private wanted = false;
  private dirty: Rect | null = null;

  constructor(
    private readonly page: Page,
    private readonly send: (data: ArrayBuffer) => void,
    private readonly inputAllowed: () => boolean,
  ) {}

  start() {
    this.send(new TextEncoder().encode('RFB 003.008\n').buffer as ArrayBuffer);
  }

  changed(rect: Rect) {
    const d = this.dirty;
    this.dirty = d
      ? {
          x: Math.min(d.x, rect.x),
          y: Math.min(d.y, rect.y),
          width: Math.max(d.x + d.width, rect.x + rect.width) - Math.min(d.x, rect.x),
          height: Math.max(d.y + d.height, rect.y + rect.height) - Math.min(d.y, rect.y),
        }
      : rect;
    this.flush();
  }

  feed(data: ArrayBuffer) {
    const next = new Uint8Array(this.buffer.length + data.byteLength);
    next.set(this.buffer);
    next.set(new Uint8Array(data), this.buffer.length);
    this.buffer = next;
    while (this.handle()) {
      // keep reading whole messages
    }
  }

  private take(count: number): DataView | null {
    if (this.buffer.length < count) return null;
    const view = new DataView(this.buffer.buffer.slice(this.buffer.byteOffset, this.buffer.byteOffset + count) as ArrayBuffer);
    this.buffer = this.buffer.slice(count);
    return view;
  }

  private handle(): boolean {
    switch (this.step) {
      case 'version':
        if (!this.take(12)) return false;
        this.send(new Uint8Array([1, 1]).buffer);
        this.step = 'security';
        return true;
      case 'security':
        if (!this.take(1)) return false;
        this.send(new Uint8Array([0, 0, 0, 0]).buffer);
        this.step = 'init';
        return true;
      case 'init': {
        if (!this.take(1)) return false;
        const name = new TextEncoder().encode('openajar mock');
        const init = new DataView(new ArrayBuffer(24 + name.length));
        init.setUint16(0, WIDTH);
        init.setUint16(2, HEIGHT);
        // 32 bpp, depth 24, little endian, true colour, max 255, shifts R0 G8 B16: canvas RGBA bytes.
        [32, 24, 0, 1].forEach((value, index) => init.setUint8(4 + index, value));
        init.setUint16(8, 255);
        init.setUint16(10, 255);
        init.setUint16(12, 255);
        init.setUint8(14, 0);
        init.setUint8(15, 8);
        init.setUint8(16, 16);
        init.setUint32(20, name.length);
        new Uint8Array(init.buffer).set(name, 24);
        this.send(init.buffer);
        this.step = 'ready';
        return true;
      }
      case 'ready':
        return this.message();
    }
  }

  private message(): boolean {
    if (!this.buffer.length) return false;
    const type = this.buffer[0];
    switch (type) {
      case 0:
        return this.take(20) !== null;
      case 2: {
        if (this.buffer.length < 4) return false;
        const count = (this.buffer[2]! << 8) | this.buffer[3]!;
        return this.take(4 + count * 4) !== null;
      }
      case 3: {
        const view = this.take(10);
        if (!view) return false;
        if (view.getUint8(1) === 0) this.dirty = { x: 0, y: 0, width: WIDTH, height: HEIGHT };
        this.wanted = true;
        this.flush();
        return true;
      }
      case 4: {
        const view = this.take(8);
        if (!view) return false;
        if (view.getUint8(1) === 1 && this.inputAllowed()) this.key(view.getUint32(4));
        return true;
      }
      case 5: {
        const view = this.take(6);
        if (!view) return false;
        if (view.getUint8(1) & 1 && this.inputAllowed()) {
          this.page.clicks.push({ x: view.getUint16(2), y: view.getUint16(4) });
          this.page.draw();
          this.changed({ x: 0, y: 0, width: WIDTH, height: HEIGHT });
        }
        return true;
      }
      case 6: {
        if (this.buffer.length < 8) return false;
        const length = new DataView(this.buffer.buffer, this.buffer.byteOffset).getInt32(4);
        return this.take(8 + Math.abs(length)) !== null;
      }
      default:
        // Nothing else is advertised, so nothing else should arrive. Drop what is left.
        this.buffer = new Uint8Array(0);
        return false;
    }
  }

  private key(keysym: number) {
    const page = this.page;
    if (keysym === 0xff08) page.typed = page.typed.slice(0, -1);
    else if (keysym === 0xff0d) page.typed = page.typed ? `${page.typed} (sent)` : '';
    else if (keysym < 0x100 && keysym >= 0x20) page.typed += String.fromCharCode(keysym);
    else if (keysym >= 0x01000100) page.typed += String.fromCodePoint(keysym - 0x01000000);
    else return;
    page.draw();
    this.changed(FIELD);
  }

  private flush() {
    if (!this.wanted || !this.dirty) return;
    const rect = this.dirty;
    this.dirty = null;
    this.wanted = false;
    const pixels = this.page.pixels(rect);
    const message = new Uint8Array(4 + 12 + pixels.length);
    const view = new DataView(message.buffer);
    view.setUint16(2, 1);
    view.setUint16(4, rect.x);
    view.setUint16(6, rect.y);
    view.setUint16(8, rect.width);
    view.setUint16(10, rect.height);
    view.setInt32(12, 0);
    message.set(pixels, 16);
    this.send(message.buffer);
  }
}

const SHELL_HELP = 'Commands: help, ls, whoami, date, uptime, clear, exit. Nothing here runs; it is the mock.';

function shell(lane: string, send: (text: string) => void, close: (code: number, reason: string) => void) {
  const prompt = () => `\x1b[2m${lane}\x1b[0m $ `;
  let line = '';
  send(`openajar mock terminal on ${lane}. Type help.\r\n${prompt()}`);
  const run = (command: string) => {
    switch (command) {
      case '':
        return '';
      case 'help':
        return `${SHELL_HELP}\r\n`;
      case 'ls':
        return 'downloads  notes.md  profile  screenshots\r\n';
      case 'whoami':
        return 'openajar\r\n';
      case 'date':
        return `${new Date().toUTCString()}\r\n`;
      case 'uptime':
        return ' up 3 days,  2 users,  load average: 0.21, 0.18, 0.12\r\n';
      case 'clear':
        return '\x1b[2J\x1b[H';
      default:
        return `${command.split(' ')[0]}: command not found\r\n`;
    }
  };
  return (data: string) => {
    if (data.startsWith('{')) {
      try {
        if ((JSON.parse(data) as { type?: string }).type === 'resize') return;
      } catch {
        // Not a resize: it is typing that starts with a brace.
      }
    }
    for (const char of data) {
      if (char === '\r') {
        const command = line.trim();
        line = '';
        send('\r\n');
        if (command === 'exit') {
          send('logout\r\n');
          setTimeout(() => close(1000, 'exit'), 100);
          return;
        }
        send(run(command) + prompt());
      } else if (char === '\x7f') {
        if (line) {
          line = line.slice(0, -1);
          send('\b \b');
        }
      } else if (char === '\x03') {
        line = '';
        send(`^C\r\n${prompt()}`);
      } else if (char >= ' ') {
        line += char;
        send(char);
      }
    }
  };
}

const link = ws.link(/\/api\/orgs\/[^/]+\/lanes\/[^/]+\/[^/]+\/(vnc|terminal)/);

/**
 * A real WebSocket copies what it is given on send. The mock one passes the same object on a
 * moment later, and noVNC reuses one send buffer, so quick keys would overwrite each other
 * before the mock reads them. Copy at send, as the browser does.
 */
function copyOnSend(socket: WebSocket | undefined) {
  if (!socket) return;
  const send = socket.send.bind(socket);
  socket.send = (data) => send(ArrayBuffer.isView(data) ? new Uint8Array(data.buffer.slice(data.byteOffset, data.byteOffset + data.byteLength)) : data);
}

function connect({ client }: Pick<WebSocketHandlerConnection, 'client'>) {
  const target = parse(client.url.href);
  if (!target) return;
  copyOnSend((client as unknown as { socket?: WebSocket }).socket);
  const { org, id, kind, params } = target;
  let ended = false;
  const timers: ReturnType<typeof setTimeout>[] = [];
  const later = (ms: number, run: () => void) => timers.push(setTimeout(() => !ended && run(), ms));
  const close = (code: number, reason: string) => {
    if (ended) return;
    ended = true;
    timers.forEach(clearTimeout);
    stop();
    client.close(code, reason);
  };
  const control = params.get('mode') === 'control';
  // The lane closing ends what shows it, as on a node. Control moving away ends a driving socket.
  const stop = subscribe(org, (event) => {
    if (event.type === 'lane' && event.payload.id === id && event.payload.state === 'cold') close(1000, 'lane_closed');
    const moved = (event.type === 'lane' && event.payload.id === id) || (event.type === 'control' && event.payload.lane === id);
    if (control && moved && !holds(org, id)) close(4403, 'handed_back');
  });
  client.addEventListener('close', () => {
    ended = true;
    timers.forEach(clearTimeout);
    stop();
  });

  // The relay accepts first and then asks the node, which takes a moment.
  later(scenario.has('stream-slow') ? 4_000 : 400, () => {
    const refused = refusal(org, id, kind, control);
    if (refused) return close(...refused);
    // 1011: the server hit an error. The panel reads it as a drop and retries soon.
    if (scenario.has('stream-flaky')) later(4_000, () => close(1011, ''));
    if (scenario.has('stream-ended')) later(3_000, () => close(1000, ''));

    if (kind === 'terminal') {
      const feed = shell(id, (text) => !ended && client.send(text), close);
      // Read only is the relay's job: nothing typed in a view terminal reaches the shell.
      client.addEventListener('message', (event) => control && typeof event.data === 'string' && feed(event.data));
      return;
    }

    const lane = findLane(org, id)!;
    const page = new Page(id, lane.signal === 'captcha' ? 'captcha' : lane.signal === 'logged_out' ? 'logged_out' : 'none');
    const server = new RfbServer(
      page,
      (data) => !ended && client.send(data),
      () => control && findLane(org, id)?.control.state === 'human',
    );
    client.addEventListener('message', (event) => {
      // noVNC sends a Uint8Array view, which the mock socket passes on as it is.
      if (ArrayBuffer.isView(event.data)) server.feed(event.data.buffer.slice(event.data.byteOffset, event.data.byteOffset + event.data.byteLength) as ArrayBuffer);
      else if (event.data instanceof ArrayBuffer) server.feed(event.data);
      else if (event.data instanceof Blob) void event.data.arrayBuffer().then((buffer) => server.feed(buffer));
      else if (typeof event.data === 'string') server.feed(new TextEncoder().encode(event.data).buffer as ArrayBuffer);
    });
    void page.ready.then(() => {
      if (ended) return;
      page.draw();
      server.start();
      const tick = () => {
        if (ended) return;
        // A still scene keeps screenshots stable, so the clock only ticks when things move.
        if (!scenario.has('still')) {
          page.draw();
          server.changed(CLOCK);
        }
        later(1_000, tick);
      };
      later(1_000, tick);
    });
  });
}

export const streamHandlers = [link.addEventListener('connection', connect)];

