/**
 * A node's log tail. Logs are relayed from the node and redacted on the way through, and the
 * cloud keeps none of them, so this is a stream and never a query: nothing here is cached.
 */
import { useEffect, useRef, useState } from 'react';
import { fleet } from './client';
import type { LogLine } from './types';

/** Enough for a long session, small enough that filtering stays instant. */
const KEEP = 5_000;
const BACKOFF_MS = [1_000, 2_000, 5_000, 10_000];

export type LogStreamState = 'connecting' | 'open' | 'reconnecting' | 'refused';

export interface SeqLine extends LogLine {
  /** Arrival order. Stable while older lines fall off the front. */
  seq: number;
}

export function useNodeLogs(org: string, id: string, enabled: boolean) {
  const [lines, setLines] = useState<SeqLine[]>([]);
  const [state, setState] = useState<LogStreamState>('connecting');
  const seq = useRef(0);

  useEffect(() => {
    if (!enabled) return;
    let source: EventSource | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let frame = 0;
    let attempt = 0;
    let everOpen = false;
    let stopped = false;
    let pending: SeqLine[] = [];

    // A backlog arrives as hundreds of events at once, so they land in one render per frame.
    const flush = () => {
      frame = 0;
      const batch = pending;
      pending = [];
      setLines((current) => {
        const next = current.concat(batch);
        return next.length > KEEP ? next.slice(next.length - KEEP) : next;
      });
    };

    const connect = () => {
      setState(attempt ? 'reconnecting' : 'connecting');
      source = new EventSource(fleet.nodeLogsUrl(org, id, {}));
      source.onopen = () => {
        attempt = 0;
        everOpen = true;
        setState('open');
      };
      source.addEventListener('log', (event) => {
        let line: LogLine;
        try {
          line = JSON.parse((event as MessageEvent<string>).data) as LogLine;
        } catch {
          return;
        }
        // Node-supplied: keep only the known fields, as text, and cap the length.
        seq.current += 1;
        pending.push({
          seq: seq.current,
          at: String(line.at),
          source: line.source,
          level: line.level,
          message: String(line.message ?? '').slice(0, 2_000),
          lane: line.lane ? String(line.lane) : null,
        });
        if (!frame) frame = requestAnimationFrame(flush);
      });
      source.onerror = () => {
        const closed = source?.readyState === EventSource.CLOSED;
        source?.close();
        if (stopped) return;
        // Refused before it ever opened: the node is offline or this person may not read logs.
        if (closed && !everOpen) {
          setState('refused');
          return;
        }
        setState('reconnecting');
        timer = setTimeout(connect, BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)]);
        attempt += 1;
      };
    };

    setLines([]);
    connect();
    return () => {
      stopped = true;
      source?.close();
      clearTimeout(timer);
      cancelAnimationFrame(frame);
    };
  }, [org, id, enabled]);

  return { lines, state };
}
