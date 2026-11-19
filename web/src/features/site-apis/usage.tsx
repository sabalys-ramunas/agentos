/**
 * A week of calls and what it cost the account. The chart answers how calls were served, hour by
 * hour, per endpoint. The gauge beside it answers the only question a person has about pace: how much
 * of this account's pace is in use. It is one tick per read, so it can be counted, and it stays grey
 * and calm until it matters. The account is one account; nothing here offers another way round.
 */
import { Link } from '@tanstack/react-router';
import { ArrowUpRight } from 'lucide-react';
import { useMemo, useState } from 'react';
import { useSiteUsage } from '@/api/hooks';
import type { SiteApiDetail, SiteUsageHour } from '@/api/types';
import { SiteMark } from '@/components/brand/site-mark';
import { StackedBars } from '@/components/charts/stacked-bars';
import { Select } from '@/components/ui/select';
import { Skeleton } from '@/components/ui/states';
import { cn } from '@/lib/cn';
import { compact, plural } from '@/lib/format';
import { markOf, speed } from './words';

const SERIES = [
  { key: 'cached', label: 'From a saved answer', color: 'var(--mark-cached)' },
  { key: 'fresh', label: 'Read fresh from the site', color: 'var(--mark-fresh)' },
  { key: 'errors', label: 'Could not answer', color: 'var(--mark-failed)' },
];

function totalsOf(hours: SiteUsageHour[], endpoint: string) {
  const total = { cached: 0, fresh: 0, errors: 0 };
  for (const hour of hours) {
    for (const [key, call] of Object.entries(hour.calls)) {
      if (endpoint !== 'all' && key !== endpoint) continue;
      total.cached += call.cached;
      total.fresh += call.fresh;
      total.errors += call.errors;
    }
  }
  return total;
}

function Stat({ label, value, note }: { label: string; value: string; note: string }) {
  return (
    <div className="bg-card p-4">
      <p className="text-secondary text-muted">{label}</p>
      <p className="mt-1 text-heading">{value}</p>
      <p className="mt-1 text-caption text-faint">{note}</p>
    </div>
  );
}

export function Requests({ org, api }: { org: string; api: SiteApiDetail }) {
  const usage = useSiteUsage(org, api.key);
  const [endpoint, setEndpoint] = useState('all');
  const hours = useMemo(() => usage.data ?? [], [usage.data]);
  const buckets = useMemo(
    () =>
      hours.map((hour) => {
        const total = totalsOf([hour], endpoint);
        return { at: hour.at, values: total };
      }),
    [endpoint, hours],
  );
  const total = totalsOf(hours, endpoint);
  const calls = total.cached + total.fresh + total.errors;
  const selected = api.endpoints.find((item) => item.key === endpoint);
  const median = selected?.p50_ms ?? api.endpoints[0]?.p50_ms ?? 0;
  return (
    <section aria-labelledby="site-requests" className="min-w-0">
      <div className="mb-4 flex items-center justify-between gap-4">
        <h2 id="site-requests" className="text-section-title">
          Requests, last 7 days
        </h2>
        {api.endpoints.length > 1 ? (
          <Select
            aria-label="Which endpoint"
            size="sm"
            className="w-48"
            value={endpoint}
            onValueChange={setEndpoint}
            options={[{ value: 'all', label: 'All endpoints' }, ...api.endpoints.map((item) => ({ value: item.key, label: item.title }))]}
          />
        ) : null}
      </div>
      <div className="overflow-hidden rounded-(--radius-card) border border-line">
        <div className="grid grid-cols-2 gap-px bg-line md:grid-cols-4">
          <Stat label="Calls" value={compact(calls)} note={`${plural(api.endpoints.length, 'endpoint')}`} />
          <Stat label="From saved answers" value={calls ? `${Math.round((total.cached / calls) * 100)}%` : '–'} note="Never touched the site" />
          <Stat label="Fresh read takes" value={speed(median)} note="Half are quicker" />
          <Stat label="Could not answer" value={compact(total.errors)} note={total.errors ? 'Marked red below' : 'None this week'} />
        </div>
        <div className="border-t border-line p-4">
          {usage.data ? (
            <StackedBars label="Calls per hour, by how they were answered" series={SERIES} buckets={buckets} bucketSeconds={21_600} height={168} format={compact} />
          ) : (
            <Skeleton className="h-48 rounded-md" />
          )}
        </div>
      </div>
    </section>
  );
}

/** One tick per read this hour. This API first, then what else used the account. */
function Ticks({ pace }: { pace: SiteApiDetail['pace'] }) {
  const others = pace.others.reduce((sum, item) => sum + item.used, 0);
  const used = pace.used + others;
  const share = used / pace.per_hour;
  const ink = share >= 0.95 ? 'bg-bad' : share >= 0.8 ? 'bg-warn' : 'bg-fg';
  return (
    <div role="meter" aria-label="How much of this account's pace is in use this hour" aria-valuenow={used} aria-valuemin={0} aria-valuemax={pace.per_hour} className="flex h-8 items-end gap-px">
      {Array.from({ length: pace.per_hour }, (_, index) => (
        <span key={index} className={cn('pace-tick min-w-0 flex-1 rounded-[1px]', index < pace.used ? `h-8 ${ink}` : index < used ? 'h-6 bg-control' : 'h-3 bg-fill-strong')} />
      ))}
    </div>
  );
}

export function RunsOn({ org, api, business }: { org: string; api: SiteApiDetail; business: string | undefined }) {
  const { pace } = api;
  const [ws = '', name = ''] = api.lane.split('/');
  const others = pace.others.reduce((sum, item) => sum + item.used, 0);
  const used = pace.used + others;
  const top = Math.max(1, ...pace.hours);
  return (
    <section aria-labelledby="site-runs-on" className="min-w-0">
      <h2 id="site-runs-on" className="mb-4 flex h-7 items-center text-section-title">
        What it runs on
      </h2>
      <div className="rounded-(--radius-card) border border-line">
        <Link
          to="/$org/lanes/$ws/$name"
          params={{ org, ws, name }}
          className="group flex items-center gap-3 rounded-t-(--radius-card) border-b border-line p-4 transition-colors duration-(--duration-fast) ease-standard hover:bg-hover"
        >
          <SiteMark site={markOf(api.site)} size={32} tile label={false} className="bg-card" />
          <span className="min-w-0 flex-1">
            <span className="block truncate text-label">{api.account}</span>
            <span className="block truncate text-caption text-faint">
              {business ? `${business}, ` : ''}on {api.computer}
            </span>
          </span>
          <ArrowUpRight aria-hidden="true" className="size-4 shrink-0 text-faint transition-transform duration-(--duration-fast) ease-standard group-hover:translate-x-px group-hover:-translate-y-px" />
        </Link>
        <div className="grid gap-3 p-4">
          <p className="flex items-baseline gap-2">
            <span className="text-heading">{used}</span>
            <span className="text-secondary text-muted">of {pace.per_hour} reads used this hour</span>
          </p>
          <Ticks pace={pace} />
          <ul className="grid gap-1 text-caption text-muted">
            <li className="flex items-center gap-2">
              <span aria-hidden="true" className="size-2 rounded-sm bg-fg" />
              <span className="min-w-0 flex-1 truncate">This Site API</span>
              <span className="tabular">{pace.used}</span>
            </li>
            {pace.others.map((item) => (
              <li key={item.label} className="flex items-center gap-2">
                <span aria-hidden="true" className="size-2 rounded-sm bg-control" />
                <span className="min-w-0 flex-1 truncate">{item.label}</span>
                <span className="tabular">{item.used}</span>
              </li>
            ))}
          </ul>
          <div className="border-t border-line pt-3">
            <div aria-hidden="true" className="flex h-6 items-end gap-1">
              {pace.hours.map((count, index) => (
                <span key={index} className={cn('flex-1 rounded-t-[2px]', index === pace.hours.length - 1 ? 'bg-fg' : 'bg-fill-strong')} style={{ height: `${Math.max(8, (count / top) * 100)}%` }} />
              ))}
            </div>
            <p className="mt-1 text-caption text-faint">Reads in each of the last 12 hours</p>
          </div>
          <p className="text-caption text-pretty text-faint">Pace protects this account from your own tools. A saved answer costs nothing here.</p>
        </div>
      </div>
    </section>
  );
}
