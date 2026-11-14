/**
 * The words of the memory page, in one place: what each source means, what waits, and what a record says.
 * Memory text is never in here. It comes from the node and is shown as plain text, nowhere else.
 */
import type { MemoryState, MemoryTrust, MemoryWorkspace } from '@/api/types';

/** The trust filter is a floor: "at least this trusted". Leaving out outside text is the useful cut. */
export const TRUST_FILTERS: { value: string; label: string }[] = [
  { value: 'all', label: 'Everything' },
  { value: 'agent', label: 'Leave out outside text' },
  { value: 'node', label: 'Written by agentos or confirmed by you' },
  { value: 'owner', label: 'Confirmed by you only' },
];

export const TYPE_FILTERS: { value: string; label: string }[] = [
  { value: 'all', label: 'Every kind' },
  { value: 'fact', label: 'Facts' },
  { value: 'decision', label: 'Decisions' },
  { value: 'preference', label: 'Preferences' },
  { value: 'contact', label: 'Contacts' },
  { value: 'document', label: 'Documents' },
  { value: 'run_summary', label: 'Run summaries' },
];

/** What a person reads about one entry's source, in their own words. */
export const SOURCE_SENTENCE: Record<MemoryTrust, string> = {
  owner: 'You confirmed this, or wrote it.',
  node: 'agentos wrote this itself, from what it did.',
  agent: 'An agent wrote this from what it did. Nobody has checked it yet.',
  untrusted: 'An agent wrote this after reading outside text, such as a web page. Treat it as a claim to check, not a fact.',
};

/**
 * Whether an entry is still waiting for a look: an agent's or outside text nobody has reviewed or confirmed.
 * A search hit does not say whether it was reviewed (`reviewed` is null), so it is not claimed to wait.
 */
export function waitsForLook(entry: { trust: MemoryTrust; state: MemoryState; reviewed?: boolean | null }): boolean {
  return entry.state === 'live' && (entry.trust === 'agent' || entry.trust === 'untrusted') && entry.reviewed === false;
}

export function waitingSentence(count: number): string {
  if (count === 0) return 'Nothing waits for a look.';
  return count === 1 ? '1 thing waits for a look.' : `${count} things wait for a look.`;
}

/** Bytes as a person counts them. The caps are shown the same way, so the two numbers read alike. */
export function bytesText(bytes: number): string {
  if (bytes < 1024) return `${bytes} bytes`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function keptSentence(row: MemoryWorkspace): string {
  return `${row.live_entries} of ${row.live_entries_cap} entries kept`;
}

export function recordSentence(row: MemoryWorkspace): string {
  return row.chain_ok ? 'Record checks out' : 'Changed outside agentos';
}

/** The record is the warning the screen must never soften: it says what happened, and what to do. */
export const RECORD_WARNING =
  'The record of this workspace was changed outside agentos, so its history cannot be trusted as it was written. Read the entries with care, and ask whoever has the computer what changed.';

export const FORGET_HINT =
  'Forgetting erases the title and text of this entry in every version, for good. The reason stays on record, and the entry stays listed as forgotten.';
