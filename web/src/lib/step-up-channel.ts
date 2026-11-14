/**
 * The confirmation link opens in a new tab. This tells the tab waiting in the step-up
 * dialog straight away, instead of leaving it to find out on its next check.
 */
const CHANNEL = 'agentos.step-up';

export function announceStepUp() {
  if (typeof BroadcastChannel === 'undefined') return;
  const channel = new BroadcastChannel(CHANNEL);
  channel.postMessage('confirmed');
  channel.close();
}

export function onStepUpAnnounced(listener: () => void): () => void {
  if (typeof BroadcastChannel === 'undefined') return () => undefined;
  const channel = new BroadcastChannel(CHANNEL);
  channel.onmessage = () => listener();
  return () => channel.close();
}
