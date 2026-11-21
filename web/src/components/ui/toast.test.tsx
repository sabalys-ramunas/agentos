import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { toast } from 'sonner';
import { ApiError } from '@/api/http';
import { renderToasts as stack } from '@/test/render';
import { chosen, switched, toastChange, UNDO_MS } from './toast';

afterEach(() => {
  act(() => toast.dismiss());
  vi.useRealTimers();
});

describe('a toast that says what changed', () => {
  it('names the change in plain words', () => {
    expect(switched('Quiet hours', true)).toEqual({ said: 'Quiet hours turned on', undone: 'Quiet hours turned off again' });
    expect(chosen('Email digest', 'Weekly', 'Daily')).toEqual({ said: 'Email digest set to Weekly', undone: 'Email digest set back to Daily' });
  });

  it('offers Undo, which puts the old value back and says so', async () => {
    stack();
    const undo = vi.fn(() => Promise.resolve());
    act(() => void toastChange({ ...switched('Quiet hours', true), undo }));
    expect(await screen.findByText('Quiet hours turned on')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Undo' }));
    expect(undo).toHaveBeenCalledOnce();
    expect(await screen.findByText('Quiet hours turned off again')).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText('Quiet hours turned on')).not.toBeInTheDocument());
  });

  it('says plainly when Undo could not put it back', async () => {
    stack();
    const undo = () => Promise.reject(new ApiError(500, 'internal', 'The machine did not answer.'));
    act(() => void toastChange({ ...switched('Push to my devices', false), undo }));
    await userEvent.click(await screen.findByRole('button', { name: 'Undo' }));
    expect(await screen.findByText(/^Could not undo, so it stays as it is\./)).toBeInTheDocument();
  });

  it('is announced: it lands in the stack’s polite live region', async () => {
    stack();
    act(() => void toastChange({ ...switched('Quiet hours', true), undo: () => Promise.resolve() }));
    const said = await screen.findByText('Quiet hours turned on');
    expect(said.closest('[aria-live="polite"]')).not.toBeNull();
  });

  it('has no Undo for a change that has nothing to undo', async () => {
    stack();
    act(() => void toastChange({ said: 'Answers in chat turned off' }));
    expect(await screen.findByText('Answers in chat turned off')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Undo' })).toBeNull();
  });

  it('stays about six seconds, and longer while it is hovered or focused', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    stack();
    act(() => void toastChange({ ...switched('Quiet hours', true), undo: () => Promise.resolve() }));
    const said = await screen.findByText('Quiet hours turned on');
    const box = said.closest('[data-change-toast]')!;
    act(() => void vi.advanceTimersByTime(UNDO_MS - 1_000));
    expect(screen.getByText('Quiet hours turned on')).toBeInTheDocument();
    // Held while the pointer is on it, and while Undo has focus.
    fireEvent.mouseEnter(box);
    act(() => void vi.advanceTimersByTime(20_000));
    expect(screen.getByText('Quiet hours turned on')).toBeInTheDocument();
    fireEvent.focus(screen.getByRole('button', { name: 'Undo' }));
    fireEvent.mouseLeave(box);
    act(() => void vi.advanceTimersByTime(20_000));
    expect(screen.getByText('Quiet hours turned on')).toBeInTheDocument();
    // Let go of, it stays a moment more, then goes.
    fireEvent.blur(screen.getByRole('button', { name: 'Undo' }));
    act(() => void vi.advanceTimersByTime(1_500));
    expect(screen.getByText('Quiet hours turned on')).toBeInTheDocument();
    act(() => void vi.advanceTimersByTime(1_500));
    await waitFor(() => expect(screen.queryByText('Quiet hours turned on')).not.toBeInTheDocument());
  });
});
