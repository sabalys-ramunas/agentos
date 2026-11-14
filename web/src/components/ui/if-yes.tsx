import type { ReactNode } from 'react';
import { cn } from '@/lib/cn';

/**
 * "What happens if you approve", in one line after a mono "IF YES". Each request says it in words
 * fitted to its kind: "3 commits into main +33 −5", "€0.02 at most, deletes by 16:21". A cut line
 * keeps its full text as a title.
 */
export function IfYes({ children, title, size = 'line', className }: { children: ReactNode; title?: string; size?: 'line' | 'block'; className?: string }) {
  return (
    <div className={cn('flex min-w-0 items-center gap-2 text-secondary text-muted', size === 'block' && 'items-start', className)}>
      <span className="shrink-0 text-eyebrow text-faint">{size === 'block' ? 'If you say yes' : 'If yes'}</span>
      <span title={title} className={cn('flex min-w-0 items-center gap-2', size === 'line' ? 'truncate whitespace-nowrap' : 'flex-wrap')}>
        {children}
      </span>
    </div>
  );
}
