import { Cog, Globe, Smartphone, SquareTerminal } from 'lucide-react';
import type { LaneKind } from '@/api/types';
import { cn } from '@/lib/cn';
import { BusinessMark } from './brand/identity';
import type { MarkSize } from './brand/site-mark';

const ICONS = { browser: Globe, terminal: SquareTerminal, service: Cog, android: Smartphone } as const;
const NAMES: Record<LaneKind, string> = {
  browser: 'Browser lane',
  terminal: 'Terminal lane',
  service: 'Service lane',
  android: 'Phone lane',
};

export function LaneIcon({ kind, className }: { kind: LaneKind; className?: string }) {
  const Icon = ICONS[kind];
  return <Icon aria-label={NAMES[kind]} className={cn('size-4 text-faint', className)} />;
}

/**
 * The workspace mark: the business's colour and initial at full strength (a fill, never a
 * surface). Kept as a name for existing screens; it is `BusinessMark`.
 */
export function WorkspaceMark({ name, color, size = 20, className }: { name: string; color: string; size?: MarkSize; className?: string }) {
  return <BusinessMark name={name} color={color} size={size} className={className} />;
}
