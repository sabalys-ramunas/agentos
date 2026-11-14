/**
 * Small shared pieces of the Needs you queue (Mono): the row's look, the decision bar's look and the
 * short age on a row. Screens never write primitive classes, so these are utility strings.
 */
import { cn } from '@/lib/cn';
import { waitedFor } from '@/features/home/sentence';

/** A row of the queue: the open one is the pressed fill with a 2 px ink edge on its left. */
export const queueRow = (current: boolean) =>
  cn(
    'flex w-full min-w-0 items-start gap-3 border-l-2 py-3 pr-4 pl-4 text-left transition-colors duration-(--duration-fast) ease-standard',
    current ? 'border-fg bg-pressed' : 'border-transparent hover:bg-hover',
  );

/** The foot of the open request: who asked on the left, the two answers on the right. */
export const decideBar = 'flex flex-wrap items-center gap-3 border-t border-line px-5 py-3';

/** "6 min", "2 h", "3 days": a row's age. */
export const waitedShort = (iso: string, now = Date.now()) => waitedFor(iso, now);
