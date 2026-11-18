/** The catalog's own frame: a titled example, and a row that names what a sample is. */
import type { ReactNode } from 'react';
import { cn } from '@/lib/cn';

/** One example in an entry: a quiet caption and the thing itself. */
export function Example({ label, children, className }: { label: string; children: ReactNode; className?: string }) {
  return (
    <div className={cn('grid min-w-0 content-start gap-2', className)}>
      <span className="text-caption text-faint">{label}</span>
      {children}
    </div>
  );
}

/** A row of samples that wraps on a phone. */
export function Row({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn('flex min-w-0 flex-wrap items-center gap-2', className)}>{children}</div>;
}
