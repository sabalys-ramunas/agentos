/**
 * What waits for a person, as Home reads it, and the order it shows it in (heaviest first). Home draws it on
 * its window's edge (home-hero.tsx); deciding happens on Needs you.
 */
import type { Approval, BrandProposal, Question } from '@/api/types';
import { STAKE_ORDER, stakeOf } from '@/features/needs-you/kinds';

export type Waiting =
  | { type: 'approval'; id: string; at: string; approval: Approval }
  | { type: 'brand'; id: string; at: string; proposal: BrandProposal }
  | { type: 'question'; id: string; at: string; question: Question };

/** Heaviest first, as Needs you orders them: money, the irreversible, pushes ... then brand changes, then questions. */
export function waitingOrder(items: Waiting[]): Waiting[] {
  const weight = (item: Waiting) =>
    item.type === 'approval' ? STAKE_ORDER.indexOf(stakeOf(item.approval).key) : item.type === 'brand' ? STAKE_ORDER.length : STAKE_ORDER.length + 1;
  return [...items].sort((a, b) => weight(a) - weight(b) || a.at.localeCompare(b.at));
}
