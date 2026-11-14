import { Link } from '@tanstack/react-router';
import type { Enforcement } from '@/api/types';
import { StatusDot } from '@/components/ui/status';
import { cn } from '@/lib/cn';
import { routineName } from '@/lib/principal';
import type { LimitGap } from './describe';

/**
 * The cloud's own sentence about whether a standing approval does anything, shown as written.
 * Today no node applies one, so this is never allowed to read as protection.
 */
export function EnforcementNote({ enforcement, className }: { enforcement: Enforcement; className?: string }) {
  const applied = enforcement.state === 'applied';
  return (
    <p className={cn('flex items-baseline gap-2 text-13', className)}>
      <StatusDot tone={applied ? 'ok' : 'warn'} className="translate-y-px" />
      <span className="min-w-0">
        <span className={applied ? 'text-muted' : 'text-fg'}>{enforcement.message}</span>
        {enforcement.state === 'partial' && enforcement.not_applied_by.length ? (
          <span className="text-faint"> Not applied on {enforcement.not_applied_by.join(', ')}.</span>
        ) : null}
      </span>
    </p>
  );
}

/**
 * A person who set this as a limit must not read the count as a cap: said beside the approval,
 * wherever what it covers runs on its own, with the way to keep the limit one tap away.
 */
export function LimitNote({ org, gap, nameOf }: { org: string; gap: LimitGap; nameOf: (lane: string) => string }) {
  const [routine] = gap.routines;
  const [lane] = gap.lanes;
  const several = gap.routines.length + gap.lanes.length > 1;
  const names = [...gap.routines.map(routineName), ...gap.lanes.map(nameOf)];
  const shown = names.length > 3 ? `${names.slice(0, 3).join(', ')} and ${names.length - 3} more` : names.join(' and ').replace(/ and (?=.* and )/g, ', ');
  const [ws, name] = (lane ?? '').split('/');
  const keep = 'Turn on Ask me first';
  return (
    <p className="flex items-baseline gap-2 text-13">
      <StatusDot tone="warn" className="translate-y-px" />
      <span className="min-w-0 text-fg">
        This limit only counts while {gap.routines.length ? 'a routine' : 'an account'} asks first. {shown} {several ? 'run' : 'runs'} on {several ? 'their' : 'its'} own, so what goes out there
        does not use this limit and is not capped by it.{' '}
        {several ? (
          keep
        ) : routine ? (
          <Link to="/$org/flows/$flow" params={{ org, flow: routine }} className="link">
            {keep}
          </Link>
        ) : (
          <Link to="/$org/lanes/$ws/$name" params={{ org, ws: ws ?? '', name: name ?? '' }} className="link">
            {keep}
          </Link>
        )}{' '}
        to keep the limit.
      </span>
    </p>
  );
}
