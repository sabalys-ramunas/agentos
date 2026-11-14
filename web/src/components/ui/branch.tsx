import { GitBranch } from 'lucide-react';
import { cn } from '@/lib/cn';

/**
 * A branch as a pill: mono, a fill and a hairline, its full name on hover when it is cut. The main
 * branch (`main`) is ink, so "fix/... → main" reads at a glance.
 */
export function BranchPill({ name, main = false, size = 'sm', className }: { name: string; main?: boolean; size?: 'sm' | 'lg'; className?: string }) {
  return (
    <span
      className={cn(
        'inline-flex max-w-65 min-w-0 items-center gap-1 rounded-full border align-middle font-mono',
        size === 'lg' ? 'py-1 pr-3 pl-2 text-code' : 'py-px pr-2 pl-1 text-code-caption',
        main ? 'border-fg bg-fg text-on-primary' : 'border-line bg-hover text-muted',
        className,
      )}
    >
      <GitBranch aria-hidden="true" className={size === 'lg' ? 'size-3.5' : 'size-3'} />
      <span title={name} className="truncate">
        {name}
      </span>
    </span>
  );
}
