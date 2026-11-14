/**
 * The words of one business and its login queue (web/DESIGN.md: words live in one place). Only what
 * is not ready yet is queued, and a login is the one thing a person does by hand for an account.
 */
import type { LoginQueue, Readiness } from '@/api/types';
import { plural } from '@/lib/format';
import type { StatusLabel } from '@/lib/labels';

export const READINESS_WORDS: Record<Exclude<Readiness, 'ready'>, StatusLabel> = {
  needs_login: { label: 'Needs a login', tone: 'warn' },
  needs_attention: { label: 'Needs a look', tone: 'bad' },
};

/** The queue in one sentence: how far along the business is, and what is left. */
export function queueSentence(queue: Pick<LoginQueue, 'ready' | 'total' | 'needs_login' | 'needs_attention'>): string {
  if (queue.total === 0) return 'No accounts here yet.';
  if (queue.needs_login + queue.needs_attention === 0) return `All ${queue.total} ${plural(queue.total, 'account')} here are ready.`;
  const left = [
    queue.needs_login ? `${plural(queue.needs_login, 'login')} to go` : null,
    queue.needs_attention ? `${queue.needs_attention} to look at` : null,
  ]
    .filter(Boolean)
    .join(', ');
  return `${queue.ready} of ${queue.total} ready. ${left}.`;
}
