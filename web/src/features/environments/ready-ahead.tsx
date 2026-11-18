/**
 * Ready ahead: one quiet line on Environments that says what the node keeps ready for the next
 * open, and what it costs, with the way to the Prepared machines page. Shown only to the owner, when the node says.
 */
import { Link } from '@tanstack/react-router';
import { ChevronRight, Layers } from 'lucide-react';
import { useAccess, usePreparedMachines } from '@/api/hooks';
import { plural } from '@/lib/format';
import { euro } from './words';
import { cardLink } from '@/components/ui/card';
import { cn } from '@/lib/cn';

export function ReadyAhead({ org }: { org: string }) {
  const access = useAccess(org);
  const query = usePreparedMachines(org, access.role === 'owner');
  const data = query.data;
  if (!data || (!data.machines.length && !data.pool.target && !data.pool.warm)) return null;
  const ready = data.machines.filter((machine) => machine.state === 'ready').length;
  const building = data.machines.filter((machine) => machine.state === 'building').length;
  const failed = data.machines.filter((machine) => machine.state === 'failed').length;
  const month = (data.eur_month ?? data.machines.reduce((sum, machine) => sum + machine.cost_eur_month, 0)) + (data.pool.month_eur ?? 0);
  const parts = [`${ready} ready`, building ? `${building} building` : null, failed ? `${failed} failed` : null].filter(Boolean).join(', ');
  return (
    <Link
      to="/$org/environments/prepared"
      params={{ org }}
      className={cn(cardLink, 'group flex min-h-14 flex-wrap items-center gap-x-4 gap-y-1 px-5 py-3')}
    >
      <Layers aria-hidden="true" className="size-4 text-faint" />
      <span className="text-14 font-medium text-fg">Prepared machines</span>
      <span className="text-secondary text-muted">
        {parts} · warm pool {data.pool.target ? `${data.pool.warm} of ${data.pool.target} warm` : 'off'} · {euro(month)} this month
      </span>
      <span className="ml-auto inline-flex items-center gap-1 text-secondary text-muted group-hover:text-fg">
        {data.month ? `${plural(data.month.from_prepared, 'open')} skipped setup` : 'See them'}
        <ChevronRight aria-hidden="true" className="size-4 transition-transform duration-(--duration-fast) group-hover:translate-x-0.5" />
      </span>
    </Link>
  );
}
