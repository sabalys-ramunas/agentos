/**
 * The kind of work (web/DESIGN.md, Kinds). Four kinds carry a hue: tests (violet), design checks
 * (pink), build and CI (teal) and dev servers (blue). Machines, pushes, posts, payments and the rest
 * are neutral: the same tile in grey. A kind is always a rounded tile with its own icon and its word
 * in mono caps, never a dot, so it never reads as a status.
 */
import {
  Box,
  Cloud,
  CreditCard,
  FlaskConical,
  GitBranch,
  Globe,
  Mail,
  MessageSquare,
  Palette,
  Radio,
  Scan,
  Server,
  SquareTerminal,
  Workflow,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { cn } from '@/lib/cn';

export type Kind = 'test' | 'design' | 'build' | 'server';
export type NeutralKind =
  | 'machine' | 'push' | 'post' | 'payment' | 'vm' | 'lane' | 'brand' | 'email' | 'command' | 'account' | 'routine';
export type AnyKind = Kind | NeutralKind;

export const KINDS: Record<AnyKind, { word: string; icon: LucideIcon; hue: boolean }> = {
  test: { word: 'Tests', icon: FlaskConical, hue: true },
  design: { word: 'Design check', icon: Scan, hue: true },
  build: { word: 'Build', icon: Box, hue: true },
  server: { word: 'Dev server', icon: Radio, hue: true },
  machine: { word: 'Machine', icon: Server, hue: false },
  push: { word: 'Push', icon: GitBranch, hue: false },
  post: { word: 'Post', icon: MessageSquare, hue: false },
  payment: { word: 'Payment', icon: CreditCard, hue: false },
  vm: { word: 'Disposable VM', icon: Cloud, hue: false },
  lane: { word: 'New lane', icon: SquareTerminal, hue: false },
  brand: { word: 'Brand', icon: Palette, hue: false },
  email: { word: 'Email', icon: Mail, hue: false },
  /** A run whose command says no kind of work: a script, a one-off. */
  command: { word: 'Command', icon: SquareTerminal, hue: false },
  /** An account an agent is acting in (a browser lane). */
  account: { word: 'Account', icon: Globe, hue: false },
  /** A routine's run. */
  routine: { word: 'Routine', icon: Workflow, hue: false },
};

const tile = {
  xs: 'size-4.5 rounded-sm [&_svg]:size-3',
  sm: 'size-6 rounded-sm [&_svg]:size-3.5',
  md: 'size-7 rounded-md [&_svg]:size-4',
  lg: 'size-8 rounded-md [&_svg]:size-4',
} as const;

export interface KindTileProps {
  kind: AnyKind;
  size?: keyof typeof tile;
  /** Waiting for a slot: a dashed edge and no fill. */
  waiting?: boolean;
  /** Read the kind's word to screen readers; off when the word is already beside the tile. */
  labelled?: boolean;
  className?: string;
}

/** The kind's tile: its soft tint, its line, and its own icon in its mark colour. */
export function KindTile({ kind, size = 'md', waiting = false, labelled = false, className }: KindTileProps) {
  const { icon: Icon, word, hue } = KINDS[kind];
  return (
    <span
      data-kind={hue ? kind : undefined}
      data-waiting={waiting ? '' : undefined}
      role={labelled ? 'img' : undefined}
      aria-label={labelled ? word : undefined}
      aria-hidden={labelled ? undefined : true}
      className={cn('ui-kind-tile', tile[size], className)}
    >
      <Icon />
    </span>
  );
}

/** The kind's eyebrow: the tile and its word in mono caps, in the kind's text colour. */
export function KindEyebrow({ kind, label, className }: { kind: AnyKind; label?: string; className?: string }) {
  const { word, hue } = KINDS[kind];
  return (
    <span data-kind={hue ? kind : undefined} className={cn('inline-flex min-w-0 items-center gap-2', className)}>
      <KindTile kind={kind} size="xs" />
      <span title={label ?? word} className="ui-kind-word truncate text-eyebrow">
        {label ?? word}
      </span>
    </span>
  );
}
