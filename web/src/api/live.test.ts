import { QueryClient } from '@tanstack/react-query';
import { vi } from 'vitest';
import { idleControl } from '@/lib/lanes';
import { keys } from './keys';
import { applyLiveEvent } from './live';
import type { Approval, FleetNode, Lane, Page, Question, Schedule, Status, TraceEvent, Workspace } from './types';

const ORG = 'northwind-group';

const lane = (patch: Partial<Lane> = {}): Lane => ({
  id: 'aster/instagram',
  workspace: 'aster',
  name: 'instagram',
  kind: 'browser',
  engine: 'camoufox',
  preset: 'account',
  state: 'warm',
  signal: 'ok',
  proxy: null,
  exit_ip: null,
  last_used: null,
  node: 'home-mini',
  quarantine_reason: null,
  logged_in_hint: 'ok',
  control: { ...idleControl },
  hold: null,
  hold_until: null,
  signal_detail: null,
  account: null,
  ...patch,
});

const approval = (id: string, state: Approval['state'] = 'pending'): Approval => ({
  id,
  state,
  effect: 'commit',
  lane: 'aster/instagram',
  summary: 'Post',
  content_hash: 'sha256:a',
  preview: {},
  requested_by: { kind: 'agent', id: 'claude-code' },
  created_at: '2026-11-04T00:00:00Z',
  expires_at: '2026-11-05T00:00:00Z',
});

const trace = (id: string): TraceEvent => ({
  id,
  at: '2026-11-04T00:00:00Z',
  kind: 'tool_call',
  tool: 'browser.read',
  lane: 'aster/instagram',
  principal: 'claude-code',
  effect: 'read',
  status: 'succeeded_verified',
  summary: 'Read',
  cost: { tokens: 100, usd: 0.5 },
  run_id: null,
});

const status = { needs_you: 2, cost_today: { tokens: 1000, usd: 1 }, nodes: [{ id: 'home-mini', state: 'up', degraded: false }] } as Status;

function client() {
  const value = new QueryClient();
  value.setQueryData(keys.status(ORG), status);
  return value;
}

describe('applyLiveEvent', () => {
  it('reads a routine\'s runs and the routines list again when one of its runs moves (B6)', () => {
    const qc = client();
    const spy = vi.spyOn(qc, 'invalidateQueries');
    applyLiveEvent(qc, ORG, { type: 'run', payload: { id: 'run_1', flow: 'biz-a.social.post', lane: 'biz-a/social', status: 'queued' } as never });
    expect(spy).toHaveBeenCalledWith({ queryKey: keys.routineRuns(ORG, 'biz-a.social.post') });
    expect(spy).toHaveBeenCalledWith({ queryKey: keys.flows(ORG) });
  });

  it('patches a lane wherever it is cached', () => {
    const qc = client();
    qc.setQueryData<Workspace[]>(keys.workspaces(ORG), [
      { id: 'aster', name: 'Aster', color: '#000', health: 'ok', summary: '', lanes: [lane()] },
    ]);
    qc.setQueryData(keys.lane(ORG, 'aster/instagram'), lane());
    applyLiveEvent(qc, ORG, { type: 'lane', payload: lane({ state: 'active' }) });
    expect(qc.getQueryData<Workspace[]>(keys.workspaces(ORG))?.[0]?.lanes[0]?.state).toBe('active');
    expect(qc.getQueryData<Lane>(keys.lane(ORG, 'aster/instagram'))?.state).toBe('active');
  });

  it('reads the meters again, and only the lane whose limit or pace changed', () => {
    const qc = client();
    const spy = vi.spyOn(qc, 'invalidateQueries');
    applyLiveEvent(qc, ORG, { type: 'meter', payload: { scope: 'lane', id: 'aster/instagram', kind: 'limits' } });
    expect(spy).toHaveBeenCalledWith({ queryKey: keys.meters(ORG) });
    expect(spy).toHaveBeenCalledWith({ queryKey: keys.laneAccount(ORG, 'aster/instagram') });
    expect(spy).not.toHaveBeenCalledWith({ queryKey: keys.laneAccount(ORG, 'aster/shop') });
  });

  it('reads every account page again when a workspace clock changes', () => {
    const qc = client();
    const spy = vi.spyOn(qc, 'invalidateQueries');
    applyLiveEvent(qc, ORG, { type: 'meter', payload: { scope: 'workspace', id: 'aster', kind: 'pace' } });
    expect(spy).toHaveBeenCalledWith({ queryKey: keys.account(ORG) });
  });

  it('adds a lane the panel has not seen', () => {
    const qc = client();
    qc.setQueryData<Workspace[]>(keys.workspaces(ORG), [
      { id: 'aster', name: 'Aster', color: '#000', health: 'ok', summary: '', lanes: [] },
    ]);
    applyLiveEvent(qc, ORG, { type: 'lane', payload: lane() });
    expect(qc.getQueryData<Workspace[]>(keys.workspaces(ORG))?.[0]?.lanes).toHaveLength(1);
  });

  it('adds a new approval to the top and counts it', () => {
    const qc = client();
    qc.setQueryData(keys.approvals(ORG), [approval('a')]);
    applyLiveEvent(qc, ORG, { type: 'approval', payload: approval('b') });
    expect(qc.getQueryData<Approval[]>(keys.approvals(ORG))?.map((item) => item.id)).toEqual(['b', 'a']);
    expect(qc.getQueryData<Status>(keys.status(ORG))?.needs_you).toBe(3);
  });

  it('removes an approval decided elsewhere and counts it once', () => {
    const qc = client();
    qc.setQueryData(keys.approvals(ORG), [approval('a'), approval('b')]);
    applyLiveEvent(qc, ORG, { type: 'approval', payload: approval('a', 'approved') });
    applyLiveEvent(qc, ORG, { type: 'approval', payload: approval('a', 'approved') });
    expect(qc.getQueryData<Approval[]>(keys.approvals(ORG))?.map((item) => item.id)).toEqual(['b']);
    expect(qc.getQueryData<Status>(keys.status(ORG))?.needs_you).toBe(1);
  });

  it('prepends a trace once and adds its cost', () => {
    const qc = client();
    const key = keys.activity(ORG, {});
    qc.setQueryData(key, { pages: [{ items: [trace('old')], next_before: null } satisfies Page<TraceEvent>], pageParams: [undefined] });
    applyLiveEvent(qc, ORG, { type: 'trace', payload: trace('new') });
    applyLiveEvent(qc, ORG, { type: 'trace', payload: trace('new') });
    const data = qc.getQueryData<{ pages: Page<TraceEvent>[] }>(key);
    expect(data?.pages[0]?.items.map((item) => item.id)).toEqual(['new', 'old']);
    expect(qc.getQueryData<Status>(keys.status(ORG))?.cost_today).toEqual({ tokens: 1100, usd: 1.5 });
  });

  it('keeps a trace that comes without a cost and counts nothing for it', () => {
    const qc = client();
    const key = keys.activity(ORG, {});
    qc.setQueryData(key, { pages: [{ items: [], next_before: null }], pageParams: [undefined] });
    const bare = { ...trace('bare'), cost: undefined } as unknown as TraceEvent;
    expect(() => applyLiveEvent(qc, ORG, { type: 'trace', payload: bare })).not.toThrow();
    const data = qc.getQueryData<{ pages: Page<TraceEvent>[] }>(key);
    expect(data?.pages[0]?.items.map((item) => item.id)).toEqual(['bare']);
    expect(qc.getQueryData<Status>(keys.status(ORG))?.cost_today).toEqual({ tokens: 1000, usd: 1 });
  });

  it('leaves a filtered activity view alone', () => {
    const qc = client();
    const key = keys.activity(ORG, { status: 'failed' });
    const page = { pages: [{ items: [], next_before: null }], pageParams: [undefined] };
    qc.setQueryData(key, page);
    applyLiveEvent(qc, ORG, { type: 'trace', payload: trace('new') });
    expect(qc.getQueryData(key)).toEqual(page);
  });

  it('adds a newly enrolled node to the fleet and the top bar', () => {
    const qc = client();
    qc.setQueryData<FleetNode[]>(keys.nodes(ORG), []);
    const node = { id: 'edge-3', state: 'up', degraded: false } as FleetNode;
    applyLiveEvent(qc, ORG, { type: 'node', payload: node });
    expect(qc.getQueryData<FleetNode[]>(keys.nodes(ORG))).toHaveLength(1);
    expect(qc.getQueryData<Status>(keys.status(ORG))?.nodes).toHaveLength(2);
  });

  it('patches who holds a lane from a control event', () => {
    const qc = client();
    qc.setQueryData(keys.lane(ORG, 'aster/instagram'), lane());
    qc.setQueryData<Workspace[]>(keys.workspaces(ORG), [{ id: 'aster', name: 'Aster', color: '#000', health: 'ok', summary: '', lanes: [lane()] }]);
    const control = { ...idleControl, state: 'human' as const, by: 'Mara' };
    applyLiveEvent(qc, ORG, { type: 'control', payload: { lane: 'aster/instagram', control } });
    expect(qc.getQueryData<Lane>(keys.lane(ORG, 'aster/instagram'))?.control.state).toBe('human');
    expect(qc.getQueryData<Workspace[]>(keys.workspaces(ORG))?.[0]?.lanes[0]?.control.by).toBe('Mara');
  });

  it('adds an open question and removes an answered one, counting each once', () => {
    const qc = client();
    qc.setQueryData(keys.questions(ORG), []);
    const question = { id: 'q1', state: 'open' } as Question;
    applyLiveEvent(qc, ORG, { type: 'question', payload: question });
    expect(qc.getQueryData<Status>(keys.status(ORG))?.needs_you).toBe(3);
    applyLiveEvent(qc, ORG, { type: 'question', payload: { ...question, state: 'answered' } });
    applyLiveEvent(qc, ORG, { type: 'question', payload: { ...question, state: 'answered' } });
    expect(qc.getQueryData<Question[]>(keys.questions(ORG))).toEqual([]);
    expect(qc.getQueryData<Status>(keys.status(ORG))?.needs_you).toBe(2);
  });

  it('applies and removes schedules', () => {
    const qc = client();
    qc.setQueryData<Schedule[]>(keys.schedules(ORG), [{ id: 's1', flow: 'x', params: {} }]);
    applyLiveEvent(qc, ORG, { type: 'schedule', payload: { id: 's1', flow: 'x', params: {}, enabled: false } });
    expect(qc.getQueryData<Schedule[]>(keys.schedules(ORG))?.[0]?.enabled).toBe(false);
    applyLiveEvent(qc, ORG, { type: 'schedule', payload: { id: 's1', flow: 'x', params: {}, deleted: true } });
    expect(qc.getQueryData<Schedule[]>(keys.schedules(ORG))).toEqual([]);
  });

  it('reads everything again on resync', () => {
    const qc = client();
    qc.setQueryData(keys.approvals(ORG), []);
    applyLiveEvent(qc, ORG, { type: 'resync', payload: {} });
    expect(qc.getQueryState(keys.approvals(ORG))?.isInvalidated).toBe(true);
  });

  it('does nothing to a query that was never loaded', () => {
    const qc = new QueryClient();
    applyLiveEvent(qc, ORG, { type: 'approval', payload: approval('a') });
    expect(qc.getQueryData(keys.approvals(ORG))).toBeUndefined();
  });
});
