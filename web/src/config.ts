/** The single switch between the mock backend and the real API. */
export const apiMode: 'mock' | 'live' = import.meta.env.VITE_API_MODE === 'mock' ? 'mock' : 'live';
