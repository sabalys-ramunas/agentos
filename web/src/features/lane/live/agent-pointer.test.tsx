import { act, render, screen } from '@testing-library/react';
import { QueryClient } from '@tanstack/react-query';
import { applyLiveEvent } from '@/api/live';
import { AgentPointer, POINTER_FADE_MS } from './agent-pointer';

const LANE = 'env/e1-browser';
const point = (x: number, y: number, kind: 'move' | 'press' = 'move') =>
  applyLiveEvent(new QueryClient(), 'local', { type: 'pointer', payload: { lane: LANE, x, y, w: 1920, h: 1080, kind } });
const pointer = () => screen.getByTestId('agent-pointer');

describe("the agent's pointer over the live view", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it('appears where the agent acts, placed on the picture as it is scaled, with the agent named', () => {
    const { rerender } = render(<AgentPointer lane={LANE} name="Claude Code" width={960} live control="agent" />);
    expect(screen.queryByTestId('agent-pointer')).not.toBeInTheDocument();
    act(() => point(400, 300));
    expect(pointer()).toHaveAttribute('data-shown', 'true');
    expect(pointer()).toHaveAttribute('data-jump', 'true');  // it appears there, no glide from nowhere
    expect(pointer().style.transform).toBe('translate3d(200px, 150px, 0)');
    expect(pointer()).toHaveTextContent('Claude Code');
    // the picture zoomed to twice its size: the same point, twice as far
    rerender(<AgentPointer lane={LANE} name="Claude Code" width={1920} live control="agent" />);
    expect(pointer().style.transform).toBe('translate3d(400px, 300px, 0)');
    // and the pointer itself stays one size, whatever the picture's scale
    const body = () => pointer().querySelector<HTMLElement>('.agent-pointer-body')!;
    expect(body().style.transform).toBe('scale(0.8)');
    rerender(<AgentPointer lane={LANE} name="Claude Code" width={480} live control="agent" />);
    expect(body().style.transform).toBe('scale(0.8)');
    act(() => vi.advanceTimersByTime(50));
    expect(pointer()).toHaveAttribute('data-jump', 'false');  // the next point glides
  });

  it('fades after a few quiet seconds, and a press dips the arrow', () => {
    render(<AgentPointer lane={LANE} name="Claude Code" width={960} live control="agent" />);
    act(() => point(10, 10));
    act(() => vi.advanceTimersByTime(POINTER_FADE_MS - 100));
    act(() => point(20, 20, 'press'));
    expect(pointer().querySelector('svg')).toHaveAttribute('data-press', 'true');
    act(() => vi.advanceTimersByTime(POINTER_FADE_MS - 100));
    expect(pointer()).toHaveAttribute('data-shown', 'true');
    act(() => vi.advanceTimersByTime(200));
    expect(pointer()).toHaveAttribute('data-shown', 'false');
  });

  it('hides while a person has control, and when the agent works elsewhere', () => {
    const { rerender } = render(<AgentPointer lane={LANE} name="Claude Code" width={960} live control="agent" />);
    act(() => point(10, 10));
    rerender(<AgentPointer lane={LANE} name="Claude Code" width={960} live control="human" />);
    expect(pointer()).toHaveAttribute('data-shown', 'false');
    rerender(<AgentPointer lane={LANE} name="Claude Code" width={960} live control="agent" />);
    act(() => point(30, 30));
    expect(pointer()).toHaveAttribute('data-shown', 'true');
    act(() => applyLiveEvent(new QueryClient(), 'local', { type: 'pointer', payload: { lane: LANE, hide: 'terminal' } }));
    expect(pointer()).toHaveAttribute('data-shown', 'false');
  });

  it("ignores another lane's points and points off the screen", () => {
    render(<AgentPointer lane={LANE} name="Claude Code" width={960} live control="agent" />);
    act(() => applyLiveEvent(new QueryClient(), 'local', { type: 'pointer', payload: { lane: 'env/other', x: 1, y: 1, w: 10, h: 10 } }));
    act(() => applyLiveEvent(new QueryClient(), 'local', { type: 'pointer', payload: { lane: LANE, x: 50, y: 1, w: 10, h: 10 } }));
    expect(screen.queryByTestId('agent-pointer')).not.toBeInTheDocument();
  });
});
