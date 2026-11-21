/**
 * Working now on Home (Dawn's Full): each open environment as a card with a picture of its screen, who works in
 * it and what it is doing, and the time it has left. It reads the same list as the Environments page and the
 * rail, so the three agree. Nothing at all when the node has no environments to say (a hosted app, an old
 * node); a calm line when none is open.
 */
import { Link } from '@tanstack/react-router';
import { ChevronRight } from 'lucide-react';
import { isNotServed } from '@/api/http';
import { useEnvironments } from '@/api/hooks';
import { Card } from '@/components/ui/card';
import { ErrorState } from '@/components/ui/states';
import { plural } from '@/lib/format';
import { useNow } from '@/lib/use-now';
import dawnSmall from '@/assets/dawn/dawn-720.webp';
import { EnvCard, EnvCardsSkeleton } from '../environments/env-card';
import { euro, splitEnvironments } from '../environments/words';

/** Cards before "All environments": Home stays a glance. */
const HOME_CARDS = 6;

export function WorkingNow({ org }: { org: string }) {
  const query = useEnvironments(org, 'all', true, { pictures: true });
  const open = query.data ? splitEnvironments(query.data.environments).open : [];
  // Minutes are what the chips say, so the cards are drawn again twice a minute, not every second.
  const now = useNow(open.length > 0, 30_000);
  // A node without environments, or a hosted app that does not serve them, has nothing to say here.
  if (query.data && query.data.available === false) return null;
  if (query.isError && isNotServed(query.error)) return null;
  const cost = open.reduce((sum, env) => sum + env.cost_eur, 0);
  const shown = open.slice(0, HOME_CARDS);
  return (
    <section aria-labelledby="home-working" className="mt-10">
      <div className="mb-4 flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 id="home-working" className="text-section-title">
          Working now
        </h2>
        {open.length ? (
          <span className="text-secondary text-muted">
            {plural(open.length, 'environment')} · {euro(cost)} so far
          </span>
        ) : null}
        <Link to="/$org/environments" params={{ org }} className="ml-auto inline-flex min-h-8 items-center gap-1 text-secondary text-muted hover:text-fg">
          {open.length > HOME_CARDS ? `All ${open.length} environments` : 'All environments'}
          <ChevronRight aria-hidden="true" className="size-4" />
        </Link>
      </div>
      {query.isError ? (
        <Card>
          <ErrorState error={query.error} onRetry={() => void query.refetch()} />
        </Card>
      ) : !query.data ? (
        <EnvCardsSkeleton />
      ) : shown.length ? (
        <ul aria-label="Open environments" className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {shown.map((env) => (
            <EnvCard key={env.id} org={org} env={env} now={now} />
          ))}
        </ul>
      ) : (
        <Card className="flex items-center gap-4 p-3 pr-5">
          <img src={dawnSmall} alt="" aria-hidden="true" className="aspect-video w-28 shrink-0 rounded-md object-cover sm:w-36" />
          <div className="min-w-0">
            <p className="text-label font-semibold text-fg">No agent is working right now</p>
            <p className="mt-1 text-secondary text-muted">When an agent opens an environment, its screen shows here while it works.</p>
          </div>
        </Card>
      )}
    </section>
  );
}
