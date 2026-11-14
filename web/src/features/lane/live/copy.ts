/** What each connection phase says, in words. One place, so the live view and the terminal agree. */
import type { StreamKind, StreamState } from './stream';

export interface StreamCopy {
  title: string;
  body: string;
  /** The one thing to do, when there is one. */
  action: 'retry' | 'sign_in' | 'needs_you' | null;
  /** Short word for the toolbar. */
  label: string;
}

const THING: Record<StreamKind, string> = { vnc: 'live view', terminal: 'terminal' };

/** Node refusals arrive as codes from the wire. These are the ones a person can act on. */
function refusal(reason: string | null, kind: StreamKind): string {
  switch (reason) {
    case 'lane_closed':
      return 'The lane was closed. Open it again to see it here.';
    case 'exit':
      return 'The shell exited.';
    case 'service_unavailable':
    case 'not_ready':
      return 'The lane is not open on the node. Open it, then reconnect.';
    case 'lane_quarantined':
      return 'The lane is quarantined. Reopen it to run its self-check.';
    case 'invalid':
      return kind === 'vnc' ? 'The node has no live view for this lane.' : 'The node has no terminal for this lane.';
    case 'unsupported':
      return `This node does not serve a ${THING[kind]} yet.`;
    case null:
    case '':
    case 'closed':
      return `The node closed the ${THING[kind]}.`;
    default:
      return `The node closed the ${THING[kind]} (${reason}).`;
  }
}

export function seconds(retryAt: number | null, now: number): number {
  return retryAt === null ? 0 : Math.max(0, Math.ceil((retryAt - now) / 1000));
}

export function streamCopy(state: StreamState, kind: StreamKind, node: string, now: number): StreamCopy {
  const thing = THING[kind];
  const left = seconds(state.retryAt, now);
  const when = state.retryAt === null || left === 0 ? 'Trying now.' : `Trying again in ${left} s.`;
  switch (state.phase) {
    case 'idle':
    case 'connecting':
      return { title: `Connecting to the ${thing}`, body: `Asking ${node} for it. This takes a second or two.`, action: null, label: 'Connecting' };
    case 'live':
      return { title: 'Live', body: '', action: null, label: 'Live' };
    case 'reconnecting':
      if (state.reason === 'handed_back' || state.reason === 'not_holding') {
        return { title: 'Back to watching', body: 'Control moved away from you, so this switches to the read-only view.', action: null, label: 'Switching' };
      }
      return state.reason === 'rate limited'
        ? { title: 'Too many connections', body: `Wait a moment. ${when}`, action: 'retry', label: 'Waiting' }
        : { title: 'Reconnecting', body: `The connection dropped. ${when}`, action: 'retry', label: 'Reconnecting' };
    case 'away':
      return {
        title: `${node} is away`,
        body: `The computer is not connected, so there is nothing to show. It is safe to wait here. ${when}`,
        action: 'retry',
        label: 'Computer away',
      };
    case 'waiting':
      return {
        title: 'Waiting for approval',
        body: state.reason ? `${state.reason}. Approve it in Needs you, then reconnect.` : 'Opening a shell here needs an approval first. Approve it in Needs you, then reconnect.',
        action: 'needs_you',
        label: 'Waiting',
      };
    case 'signed_out':
      return { title: 'You are signed out', body: `Sign in again to open the ${thing}.`, action: 'sign_in', label: 'Signed out' };
    case 'denied':
      if (state.reason === 'not_holding') {
        return {
          title: 'Take over to type',
          body: `You do not hold this lane right now, so the ${thing} only opens read only. Take over first, or try again to watch.`,
          action: 'retry',
          label: 'Read only',
        };
      }
      return {
        title: 'Not allowed',
        body:
          state.reason === 'revoked'
            ? 'Your access to this workspace changed while it was open.'
            : `Your role cannot open the ${thing} of this lane. An owner or admin can change that.`,
        action: null,
        label: 'Not allowed',
      };
    case 'missing':
      return { title: 'This lane is not there', body: 'It may have been removed from the node. Go back to the workspace.', action: null, label: 'Not found' };
    case 'ended':
      return { title: `The ${thing} ended`, body: refusal(state.reason, kind), action: 'retry', label: 'Ended' };
    case 'failed':
      return {
        title: 'Could not connect',
        body: `The ${thing} did not open after several tries. Check that ${node} is up, then try again.`,
        action: 'retry',
        label: 'Not connected',
      };
  }
}
