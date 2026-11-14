import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { useSession } from '@/api/hooks';
import { readTechnical, technicalKey, useTechnical, writeTechnical } from './technical';

describe('the Technical details preference', () => {
  beforeEach(() => localStorage.clear());

  it('is off by default', () => {
    expect(readTechnical('usr_DEMO')).toBe(false);
    expect(readTechnical(null)).toBe(false);
  });

  it('is kept per person, so another account in this browser keeps its own', () => {
    writeTechnical('usr_A', true);
    expect(readTechnical('usr_A')).toBe(true);
    expect(readTechnical('usr_B')).toBe(false);
    expect(localStorage.getItem(technicalKey('usr_A'))).toBe('on');
    writeTechnical('usr_A', false);
    expect(localStorage.getItem(technicalKey('usr_A'))).toBeNull();
  });

  it('follows the signed-in person, and flips for every reader at once', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const shared = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
    const { result } = renderHook(() => ({ technical: useTechnical(), session: useSession() }), { wrapper: shared });
    const other = renderHook(() => useTechnical(), { wrapper: shared });
    expect(result.current.technical[0]).toBe(false);
    await waitFor(() => expect(result.current.session.data).toBeDefined());
    act(() => result.current.technical[1](true));
    expect(result.current.technical[0]).toBe(true);
    expect(other.result.current[0]).toBe(true);
    expect(readTechnical(result.current.session.data!.user.id)).toBe(true);
  });
});
