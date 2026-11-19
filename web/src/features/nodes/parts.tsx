/** Small pieces every node tab shares, so the tabs read as one instrument. */
import { Radio } from 'lucide-react';
import type { ReactNode } from 'react';
import { Card } from '@/components/ui/card';
import { cn } from '@/lib/cn';
import { useTechnical } from '@/lib/technical';

/** A tab's section: a quiet title row with its controls, and the content under it. */
export function Panel({ title, description, action, children, className }: { title: string; description?: ReactNode; action?: ReactNode; children: ReactNode; className?: string }) {
  return (
    <section aria-label={title} className={cn('grid gap-3', className)}>
      <div className="flex min-h-8 flex-wrap items-center justify-between gap-x-4 gap-y-2">
        <div className="min-w-0">
          <h2 className="text-body font-semibold">{title}</h2>
          {description ? <p className="text-secondary text-faint">{description}</p> : null}
        </div>
        {action ? <div className="flex flex-wrap items-center gap-2">{action}</div> : null}
      </div>
      {children}
    </section>
  );
}

/**
 * The node is reachable but its build does not answer this report. That is a fact about the
 * node, never an empty list, so it says so plainly and shows no made-up numbers.
 */
export function NotReported({ node, what, className }: { node: string; what: string; className?: string }) {
  return (
    <Card className={cn('flex items-start gap-3 p-4 sm:p-6', className)}>
      <span className="flex h-5 shrink-0 items-center">
        <Radio className="size-4 text-faint" />
      </span>
      <div className="min-w-0">
        <p className="text-label">
          <span className="mono">{node}</span> does not report {what} yet
        </p>
        <p className="mt-1 max-w-prose text-secondary text-muted">
          It runs an older version that does not send this yet. Nothing is wrong with the machine, and this fills in by itself once it is updated.
        </p>
      </div>
    </Card>
  );
}

/** Label over value, in a grid of hairline cells. Cells keep their size whatever the state. */
export function Facts({ items, className }: { items: { label: string; value: ReactNode; mono?: boolean }[]; className?: string }) {
  return (
    <dl className={cn('grid grid-cols-2 gap-px overflow-hidden rounded-xl border border-line bg-line sm:grid-cols-3 lg:grid-cols-4', className)}>
      {items.map((item) => (
        <div key={item.label} className="min-w-0 bg-card px-4 py-3">
          <dt className="text-caption text-faint">{item.label}</dt>
          <dd className={cn('mt-1 truncate text-secondary text-fg', item.mono && 'mono')}>{item.value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** A dash for "no value", in the table cell's own colour. */
export const NONE = <span className="text-faint">–</span>;

/** A command typed on the machine. Its words say what to do; the command itself shows under Technical details. */
export function TypedCommand({ command, className }: { command: string; className?: string }) {
  const [technical] = useTechnical();
  return technical ? <span className={cn('mono mt-1 block break-all text-caption text-faint', className)}>{command}</span> : null;
}
