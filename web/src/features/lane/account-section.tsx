/** The frame every quiet section of an account's page shares: a faint heading, the answer, then what backs it. */
import type { ReactNode } from 'react';
import { cn } from '@/lib/cn';
import type { Tone } from '@/lib/labels';
import { StatusDot } from '@/components/ui/status';

export function AccountSection({ id, title, action, children, className }: { id: string; title: string; action?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section aria-labelledby={id} className={cn('grid content-start gap-3', className)}>
      <div className="flex min-h-7 items-center justify-between gap-3">
        <h2 id={id} className="text-secondary text-faint font-semibold">
          {title}
        </h2>
        {action}
      </div>
      {children}
    </section>
  );
}

/** The answer, first: a dot and its word, then one sentence that says what it means. */
export function Answer({ tone, word, children }: { tone: Tone; word: string; children?: ReactNode }) {
  return (
    <div className="grid gap-1">
      <p className="flex items-center gap-2 text-label">
        <StatusDot tone={tone} />
        {word}
      </p>
      {children ? <p className="text-secondary text-pretty text-muted">{children}</p> : null}
    </div>
  );
}
