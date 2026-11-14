import type { NodeConnections, Schedule } from '@/api/types';
import { describeNode, incidentsFrom, listWords, span } from './describe';

const schedule = (cron: string | null, enabled = true): Schedule => ({ id: cron ?? 'watch', flow: 'aster.blog.post', cron, watch: cron ? null : 'feed', params: {}, enabled });

describe('describeNode', () => {
  it('says what a node runs, holds and fires, in one sentence', () => {
    const text = describeNode({
      lanes: 6,
      workspaces: ['Aster Coffee', 'Personal'],
      brains: ['claude-max'],
      schedules: [schedule('0 9 * * *'), schedule('0 18 * * *')],
    });
    expect(text).toBe('Runs 6 accounts for Aster Coffee and Personal, holds the claude-max brain, and has 2 schedules that fire 2 times a day.');
  });

  it('counts a weekly schedule by the week, so it is not read as daily', () => {
    const text = describeNode({ lanes: 1, workspaces: ['Harbor'], brains: [], schedules: [schedule('30 8 * * 1')] });
    expect(text).toBe('Runs 1 account for Harbor, and has 1 schedule that fires 1 time a week.');
  });

  it('leaves out paused schedules and names a watch plainly', () => {
    const text = describeNode({ lanes: 2, workspaces: ['A', 'B', 'C', 'D'], brains: ['x', 'y'], schedules: [schedule(null), schedule('0 9 * * *', false)] });
    expect(text).toBe('Runs 2 accounts for 4 workspaces, holds the x and y brains, and has 1 schedule that fires on a change.');
  });

  it('says nothing about schedules a node does not report', () => {
    expect(describeNode({ lanes: 0, workspaces: [], brains: [], schedules: null })).toBe('Runs no accounts yet.');
  });
});

describe('incidentsFrom', () => {
  const at = (minutes: number) => new Date(Date.UTC(2026, 10, 4, 0, minutes)).toISOString();
  const log = (events: NodeConnections['events']): NodeConnections => ({ uptime_pct: 99, days: [], events });

  it('pairs each disconnect with the next connect, in time order', () => {
    const bands = incidentsFrom(
      log([
        { at: at(50), kind: 'connected', detail: null },
        { at: at(10), kind: 'disconnected', detail: null },
        { at: at(52), kind: 'degraded', detail: null },
      ]),
    );
    expect(bands).toEqual([{ from: Date.parse(at(10)), to: Date.parse(at(50)), label: 'Machine away' }]);
  });

  it('runs an open incident up to now', () => {
    const now = Date.parse(at(90));
    expect(incidentsFrom(log([{ at: at(80), kind: 'disconnected', detail: null }]), now)).toEqual([{ from: Date.parse(at(80)), to: now, label: 'Machine away' }]);
  });

  it('is empty without a log', () => {
    expect(incidentsFrom(null)).toEqual([]);
  });
});

describe('words', () => {
  it('joins lists and spans plainly', () => {
    expect(listWords(['a', 'b', 'c'])).toBe('a, b and c');
    expect(span(42 * 60_000)).toBe('42 min');
    expect(span(130 * 60_000)).toBe('2 h 10 min');
  });
});
