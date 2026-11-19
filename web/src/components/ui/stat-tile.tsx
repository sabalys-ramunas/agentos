import type { ReactNode } from 'react';
import { cn } from '@/lib/cn';

interface StatTileProps {
  label: string;
  value: ReactNode;
  unit?: string;
  /** A short line under the number: "of 9 checked pages", "+3 since yesterday". */
  hint?: ReactNode;
  /** Colours the number only when it asks for attention. */
  tone?: 'warn' | 'bad';
  className?: string;
}

/** One big number with its label: the stat role in tabular figures, its unit in the tertiary ink. */
export function StatTile({ label, value, unit, hint, tone, className }: StatTileProps) {
  return (
    <div className={cn('grid min-w-0 content-start gap-1 p-5', className)}>
      <span className="text-caption text-faint">{label}</span>
      <span className={cn('text-stat', tone === 'warn' ? 'text-warn' : tone === 'bad' ? 'text-bad' : 'text-fg')}>
        {value}
        {unit ? <span className="ml-1 text-body tracking-normal text-faint">{unit}</span> : null}
      </span>
      {hint ? <span className="text-secondary text-muted">{hint}</span> : null}
    </div>
  );
}

/** A row of stat tiles in one card, divided by hairlines (a summary strip). */
export function StatStrip({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className={cn('grid grid-cols-2 divide-line overflow-hidden rounded-xl border border-line bg-card shadow-lift md:grid-cols-4 md:divide-x [&>*]:border-line max-md:[&>*:nth-child(n+3)]:border-t max-md:[&>*:nth-child(odd)]:border-r', className)}>
      {children}
    </div>
  );
}
