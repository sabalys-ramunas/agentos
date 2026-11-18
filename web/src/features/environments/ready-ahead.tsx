/**
 * Ready ahead (concept): one quiet line on Environments that says what the node keeps ready for the next
 * open, and what it costs, with the way to the Prepared machines page. Shown only when the node says.
 */
import { Link } from '@tanstack/react-router';
import { ChevronRight, Layers } from 'lucide-react';
import { usePreparedMachines } from '@/api/hooks';
import { plural } from '@/lib/format';
import { euro } from './words';

export function ReadyAhead({ org }: { org: string }) {
  const query = usePreparedMachines(org);
  const data = query.data;
  if (!data) return null;
  const ready = data.machines.filter((machine) => machine.state === 'ready').length;
  const building = data.machines.filter((machine) => machine.state === 'building').length;
  const failed = data.machines.filter((machine) => machine.state === 'failed').length;
  const month = data.machines.reduce((sum, machine) => sum + machine.cost_eur_month, 0) + (data.month?.warm_cost_eur ?? 0);
  const parts = [`${ready} ready`, building ? `${building} building` : null, failed ? `${failed} failed` : null].filter(Boolean).join(', ');
  return (
    <Link
      to="/$org/environments/prepared"
      params={{ org }}
      className="group flex min-h-14 flex-wrap items-center gap-x-4 gap-y-1 rounded-xl border border-line bg-card px-5 py-3 shadow-lift transition-colors duration-(--duration-fast) hover:border-line-strong"
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
