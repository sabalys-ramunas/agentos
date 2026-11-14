import '@testing-library/jest-dom/vitest';
import { cleanup, configure } from '@testing-library/react';
import { setupServer } from 'msw/node';
import { createElement } from 'react';
import { afterAll, afterEach, beforeAll, beforeEach, vi } from 'vitest';
import { handlers, resetDb } from '@/mocks/handlers';
import type * as WebAuthn from '@/lib/webauthn';

// jsdom has no passkey. The mock answers the passkey challenge for a heavy yes (`/proof/options`), so the
// browser's prompt is stood in for here, and only that: the rest of the WebAuthn helper stays real.
vi.mock('@/lib/webauthn', async (importOriginal) => ({
  ...(await importOriginal<typeof WebAuthn>()),
  getPasskey: vi.fn(async () => ({ id: 'mock-credential', type: 'public-key' })),
}));

// The first render in a file also pays for importing its lazy route.
configure({ asyncUtilTimeout: 5_000 });

export const server = setupServer(...handlers);

// Node's fetch rejects relative URLs, which the browser resolves against the page.
const nativeFetch = globalThis.fetch;
globalThis.fetch = ((input: RequestInfo | URL, init?: RequestInit) =>
  nativeFetch(typeof input === 'string' && input.startsWith('/') ? new URL(input, location.origin) : input, init)) as typeof fetch;

class FakeEventSource {
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  addEventListener() {}
  close() {}
}

class FakeResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

// noVNC and xterm need a real canvas and a real socket. Screens get a stand-in that shows what
// they were asked to do; the screens themselves are proven in the browser (e2e/lane-live.spec.ts).
function screenStub(kind: string) {
  return {
    default: (props: { interactive: boolean; holder: string | null; expanded: boolean; onExpandedChange: (next: boolean) => void }) =>
      createElement(
        'div',
        { 'data-testid': `${kind}-screen`, 'data-interactive': String(props.interactive), 'data-holder': props.holder ?? '' },
        createElement('button', { type: 'button', onClick: () => props.onExpandedChange(!props.expanded) }, props.expanded ? 'Leave full window' : 'Full window'),
      ),
  };
}
vi.mock('@/features/lane/live/vnc-screen', () => screenStub('vnc'));
vi.mock('@/features/lane/live/terminal-screen', () => screenStub('terminal'));

vi.stubGlobal('EventSource', FakeEventSource);
vi.stubGlobal('ResizeObserver', FakeResizeObserver);
Element.prototype.scrollIntoView = () => {};
Element.prototype.hasPointerCapture = () => false;
window.scrollTo = () => {};
window.matchMedia = ((query: string) => ({
  matches: false,
  media: query,
  addEventListener() {},
  removeEventListener() {},
})) as unknown as typeof window.matchMedia;

beforeAll(() => server.listen({ onUnhandledFrame: 'error' }));
beforeEach(() => {
  resetDb();
  localStorage.clear();
});
afterEach(() => {
  cleanup();
  server.resetHandlers();
});
afterAll(() => server.close());
