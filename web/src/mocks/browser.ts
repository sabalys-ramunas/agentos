import { setupWorker } from 'msw/browser';
import { handlers } from './handlers';
import { liveHandler, logsHandler } from './live';
import { streamHandlers } from './streams';

/** WebAuthn has no mock of its own, so a resolved fake stands in for the authenticator. */
function stubPasskeys() {
  const credential = {
    id: 'mock-credential',
    type: 'public-key',
    toJSON: () => ({ id: 'mock-credential', type: 'public-key', response: {} }),
  };
  const resolve = () => new Promise((done) => setTimeout(() => done(credential), 500));
  Object.defineProperty(navigator, 'credentials', {
    configurable: true,
    value: { get: resolve, create: resolve },
  });
}

/**
 * The worker's own requests (Deny from a notification) never pass through the mock, so it asks the
 * page to make them. See public/sw-mock.js.
 */
function bridgeWorkerFetch() {
  navigator.serviceWorker?.addEventListener('message', (event: MessageEvent) => {
    const data = event.data as { type?: string; input?: string; init?: RequestInit } | null;
    const port = event.ports[0];
    if (data?.type !== 'agentos-mock-fetch' || !port || !data.input) return;
    void fetch(data.input, data.init)
      .then(async (response) => port.postMessage({ status: response.status, body: await response.text() }))
      .catch(() => port.postMessage({ status: 503, body: '' }));
  });
}

/** The same words as `MOCK_MARKER` in build/guard.ts, which fails any build that holds them. */
const MARKER = 'agentos-mock-backend';

export async function startMockApi() {
  document.documentElement.dataset.backend = MARKER;
  stubPasskeys();
  bridgeWorkerFetch();
  const worker = setupWorker(liveHandler, logsHandler, ...streamHandlers, ...handlers);
  // The app's own service worker with the mock's inside, so push and the offline page work here too.
  await worker.start({ onUnhandledFrame: 'bypass', quiet: true, serviceWorker: { url: '/sw-mock.js', options: { scope: '/' } } });
}
