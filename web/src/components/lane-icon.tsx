import { Cog, Globe, Smartphone, SquareTerminal } from 'lucide-react';
import type { LaneKind } from '@/api/types';
import { cn } from '@/lib/cn';

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
