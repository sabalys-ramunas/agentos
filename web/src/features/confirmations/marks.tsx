/** One small drawing per rung, so a strength is recognised before it is read. */
import { AppWindow, Fingerprint, Pointer, Zap } from 'lucide-react';
import type { Rung } from '@/lib/confirm';

const ICONS: Record<Rung, typeof Zap> = { auto: Zap, chat: Pointer, signed_in: AppWindow, present: Fingerprint };

export function RungIcon({ rung, className }: { rung: Rung; className?: string }) {
  const Icon = ICONS[rung];
  return <Icon aria-hidden="true" className={className} />;
}
