/** Turns any failure into one calm sentence. Screens and toasts both read from here. */
import { ApiError } from '@/api/http';
import { plainReason } from './plain-node';

function wait(seconds: number): string {
  if (seconds < 60) {
    const count = Math.max(seconds, 1);
    return `${count} ${count === 1 ? 'second' : 'seconds'}`;
  }
  const minutes = Math.ceil(seconds / 60);
  if (minutes < 90) return `${minutes} ${minutes === 1 ? 'minute' : 'minutes'}`;
  const hours = Math.ceil(minutes / 60);
  return `${hours} hours`;
}

export function describeError(error: unknown): string {
  if (!(error instanceof ApiError)) return 'That did not go through. Try again.';
  switch (error.code) {
    case 'network':
      return error.message;
    case 'rate_limited':
      // The daily caps explain themselves. The generic limiter only says to wait.
      if (error.body.limit !== undefined) return error.message;
      return error.retryAfter
        ? `Too many tries. Try again in ${wait(error.retryAfter)}.`
        : 'Too many tries. Wait a moment and try again.';
    default:
      return error.status >= 500 ? 'The service did not answer. Nothing was changed.' : plainReason(error.message);
  }
}

/**
 * A 409 on a decision that is not one of the known answers means the node changed what it
 * wants to do. The hash no longer matches, so the person has to read it again.
 */
export const isContentChanged = (error: unknown) =>
  error instanceof ApiError &&
  error.status === 409 &&
  error.code !== 'already_decided' &&
  error.code !== 'expired' &&
  error.code !== 'read_only';

/** Whether an item refused in a batch is still waiting. Decided and expired ones have left. */
export const staysPending = (code: string) => code !== 'already_decided' && code !== 'expired';

export const isPlanLimit = (error: unknown) => error instanceof ApiError && error.code === 'plan_limit';
