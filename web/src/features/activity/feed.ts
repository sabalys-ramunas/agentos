/**
 * The History feed, from the cloud's days of stories: each story becomes a view, and a check that
 * keeps failing the same way that day becomes one row that says how often.
 */
import type { HistoryDay } from '@/api/types';
import type { StoryView } from './story-view';
import { fromHistory } from './story-view';

export interface Entry {
  /** The newest of them. */
  view: StoryView;
  /** More than one when the same check failed the same way again that day. */
  times: number;
}

export function entriesOf(day: HistoryDay): Entry[] {
  const entries: Entry[] = [];
  const repeats = new Map<string, Entry>();
  for (const story of day.stories) {
    const view = fromHistory(story);
    if (view.lookOnly && view.result === 'failed') {
      const key = `${view.lane}|${view.sentence}|${view.note ?? ''}`;
      const seen = repeats.get(key);
      if (seen) {
        seen.times += 1;
        continue;
      }
      const entry = { view, times: 1 };
      repeats.set(key, entry);
      entries.push(entry);
      continue;
    }
    entries.push({ view, times: 1 });
  }
  return entries;
}

const dayKey = (at: Date) => `${at.getFullYear()}-${at.getMonth() + 1}-${at.getDate()}`;

/** "Today", "Yesterday", "Monday 2 November", in the browser's own time. */
export function dayLabel(iso: string, now = Date.now()): string {
  const at = new Date(iso);
  const today = new Date(now);
  const yesterday = new Date(now);
  yesterday.setDate(today.getDate() - 1);
  if (dayKey(at) === dayKey(today)) return 'Today';
  if (dayKey(at) === dayKey(yesterday)) return 'Yesterday';
  const sameYear = at.getFullYear() === today.getFullYear();
  return at.toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long', ...(sameYear ? {} : { year: 'numeric' }) }).replace(',', '');
}

/** Whole days between two `2026-11-04` dates. */
export function daysApart(newer: string, older: string): number {
  return Math.round((Date.parse(`${newer}T00:00:00Z`) - Date.parse(`${older}T00:00:00Z`)) / 86_400_000);
}

/** The quiet line between two days with nothing in between. */
export function quietLine(newer: string, older: string, label: (date: string) => string): string | null {
  const gap = daysApart(newer, older);
  if (gap <= 1) return null;
  if (gap === 2) {
    const missing = new Date(Date.parse(`${newer}T12:00:00Z`) - 86_400_000).toISOString().slice(0, 10);
    return `${label(missing)}: nothing happened. The agents rested.`;
  }
  return `Nothing happened for ${gap - 1} days. The agents rested.`;
}
