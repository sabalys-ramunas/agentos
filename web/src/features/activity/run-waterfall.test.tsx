import type { Run } from '@/api/types';
import { spansOf } from './run-waterfall';

const T0 = Date.parse('2026-11-04T10:00:00Z');
const at = (seconds: number) => new Date(T0 + seconds * 1_000).toISOString();

function run(overrides: Partial<Run>): Run {
  return {
    id: 'run_1',
    flow: null,
    lane: 'aster/blog',
    status: 'succeeded_verified',
    summary: 'A run',
    principal: 'codex',
    started_at: at(0),
    finished_at: at(30),
    cost: { tokens: 0, usd: 0 },
    proof: null,
    ...overrides,
  };
}

const step = (id: string, seconds: number, status: Run['status'] = 'succeeded_verified') => ({ id, at: at(seconds), tool: id, summary: id, status });
const lengths = (spans: ReturnType<typeof spansOf>['spans']) => spans.map((span) => (span.end - span.start) / 1_000);

describe('spansOf', () => {
  it('reads `at` as when a step finished, so a step spans from the one before', () => {
    const { spans } = spansOf(run({ steps: [step('a', 4), step('b', 5), step('c', 20)] }), T0);
    expect(lengths(spans)).toEqual([4, 1, 15]);
    expect(spans[1]?.start).toBe(T0 + 4_000);
  });

  it('keeps the whole run on the axis, including the tail after the last step', () => {
    const { start, end } = spansOf(run({ steps: [step('a', 4)] }), T0);
    expect((end - start) / 1_000).toBe(30);
  });

  it('opens a bar at `at` for a step still going, and runs it to the clock', () => {
    const live = run({ status: 'running', finished_at: null, steps: [step('a', 4), step('b', 6, 'running')] });
    const { spans, end } = spansOf(live, T0 + 10_000);
    expect(lengths(spans)).toEqual([4, 4]);
    expect(spans[1]).toMatchObject({ live: true, start: T0 + 6_000 });
    expect(end).toBe(T0 + 10_000);
  });

  it('stops the clock when the run was cancelled mid step', () => {
    const stopped = run({ status: 'cancelled', finished_at: at(9), steps: [step('a', 4), step('b', 6, 'running')] });
    const { spans } = spansOf(stopped, T0 + 600_000);
    expect(lengths(spans)).toEqual([4, 3]);
    expect(spans[1]?.live).toBe(false);
  });

  it('never runs backwards when a node clock disagrees', () => {
    const { spans } = spansOf(run({ steps: [step('a', 8), step('b', 3), step('c', 12)] }), T0);
    expect(lengths(spans)).toEqual([8, 0, 4]);
  });

  it('copes with a run that has no steps', () => {
    expect(spansOf(run({ steps: [] }), T0).spans).toEqual([]);
  });
});
