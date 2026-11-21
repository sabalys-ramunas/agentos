/**
 * The wrapper tile of a kind of ask (web/DESIGN.md, AppTile): what costs money is amber, what goes out to people
 * pink, a push violet, mail blue, the machine's own work ink. The same hues as Home's cards, so a person who saw a
 * card on Home finds the same tile in the queue.
 */
import { GitBranch, KeyRound, MessageCircleQuestion } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { KINDS } from '@/components/ui/kind';
import type { AnyKind } from '@/components/ui/kind';
import { AppTile } from '@/components/ui/tile';
import type { TileTone } from '@/components/ui/tile';

export type AskKind = AnyKind | 'question' | 'repo' | 'code';

const TONE: Record<AskKind, TileTone> = {
  push: 'violet',
  post: 'pink',
  brand: 'pink',
  payment: 'amber',
  email: 'blue',
  vm: 'ink',
  lane: 'ink',
  machine: 'ink',
  command: 'ink',
  account: 'blue',
  routine: 'teal',
  test: 'violet',
  design: 'pink',
  build: 'teal',
  server: 'blue',
  question: 'blue',
  repo: 'violet',
  code: 'green',
};

const OWN_ICON: Partial<Record<AskKind, LucideIcon>> = { question: MessageCircleQuestion, repo: GitBranch, code: KeyRound };

/** An ask's tile: 28 px, the dock's squircle (the bell's rows, Home's cards). */
export function AskTile({ kind, size = 'sm' }: { kind: AskKind; size?: 'sm' | 'md' }) {
  const icon = OWN_ICON[kind] ?? KINDS[kind as AnyKind].icon;
  return <AppTile tone={TONE[kind]} icon={icon} size={size} />;
}

