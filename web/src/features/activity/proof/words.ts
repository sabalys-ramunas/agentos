/**
 * The words of Proof, in one place (web/DESIGN.md: words live in lib, never inline). A step is
 * signed, not yet, before the log began, or changed. The log check says intact only when every
 * row it checked matches its checkpoint, and says so plainly when it cannot check at all.
 */
import type { ProofChain, ProofChainStatus, ProofShareState, ProofVerify } from '@/api/types';
import type { StatusLabel } from '@/lib/labels';
import { plural } from '@/lib/format';

export const CHAIN_WORDS: Record<ProofChainStatus, StatusLabel & { sentence: string }> = {
  ok: { label: 'Signed', tone: 'ok', sentence: 'Covered by a signed checkpoint, so it has not changed since.' },
  unprovable: {
    label: 'Not signed yet',
    tone: 'idle',
    sentence: 'Written after the last checkpoint. The next one covers it.',
  },
  unchained: {
    label: 'Before the log began',
    tone: 'neutral',
    sentence: 'Written before the signed log started, so there is no proof for it.',
  },
  tampered: { label: 'Changed', tone: 'bad', sentence: 'Changed after it was written. Do not trust this step.' },
};

export const SHARE_WORDS: Record<ProofShareState, StatusLabel> = {
  active: { label: 'Active', tone: 'ok' },
  expired: { label: 'Ended', tone: 'idle' },
  revoked: { label: 'Stopped', tone: 'neutral' },
};

/** The worst a whole chain says, in one sentence a person can act on. */
export function chainSentence(chain: ProofChain): string {
  if (chain.status === 'tampered') return 'A step in this record was changed after it was written. Do not trust the steps marked Changed.';
  if (chain.status === 'unchained') return 'These steps were written before the signed log began, so there is no proof for them.';
  if (chain.status === 'unprovable') return 'Every step up to the last checkpoint is signed. The newest steps wait for the next one.';
  return 'Every step is signed, so none of them has changed since it was written.';
}

export interface Verdict {
  tone: StatusLabel['tone'];
  label: string;
  sentence: string;
}

/**
 * Whether the log is intact, in plain words. "Intact" is said only when the server says the whole
 * log checked out. Anything else says what is known and what is not.
 */
export function verdictOf(report: ProofVerify): Verdict {
  const rows = plural(report.checked, 'step');
  if (report.status === 'tampered') {
    return {
      tone: 'bad',
      label: 'The log was changed',
      sentence: 'A signed row no longer matches the checkpoint that covers it. Do not trust the steps it covers.',
    };
  }
  if (report.status === 'unverifiable') {
    return {
      tone: 'idle',
      label: 'The log cannot be checked yet',
      sentence: 'This computer cannot check the signatures, so it does not say the log is intact.',
    };
  }
  if (report.chain === 'unprovable') {
    return {
      tone: 'ok',
      label: 'The log is intact',
      sentence: `Every signed step matches its checkpoint. The newest steps wait for the next one. ${rows} checked.`,
    };
  }
  return { tone: 'ok', label: 'The log is intact', sentence: `Every step matches its signed checkpoint. ${rows} checked.` };
}

/** A day as a title, with the weekday, in the timezone the record uses (UTC here, as the day is a date). */
export function dayTitle(day: string): string {
  const [year, month, date] = day.split('-').map(Number);
  return new Intl.DateTimeFormat('en-GB', { weekday: 'long', day: 'numeric', month: 'long', timeZone: 'UTC' }).format(
    new Date(Date.UTC(year!, month! - 1, date!)),
  );
}

/** The day before or after, as `YYYY-MM-DD`. Pure date arithmetic, so no timezone can shift it. */
export function shiftDay(day: string, by: number): string {
  const [year, month, date] = day.split('-').map(Number);
  return new Date(Date.UTC(year!, month! - 1, date! + by)).toISOString().slice(0, 10);
}
