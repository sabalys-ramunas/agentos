/**
 * Live updates. One EventSource per org feeds `applyLiveEvent`, which patches the query
 * cache. The mock backend answers the same URL with the same event stream, so this file
 * has exactly one code path.
 */
import { evenApproval } from './client';
import type { WireApproval } from './client';
import type { InfiniteData, QueryClient } from '@tanstack/react-query';
import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useSyncExternalStore } from 'react';
import { fleet } from './client';
import { keys } from './keys';
import { LIVE_EVENT_TYPES } from './types';
import type {
  Approval,
  Brain,
  CodeRequest,
  Cost,
  FleetNode,
  Lane,
  LaneControl,
  LiveEvent,
  NodeDetail,
  Page,
  Question,
  Run,
  Schedule,
  Status,
  TraceEvent,
  Workspace,
} from './types';

export type LiveConnection = 'connecting' | 'open' | 'reconnecting' | 'none';

const BACKOFF_MS = [1_000, 2_000, 5_000, 10_000, 30_000];

function patchLane(client: QueryClient, org: string, lane: Lane) {
  client.setQueryData<Lane>(keys.lane(org, lane.id), (current) =>
    current ? { ...current, ...lane } : current,
  );
  client.setQueryData<Workspace[]>(keys.workspaces(org), (current) =>
    current?.map((workspace) =>
      workspace.id === lane.workspace
        ? {
            ...workspace,
            lanes: workspace.lanes.some((item) => item.id === lane.id)
              ? workspace.lanes.map((item) => (item.id === lane.id ? { ...item, ...lane } : item))
              : [...workspace.lanes, lane],
          }
        : workspace,
    ),
  );
  client.setQueryData<NodeDetail>(keys.node(org, lane.node), (current) =>
    current
      ? { ...current, lanes: current.lanes.map((item) => (item.id === lane.id ? lane : item)) }
      : current,
  );
}

/** A decided approval also lives in "You decided today", in its own cache, and in the failed list. */
export function patchDecided(client: QueryClient, org: string, approval: Approval, own = true) {
  if (own) client.setQueryData<Approval>(keys.approval(org, approval.id), (current) => (current ? approval : current));
  const decided = approval.state === 'approved' || approval.state === 'denied';
  client.setQueryData<Approval[]>(keys.decidedApprovals(org), (current) => {
    if (!current) return current;
    const rest = current.filter((item) => item.id !== approval.id);
    return decided ? [approval, ...rest] : rest;
  });
  const failed = (approval.outcome?.state === 'failed' || approval.outcome?.state === 'partial') && !approval.outcome.cleared_at;
  client.setQueryData<Approval[]>(keys.failedApprovals(org), (current) => {
    if (!current) return current;
    const rest = current.filter((item) => item.id !== approval.id);
    return failed && approval.state !== 'pending' ? [approval, ...rest] : rest;
  });
}

function patchApproval(client: QueryClient, org: string, approval: Approval) {
  patchDecided(client, org, approval);
  let delta = 0;
  client.setQueryData<Approval[]>(keys.approvals(org), (current) => {
    if (!current) return current;
    const known = current.some((item) => item.id === approval.id);
    if (approval.state === 'pending') {
      if (known) return current.map((item) => (item.id === approval.id ? approval : item));
      delta = 1;
      return [approval, ...current];
    }
    if (known) delta = -1;
    return current.filter((item) => item.id !== approval.id);
  });
  if (delta !== 0) {
    client.setQueryData<Status>(keys.status(org), (current) =>
      current ? { ...current, needs_you: Math.max(0, current.needs_you + delta) } : current,
    );
  }
}

function patchTrace(client: QueryClient, org: string, trace: TraceEvent) {
  let replayed = false;
  // Only the unfiltered feed is patched. A filtered view picks new rows up on refetch, so
  // a row can never appear under a filter it does not match.
  client.setQueryData<InfiniteData<Page<TraceEvent>>>(keys.activity(org, {}), (current) => {
    const first = current?.pages[0];
    if (!current || !first) return current;
    // A reconnect can replay an event, and its cost must not be counted twice.
    replayed = current.pages.some((page) => page.items.some((item) => item.id === trace.id));
    if (replayed) return current;
    return {
      ...current,
      pages: [{ ...first, items: [trace, ...first.items] }, ...current.pages.slice(1)],
    };
  });
  // A trace may come without a cost (a step that measured none): it counts as nothing, never throws.
  const cost: Partial<Cost> = trace.cost ?? {};
  const tokens = cost.tokens ?? 0;
  const usd = cost.usd ?? 0;
  if (!replayed && (usd > 0 || tokens > 0)) {
    client.setQueryData<Status>(keys.status(org), (current) =>
      current
        ? {
            ...current,
            cost_today: {
              tokens: current.cost_today.tokens + tokens,
              usd: current.cost_today.usd + usd,
            },
          }
        : current,
    );
  }
}

function patchControl(client: QueryClient, org: string, id: string, control: LaneControl) {
  client.setQueryData<Lane>(keys.lane(org, id), (current) => (current ? { ...current, control } : current));
  client.setQueryData<Workspace[]>(keys.workspaces(org), (current) =>
    current?.map((workspace) => ({
      ...workspace,
      lanes: workspace.lanes.map((lane) => (lane.id === id ? { ...lane, control } : lane)),
    })),
  );
}

function patchQuestion(client: QueryClient, org: string, question: Question) {
  let delta = 0;
  client.setQueryData<Question[]>(keys.questions(org), (current) => {
    if (!current) return current;
    const known = current.some((item) => item.id === question.id);
    if (question.state === 'open') {
      if (known) return current.map((item) => (item.id === question.id ? question : item));
      delta = 1;
      return [question, ...current];
    }
    if (known) delta = -1;
    return current.filter((item) => item.id !== question.id);
  });
  if (delta !== 0) {
    client.setQueryData<Status>(keys.status(org), (current) =>
      current ? { ...current, needs_you: Math.max(0, current.needs_you + delta) } : current,
    );
  }
}

/**
 * An open code card joins the list and a closed one leaves it, and Needs you counts the change. The
 * code itself is never here: a card holds the request, and the node holds the code, once.
 */
function changeCodes(client: QueryClient, org: string, id: string, open: CodeRequest | null) {
  let delta = 0;
  client.setQueryData<CodeRequest[]>(keys.codes(org), (current) => {
    if (!current) return current;
    const known = current.some((item) => item.id === id);
    if (open) {
      if (known) return current.map((item) => (item.id === id ? open : item));
      delta = 1;
      return [open, ...current];
    }
    if (known) delta = -1;
    return current.filter((item) => item.id !== id);
  });
  if (delta !== 0) {
    client.setQueryData<Status>(keys.status(org), (current) =>
      current ? { ...current, needs_you: Math.max(0, current.needs_you + delta) } : current,
    );
  }
}

function patchCode(client: QueryClient, org: string, code: CodeRequest) {
  changeCodes(client, org, code.id, code.state === 'open' ? code : null);
}

/** A card the person just answered or declined leaves the list at once, before the live event says so. */
export function dropCode(client: QueryClient, org: string, id: string) {
  changeCodes(client, org, id, null);
}

function patchSchedule(client: QueryClient, org: string, schedule: Schedule & { deleted?: boolean }) {
  client.setQueryData<Schedule[]>(keys.schedules(org), (current) => {
    if (!current) return current;
    if (schedule.deleted) return current.filter((item) => item.id !== schedule.id);
    return current.some((item) => item.id === schedule.id)
      ? current.map((item) => (item.id === schedule.id ? schedule : item))
      : [...current, schedule];
  });
}

function patchBrain(client: QueryClient, org: string, brain: Brain) {
  client.setQueryData<Brain[]>(keys.brains(org), (current) =>
    current?.map((item) => (item.name === brain.name ? { ...item, ...brain } : item)),
  );
}

function patchRun(client: QueryClient, org: string, run: Run) {
  client.setQueryData<Run>(keys.run(org, run.id), (current) =>
    current ? { ...current, ...run } : current,
  );
  // A routine's runs and its place on the list move with each of its runs.
  if (run.flow) {
    void client.invalidateQueries({ queryKey: keys.routineRuns(org, run.flow) });
    void client.invalidateQueries({ queryKey: keys.flows(org) });
  }
}

/**
 * A limit, a pace or a cutoff changed. The meters are read again for everyone who has them. A lane's
 * own page is read again by its id, and a workspace's clock touches every page of the org.
 */
function refreshMeter(client: QueryClient, org: string, payload: { scope?: string; id?: string }) {
  void client.invalidateQueries({ queryKey: keys.meters(org) });
  if (payload.scope === 'workspace') return void client.invalidateQueries({ queryKey: keys.account(org) });
  if (payload.id) return void client.invalidateQueries({ queryKey: keys.laneAccount(org, payload.id) });
  return undefined;
}

function patchNode(client: QueryClient, org: string, node: FleetNode) {
  client.setQueryData<FleetNode[]>(keys.nodes(org), (current) => {
    if (!current) return current;
    return current.some((item) => item.id === node.id)
      ? current.map((item) => (item.id === node.id ? { ...item, ...node } : item))
      : [...current, node];
  });
  client.setQueryData<NodeDetail>(keys.node(org, node.id), (current) =>
    current ? { ...current, ...node } : current,
  );
  client.setQueryData<Status>(keys.status(org), (current) => {
    if (!current) return current;
    const entry = { id: node.id, state: node.state, degraded: node.degraded };
    return {
      ...current,
      nodes: current.nodes.some((item) => item.id === node.id)
        ? current.nodes.map((item) => (item.id === node.id ? entry : item))
        : [...current.nodes, entry],
    };
  });
}

/** The single entry point for every live change, real or mocked. */
export function applyLiveEvent(client: QueryClient, org: string, event: LiveEvent) {
  switch (event.type) {
    case 'lane':
      return patchLane(client, org, event.payload);
    case 'control':
      return patchControl(client, org, event.payload.lane, event.payload.control);
    case 'question':
      return patchQuestion(client, org, event.payload);
    case 'code':
      return patchCode(client, org, event.payload);
    case 'freeze':
      // What is frozen is read again: the freeze itself, and every page that shows it, follow the node.
      return void client.invalidateQueries({ queryKey: keys.freeze(org) });
    case 'schedule':
      return patchSchedule(client, org, event.payload);
    case 'brain':
      return patchBrain(client, org, event.payload);
    case 'task':
      // Brain tasks have no screen yet, so there is nothing cached to patch.
      return undefined;
    case 'resync':
      // Events were missed and cannot be replayed, so read everything again.
      return void client.invalidateQueries({ queryKey: keys.org(org) });
    case 'approval':
      return patchApproval(client, org, evenApproval(event.payload as WireApproval));
    case 'trace':
      return patchTrace(client, org, event.payload);
    case 'run':
      return patchRun(client, org, event.payload);
    case 'node':
      return patchNode(client, org, event.payload);
    case 'meter':
      return refreshMeter(client, org, event.payload);
  }
}

let connection: LiveConnection = 'connecting';
const listeners = new Set<() => void>();

function setConnection(next: LiveConnection) {
  if (next === connection) return;
  connection = next;
  listeners.forEach((listener) => listener());
}

export function useLiveConnection(): LiveConnection {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => connection,
  );
}

/** Opens the org's event stream for as long as the shell is mounted. */
export function useLiveEvents(org: string) {
  const client = useQueryClient();

  useEffect(() => {
    let source: EventSource | null = null;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let attempt = 0;
    let stopped = false;
    let everOpen = false;
    let lastEventId = '';

    const connect = () => {
      // This loop closes and reopens the stream itself, so it carries the resume point too.
      const resume = lastEventId ? `?last_event_id=${encodeURIComponent(lastEventId)}` : '';
      source = new EventSource(`${fleet.eventsUrl(org)}${resume}`);
      source.onopen = () => {
        // Events were missed while the stream was down, so catch up once.
        if (attempt > 0) void client.invalidateQueries({ queryKey: keys.org(org) });
        attempt = 0;
        everOpen = true;
        setConnection('open');
      };
      source.onerror = () => {
        source?.close();
        if (stopped) return;
        // Refused outright, so there is no stream here to lose. Say nothing and stop trying.
        if (source?.readyState === EventSource.CLOSED && !everOpen) {
          setConnection('none');
          return;
        }
        setConnection('reconnecting');
        const wait = BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)];
        attempt += 1;
        timer = setTimeout(connect, wait);
      };
      for (const type of LIVE_EVENT_TYPES) {
        source.addEventListener(type, (message) => {
          const event = message as MessageEvent<string>;
          if (event.lastEventId) lastEventId = event.lastEventId;
          const payload: unknown = event.data ? JSON.parse(event.data) : null;
          applyLiveEvent(client, org, { type, payload } as LiveEvent);
        });
      }
    };

    setConnection('connecting');
    connect();

    return () => {
      stopped = true;
      clearTimeout(timer);
      source?.close();
    };
  }, [client, org]);
}
