import { ApiError } from '@/api/http';
import type { RefusedDecision } from '@/api/types';
import { describeError, isContentChanged, staysPending } from '@/lib/errors';

/** What to say when a decision does not go through. The item is back in the queue by then. */
export function decisionError(error: unknown): string {
  if (isContentChanged(error)) return 'This changed, so nothing was decided. Review it again.';
  if (error instanceof ApiError) {
    if (error.code === 'already_decided') return 'Someone already decided this.';
    if (error.code === 'expired') return 'This approval expired, so nothing happened.';
    if (error.status === 403 || error.code === 'rate_limited') return describeError(error);
  }
  return 'That did not go through. The item is back in the queue.';
}

/** Why one item in a batch did not go through. */
export function refusalReason({ code, message }: Pick<RefusedDecision, 'code' | 'message'>): string {
  if (code === 'already_decided') return 'Someone already decided this.';
  if (code === 'expired') return 'This approval expired, so nothing happened.';
  if (code === 'content_changed' || code === 'conflict') return 'This changed, so nothing was decided. Review it again.';
  return message;
}

/** A batch refusal for changed content sends the item back to be read again. */
export const needsReading = ({ code }: Pick<RefusedDecision, 'code'>) => staysPending(code);
