/*
 * The service worker in mock mode only: the real worker, then the mock API's, plus two bridges the
 * mock needs because it has no push service and no network. The build refuses to ship this file.
 */
self.OPENAJAR_MOCK = true;

/** The worker's own requests (Deny from a notification) go through an open panel, where the mock answers. */
self.openajarFetch = async (input, init = {}) => {
  const windows = await self.clients.matchAll({ type: 'window' });
  const panel = windows[0];
  if (!panel) return Response.error();
  const channel = new MessageChannel();
  const answer = new Promise((resolve) => (channel.port1.onmessage = (event) => resolve(event.data)));
  panel.postMessage({ type: 'openajar-mock-fetch', input: String(input), init: { ...init, signal: undefined } }, [channel.port2]);
  const { status, body } = await answer;
  return new Response(status === 204 ? null : body, { status, headers: { 'Content-Type': 'application/json' } });
};

importScripts('/sw.js');
importScripts('/mockServiceWorker.js');

self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'openajar-mock-push') event.waitUntil(self.openajarShowPush(event.data.payload));
});
