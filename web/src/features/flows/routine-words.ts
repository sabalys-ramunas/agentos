/** What a person reads about a routine, in one place, so the list and its page agree. */
import type { Flow, RoutineCatalog, RoutineStep, RunStatus } from '@/api/types';
import { routineName } from '@/lib/principal';
import { dataBefore, refName, sentenceOf, sentenceText } from '@/lib/routines';
import type { SentencePart } from '@/lib/routines';
import { siteName } from '@/lib/sites';
import { clock } from '@/lib/format';

export const routineTitle = (flow: Flow) => flow.name ?? routineName(flow.id);

/** After a save over a routine that is on: the new version is kept, and the old one keeps running until someone allowed to turn it on does. */
export const NEEDS_REVIEW_WORDS = 'The old version keeps running until someone who may turn the new one on does.';

/** Its site, for a step that runs on one. */
export function stepSite(step: RoutineStep, catalog: RoutineCatalog | undefined): string | null {
  if (step.kind !== 'action' || !step.operation) return null;
  return catalog?.operations.find((item) => item.id === step.operation)?.lane.split('/')[1] ?? null;
}

/** The sentence as parts. A computer that keeps only a recording has no steps to tell. */
export function routineParts(flow: Flow, catalog?: RoutineCatalog): SentencePart[] | null {
  if (!flow.trigger || !flow.steps) return null;
  const definition = { trigger: flow.trigger, steps: flow.steps };
  return sentenceOf(definition, (ref) => refName(dataBefore(definition, null, catalog), ref)?.toLowerCase() ?? null, catalog);
}

export function routineSentence(flow: Flow, catalog?: RoutineCatalog): string {
  const parts = routineParts(flow, catalog);
  if (parts) return sentenceText(parts);
  const site = flow.lane.split('/')[1] ?? flow.lane;
  // A recording made for every account (`*`) names no site.
  if (site === '*' || flow.lane.startsWith('*')) return 'A recording for any account. It repeats what it was shown.';
  return `A recording on ${siteName(site)}. It repeats what it was shown.`;
}

/** How a run went, in the words a person uses. "Verified" is what the proof says, so it stays. */
export const OUTCOME: Partial<Record<RunStatus, { word: string; color: string }>> = {
  succeeded_verified: { word: 'Worked, and checked', color: 'var(--mark-verified)' },
  succeeded_unverified: { word: 'Worked, not checked', color: 'var(--mark-unverified)' },
  partial: { word: 'Partly done', color: 'var(--mark-partial)' },
  failed: { word: 'Did not work', color: 'var(--mark-failed)' },
};

/** "tomorrow 09:00", "Monday 08:30", "9 Nov 07:00": said so it fits in the middle of a line. */
export function whenNext(iso: string, now = Date.now()): string {
  const at = new Date(iso);
  const time = clock(at);
  const days = Math.round((new Date(at).setHours(0, 0, 0, 0) - new Date(now).setHours(0, 0, 0, 0)) / 86_400_000);
  if (days === 0) return `today ${time}`;
  if (days === 1) return `tomorrow ${time}`;
  if (days < 7) return `${at.toLocaleDateString('en', { weekday: 'long' })} ${time}`;
  return `${at.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })} ${time}`;
}
