/**
 * The mock event stream. It answers the same URL as the real API with a real
 * text/event-stream, so the panel's EventSource code cannot tell the difference.
 * Live changes come from mutations (handlers.ts) and from this ticker.
 */
import { sse } from 'msw';
import type { Lane, LiveEvent, LogLine, TraceEvent } from '@/api/types';
import { laneName } from '@/lib/labels';
import { emit, subscribe } from './bus';
import { logBacklog, nextLog, startTraffic } from './fleet';
import { db } from './handlers';
import { scenario } from './scenario';
import { makeIds } from './seed';
import type { NodeRecord, OrgData } from './seed';

type Events = { [K in LiveEvent['type']]: Extract<LiveEvent, { type: K }>['payload'] };

export const liveHandler = sse<Events>('/api/orgs/:org/events', ({ client, params, request }) => {
  const org = String(params.org);
  const unsubscribe = subscribe(org, (event: LiveEvent) => {
    try {
      client.send({ event: event.type, data: event.payload } as Parameters<typeof client.send>[0]);
    } catch {
      unsubscribe();
    }
  });
  request.signal.addEventListener('abort', unsubscribe);
  if (!scenario.has('still')) startTicker(org);
});

/** Logs are relayed from the node, so this stream has its own route and keeps nothing. */
export const logsHandler = sse<{ log: LogLine }>('/api/orgs/:org/nodes/:id/logs/stream', ({ client, params, request }) => {
  const data = db.orgs.get(String(params.org));
  const node = data?.nodes.find((item) => item.id === params.id);
  if (!data || !node) return;
  const send = (line: LogLine) => client.send({ event: 'log', data: line });
  logBacklog(data, node, 240).forEach(send);
  if (scenario.has('still')) return;
  const loop = () => {
    try {
      send(nextLog(data, node));
    } catch {
      return;
    }
    timer = setTimeout(loop, 250 + Math.random() * 1_100);
  };
  let timer = setTimeout(loop, 500);
  request.signal.addEventListener('abort', () => clearTimeout(timer));
});

const ids = makeIds(Date.now() % 99_991);
const tickers = new Map<string, ReturnType<typeof setInterval>>();
const traffic = new Map<string, () => void>();

function pick<T>(items: T[]): T | undefined {
  return items[Math.floor(Math.random() * items.length)];
}

function jitter(node: NodeRecord) {
  const cpu = Math.max(3, Math.min(92, node.cpu + Math.round((Math.random() - 0.5) * 14)));
  const used = node.mem.used_mb + Math.round((Math.random() - 0.5) * 300);
  node.cpu = cpu;
  node.load.cpu_pct = cpu;
  node.mem.used_mb = used;
  node.load.mem_used_mb = used;
  node.last_seen = new Date().toISOString();
}

function tick(data: OrgData, count: number) {
  const slug = data.org.slug;
  for (const node of data.nodes) {
    jitter(node);
    const { os: _os, arch: _arch, load: _load, ...payload } = node;
    emit(slug, { type: 'node', payload });
  }
  if (count % 3 === 0) {
    const lane = pick(data.lanes.filter((item) => item.state === 'warm' || item.state === 'active'));
    if (lane) {
      const trace: TraceEvent = {
        id: ids('trc'),
        at: new Date().toISOString(),
        kind: 'tool_call',
        tool: 'browser.read',
        lane: lane.id,
        principal: 'claude-code',
        effect: 'read',
        status: 'succeeded_verified',
        summary: `Checked notifications on ${laneName(lane.name)}`,
        cost: { tokens: 900, usd: 0.01 },
        run_id: null,
      };
      data.activity.unshift(trace);
      data.costToday = {
        tokens: data.costToday.tokens + trace.cost.tokens,
        usd: data.costToday.usd + trace.cost.usd,
      };
      emit(slug, { type: 'trace', payload: trace });
    }
  }
  if (count % 5 === 0) {
    const lane = pick(data.lanes.filter((item) => item.state === 'warm' || item.state === 'active'));
    if (lane && lane.signal === 'ok') {
      const next: Lane = { ...lane, state: lane.state === 'warm' ? 'active' : 'warm', last_used: new Date().toISOString() };
      data.lanes[data.lanes.indexOf(lane)] = next;
      emit(slug, { type: 'lane', payload: next });
    }
  }
}

function startTicker(slug: string) {
  const existing = tickers.get(slug);
  if (existing) clearInterval(existing);
  traffic.get(slug)?.();
  const world = db.orgs.get(slug);
  if (world && world.lanes.length > 0) traffic.set(slug, startTraffic(world, slug));
  let count = 0;
  const timer = setInterval(() => {
    const data = db.orgs.get(slug);
    if (data) tick(data, (count += 1));
  }, 5_000);
  tickers.set(slug, timer);
}
