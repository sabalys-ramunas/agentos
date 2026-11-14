import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { streamCopy } from './copy';
import { backoff, IDLE, readClose, StreamController, streamUrl } from './stream';
import type { ControllerOptions, StreamState } from './stream';

class FakeSocket extends EventTarget {
  static all: FakeSocket[] = [];
  readyState: number = WebSocket.CONNECTING;
  binaryType = 'blob';
  closedWith: number | null = null;
  constructor(readonly url: string) {
    super();
    FakeSocket.all.push(this);
  }
  open() {
    this.readyState = WebSocket.OPEN;
    this.dispatchEvent(new Event('open'));
  }
  /** The far end closes. */
  drop(code: number, reason = '') {
    this.readyState = WebSocket.CLOSED;
    this.dispatchEvent(Object.assign(new Event('close'), { code, reason }));
  }
  close(code?: number) {
    this.closedWith = code ?? 1000;
    this.readyState = WebSocket.CLOSED;
  }
}

function setup(overrides: Partial<ControllerOptions> = {}) {
  const states: StreamState[] = [];
  const lives: (() => void)[] = [];
  const detached: FakeSocket[] = [];
  const controller = new StreamController({
    open: (url) => new FakeSocket(url) as unknown as WebSocket,
    attach: (socket, live) => {
      lives.push(live);
      return () => detached.push(socket as unknown as FakeSocket);
    },
    onChange: (state) => states.push(state),
    random: () => 0.5,
    now: () => 0,
    ...overrides,
  });
  const last = () => states.at(-1)!;
  const socket = () => FakeSocket.all.at(-1)!;
  return { controller, states, lives, detached, last, socket };
}

beforeEach(() => {
  FakeSocket.all = [];
  vi.useFakeTimers();
});
afterEach(() => vi.useRealTimers());

describe('readClose', () => {
  it('reads the relay codes', () => {
    expect(readClose({ code: 4401, reason: '', opened: true })).toEqual({ phase: 'signed_out' });
    expect(readClose({ code: 4403, reason: 'revoked', opened: true })).toEqual({ phase: 'denied', reason: 'revoked' });
    expect(readClose({ code: 4403, reason: 'rate limited', opened: true })).toEqual({ retry: 'limited', reason: 'rate limited' });
    expect(readClose({ code: 4403, reason: 'handed_back', opened: true })).toEqual({ retry: 'switch', reason: 'handed_back' });
    expect(readClose({ code: 4403, reason: 'not_holding', opened: true })).toEqual({ retry: 'switch', reason: 'not_holding' });
    expect(readClose({ code: 4404, reason: '', opened: true })).toEqual({ phase: 'missing' });
    expect(readClose({ code: 4202, reason: 'Shell on personal/shell', opened: true })).toEqual({
      phase: 'waiting',
      reason: 'Shell on personal/shell',
    });
    expect(readClose({ code: 4503, reason: 'node offline', opened: true })).toEqual({ retry: 'away', reason: 'node offline' });
    expect(readClose({ code: 4503, reason: 'service_unavailable', opened: true })).toEqual({ phase: 'ended', reason: 'service_unavailable' });
    expect(readClose({ code: 1000, reason: 'node_gone', opened: true })).toEqual({ retry: 'away', reason: 'node_gone' });
    expect(readClose({ code: 1000, reason: 'exit', opened: true })).toEqual({ phase: 'ended', reason: 'exit' });
    expect(readClose({ code: 1006, reason: '', opened: true })).toEqual({ retry: 'drop', reason: null });
  });

  it('cannot read a refusal made before the socket opened, so it asks', () => {
    expect(readClose({ code: 1006, reason: '', opened: false })).toEqual({ probe: true });
  });
});

describe('backoff', () => {
  it('grows and caps, with a fifth of jitter either way', () => {
    const mid = () => 0.5;
    expect([1, 2, 3, 6, 9].map((attempt) => backoff(attempt, 'drop', mid))).toEqual([1000, 2000, 4000, 30000, 30000]);
    expect(backoff(1, 'away', mid)).toBe(5000);
    expect(backoff(1, 'drop', () => 0)).toBe(800);
    expect(backoff(1, 'drop', () => 1)).toBe(1200);
  });
});

describe('streamUrl', () => {
  it('builds a same-origin WebSocket URL', () => {
    expect(streamUrl('acme', 'biz-a', 'instagram', 'vnc', { mode: 'view' }, { protocol: 'https:', host: 'app.example' })).toBe(
      'wss://app.example/api/orgs/acme/lanes/biz-a/instagram/vnc?mode=view',
    );
    expect(streamUrl('acme', 'p', 'shell', 'terminal', { cols: 80, session: undefined }, { protocol: 'http:', host: 'localhost:5391' })).toBe(
      'ws://localhost:5391/api/orgs/acme/lanes/p/shell/terminal?cols=80',
    );
  });
});

describe('StreamController', () => {
  it('is live only once content arrives, not when the socket opens', () => {
    const { controller, last, lives, socket } = setup();
    controller.setUrl('ws://x/vnc');
    expect(last().phase).toBe('connecting');
    socket().open();
    expect(last().phase).toBe('connecting');
    lives[0]!();
    expect(last().phase).toBe('live');
  });

  it('reconnects with backoff after a drop, then gives up and says so', () => {
    const { controller, last, socket } = setup();
    controller.setUrl('ws://x/vnc');
    for (let attempt = 1; attempt <= 6; attempt += 1) {
      socket().open();
      socket().drop(1006);
      const delay = backoff(attempt, 'drop', () => 0.5);
      expect(last()).toMatchObject({ phase: 'reconnecting', retryAt: delay });
      vi.advanceTimersByTime(delay);
    }
    socket().open();
    socket().drop(1006);
    expect(last().phase).toBe('failed');
    expect(FakeSocket.all).toHaveLength(7);
  });

  it('counts tries again once a connection has been live', () => {
    const { controller, last, lives, socket } = setup();
    controller.setUrl('ws://x/vnc');
    socket().open();
    socket().drop(1006);
    vi.advanceTimersByTime(1_000);
    socket().open();
    lives[1]!();
    socket().drop(1011);
    expect(last()).toMatchObject({ phase: 'reconnecting', retryAt: 1000 });
  });

  it('waits for a node that is away, and keeps waiting', () => {
    const { controller, last, socket } = setup();
    controller.setUrl('ws://x/vnc');
    for (let attempt = 0; attempt < 10; attempt += 1) {
      socket().open();
      socket().drop(4503, 'node offline');
      expect(last().phase).toBe('away');
      vi.advanceTimersByTime(30_000);
    }
    expect(FakeSocket.all).toHaveLength(11);
  });

  it('does not retry what cannot heal by itself', () => {
    for (const [code, phase] of [
      [4401, 'signed_out'],
      [4403, 'denied'],
      [4404, 'missing'],
      [4202, 'waiting'],
      [1000, 'ended'],
    ] as const) {
      FakeSocket.all = [];
      const { controller, last, socket } = setup();
      controller.setUrl('ws://x/terminal');
      socket().open();
      socket().drop(code, code === 1000 ? 'exit' : '');
      expect(last().phase).toBe(phase);
      vi.advanceTimersByTime(60_000);
      expect(FakeSocket.all).toHaveLength(1);
    }
  });

  it('asks the API why when the relay refuses before opening', async () => {
    const probe = vi.fn().mockResolvedValue('signed_out');
    const { controller, last, socket } = setup({ probe });
    controller.setUrl('ws://x/vnc');
    socket().drop(1006);
    await vi.waitFor(() => expect(last().phase).toBe('signed_out'));
    expect(probe).toHaveBeenCalledOnce();
  });

  it('stops after three refusals the API cannot explain', async () => {
    const { controller, last, socket } = setup({ probe: async () => null });
    controller.setUrl('ws://x/vnc');
    for (let attempt = 0; attempt < 3; attempt += 1) {
      socket().drop(1006);
      await vi.advanceTimersByTimeAsync(5_000);
    }
    expect(last()).toMatchObject({ phase: 'failed', reason: 'refused' });
    expect(FakeSocket.all).toHaveLength(3);
  });

  it('keeps the live picture while switching mode, and drops it once the new one is live', () => {
    const { controller, last, lives, detached, socket } = setup();
    controller.setUrl('ws://x/vnc?mode=view');
    const first = socket();
    first.open();
    lives[0]!();
    controller.setUrl('ws://x/vnc?mode=control');
    expect(last()).toMatchObject({ phase: 'live', switching: true });
    expect(detached).toEqual([]);
    socket().open();
    lives[1]!();
    expect(last()).toMatchObject({ phase: 'live', switching: false });
    expect(detached).toEqual([first]);
    expect(first.closedWith).toBe(1000);
  });

  it('goes back to watching when control moves away, and reads the lane again', () => {
    const lost = vi.fn();
    const { controller, lives, last, socket } = setup({ onControlLost: lost });
    controller.setUrl('ws://x/vnc?mode=control');
    socket().open();
    lives.at(-1)!();
    socket().drop(4403, 'handed_back');
    expect(lost).toHaveBeenCalledTimes(1);
    expect(last()).toMatchObject({ phase: 'reconnecting', reason: 'handed_back' });
    expect(streamCopy(last(), 'vnc', 'home-mini', 0).title).toBe('Back to watching');
    // The lane's new control arrives and the screen asks for the view socket.
    controller.setUrl('ws://x/vnc?mode=view');
    expect(socket().url).toBe('ws://x/vnc?mode=view');
    socket().open();
    lives.at(-1)!();
    expect(last().phase).toBe('live');
  });

  it('says "take over to type" when the relay twice refuses control, instead of knocking forever', () => {
    const { controller, last, socket } = setup();
    controller.setUrl('ws://x/terminal?mode=control');
    socket().open();
    socket().drop(4403, 'not_holding');
    expect(last()).toMatchObject({ phase: 'reconnecting', reason: 'not_holding' });
    vi.advanceTimersByTime(600);
    socket().open();
    socket().drop(4403, 'not_holding');
    expect(last()).toMatchObject({ phase: 'denied', reason: 'not_holding' });
    expect(streamCopy(last(), 'terminal', 'home-mini', 0).title).toBe('Take over to type');
    const count = FakeSocket.all.length;
    vi.advanceTimersByTime(60_000);
    expect(FakeSocket.all.length).toBe(count);
  });

  it('treats a connection that opens and shows nothing as a node away', () => {
    const { controller, last, socket } = setup({ liveTimeoutMs: 1_000 });
    controller.setUrl('ws://x/vnc');
    socket().open();
    vi.advanceTimersByTime(1_000);
    expect(last()).toMatchObject({ phase: 'away', reason: 'node did not answer' });
    expect(socket().closedWith).toBe(1000);
  });

  it('closes everything on null and on dispose, and ignores the closes it caused', () => {
    const { controller, last, socket, states } = setup();
    controller.setUrl('ws://x/vnc');
    socket().open();
    controller.setUrl(null);
    expect(last()).toEqual(IDLE);
    socket().drop(1000);
    expect(last()).toEqual(IDLE);
    controller.setUrl('ws://x/vnc');
    controller.dispose();
    const count = states.length;
    socket().drop(1006);
    expect(states).toHaveLength(count);
  });
});

describe('streamCopy', () => {
  it('says what happened and when the next try is, with no em dash', () => {
    const away = streamCopy({ phase: 'away', reason: null, retryAt: 12_400, switching: false }, 'vnc', 'home-mini', 0);
    expect(away.title).toBe('home-mini is away');
    expect(away.body).toContain('Trying again in 13 s.');
    const ended = streamCopy({ phase: 'ended', reason: 'exit', retryAt: null, switching: false }, 'terminal', 'home-mini', 0);
    expect(ended.body).toBe('The shell exited.');
    const phases = ['idle', 'connecting', 'live', 'reconnecting', 'away', 'waiting', 'signed_out', 'denied', 'missing', 'ended', 'failed'] as const;
    for (const phase of phases) {
      const copy = streamCopy({ phase, reason: null, retryAt: null, switching: false }, 'vnc', 'n', 0);
      expect(`${copy.title}${copy.body}${copy.label}`).not.toContain(String.fromCharCode(0x2014));
    }
  });
});
