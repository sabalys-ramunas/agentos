/**
 * The digest as it would read if it went out now, built from the same numbers the email uses:
 * what waits, what agents did, and each workspace's line. Nothing here is a sample.
 */
import { Link } from '@tanstack/react-router';
import { Mail } from 'lucide-react';
import { useAccess, useActivityMetrics, useApprovals, useOrg, useWorkspaceActivity, useWorkspaces } from '@/api/hooks';
import type { ActivityBucket, Approval, Effect, NotificationSettings } from '@/api/types';
import { Card } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/states';
import { cn } from '@/lib/cn';
import { plural, usd } from '@/lib/format';
import { laneName } from '@/lib/labels';

/** The cloud's words for a waiting item, as in the email and the push. */
const TITLES: Partial<Record<Effect, string>> = {
  commit: 'Something is ready to publish',
  pay: 'A payment needs you',
  irreversible: 'An irreversible action needs you',
  secret: 'A login needs you',
  exec: 'A command needs you',
  service: 'A service needs you',
  brain: 'Model spend needs you',
  install_overlay: 'An install needs you',
  install_base: 'A base image change needs you',
};

const SHOWN = 5;

const title = (item: Approval) => (item.kind === 'question' ? 'An agent has a question' : (TITLES[item.effect] ?? 'An approval needs you'));

interface Line {
  name: string;
  events: number;
  verified: number;
  unverified: number;
  failed: number;
  decided: number;
}

function sum(buckets: ActivityBucket[]) {
  return buckets.reduce(
    (total, bucket) => ({
      events: total.events + bucket.events,
      verified: total.verified + bucket.runs.succeeded_verified,
      unverified: total.unverified + bucket.runs.succeeded_unverified,
      failed: total.failed + bucket.runs.partial + bucket.runs.failed,
      decided: total.decided + bucket.approvals.approved + bucket.approvals.denied,
      usd: total.usd + bucket.cost.usd,
    }),
    { events: 0, verified: 0, unverified: 0, failed: 0, decided: 0, usd: 0 },
  );
}

/** One bar per four hours of a day, or per day of a week, as the email draws them. */
function bars(buckets: ActivityBucket[], weekly: boolean, timezone: string) {
  const step = weekly ? 24 : 4;
  const groups: { label: string; events: number }[] = [];
  for (let index = 0; index < buckets.length; index += step) {
    const slice = buckets.slice(index, index + step);
    const start = slice[0];
    if (!start) continue;
    const at = new Date(start.at);
    const label = weekly
      ? at.toLocaleDateString('en', { weekday: 'short', timeZone: timezone })
      : at.toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: timezone });
    groups.push({ label, events: slice.reduce((total, bucket) => total + bucket.events, 0) });
  }
  return groups;
}

export function DigestPreview({ org, period, timezone }: { org: string; period: NotificationSettings['email_digest']; timezone: string }) {
  const weekly = period === 'weekly';
  const range = weekly ? '7d' : '24h';
  const name = useOrg(org).data?.name ?? org;
  const metrics = useActivityMetrics(org, range);
  const workspaces = useWorkspaces(org).data ?? [];
  const perWorkspace = useWorkspaceActivity(org, range, workspaces.map((item) => item.id));
  const approvals = useApprovals(org);
  const canDecide = useAccess(org).can('approvals.decide');

  const loading = metrics.isPending || approvals.isPending || perWorkspace.some((item) => item.isPending);
  const totals = sum(metrics.data?.buckets ?? []);
  const lines: Line[] = workspaces
    .map((workspace, index) => ({ name: workspace.name, ...sum(perWorkspace[index]?.data?.buckets ?? []) }))
    .filter((line) => line.events || line.verified || line.unverified || line.failed || line.decided)
    .sort((a, b) => b.events - a.events || a.name.localeCompare(b.name));
  // A viewer sees the numbers but is not asked to decide, so nothing is listed as waiting on them.
  // Oldest first, as the email lists them.
  const waiting = canDecide ? [...(approvals.data ?? [])].sort((a, b) => a.created_at.localeCompare(b.created_at)) : [];
  const runs = totals.verified + totals.unverified + totals.failed;
  const chart = bars(metrics.data?.buckets ?? [], weekly, timezone);
  const tallest = Math.max(1, ...chart.map((bar) => bar.events));
  const word = weekly ? 'week' : 'day';
  const subject = `Your ${word} on openajar · ${name}${waiting.length ? ` · ${waiting.length} need you` : ''}`;

  return (
    <Card>
      <div className="flex items-start justify-between gap-4 px-6 pt-5 pb-4">
        <div className="min-w-0">
          <h3 className="text-section-title">Digest preview</h3>
          <p className="mt-1 text-secondary text-muted">
            {period === 'off' ? 'The digest is off. If you turn it on, the next one would read like this.' : `Your next ${period} digest, if it went out now.`}
          </p>
        </div>
        <Mail className="mt-1 size-4 shrink-0 text-faint" />
      </div>
      <div className={cn('mx-6 mb-6 overflow-hidden rounded-lg border border-line', period === 'off' && 'opacity-70')}>
        <div className="border-b border-line bg-inset px-4 py-3">
          <p className="text-caption text-faint">From openajar</p>
          {loading ? <Skeleton className="mt-1 h-5 w-2/3" /> : <p className="mt-1 truncate text-label">{subject}</p>}
        </div>
        {loading ? (
          <div className="grid gap-3 p-4" role="status" aria-label="Loading">
            <Skeleton className="h-4 w-1/3" />
            <Skeleton className="h-4" />
            <Skeleton className="h-16" />
          </div>
        ) : !waiting.length && !lines.length ? (
          <p className="p-4 text-body text-muted">Nothing happened and nothing waits, so no digest would go out.</p>
        ) : (
          <div className="grid gap-6 p-4">
            {waiting.length ? (
              <section>
                <h3 className="text-label">{waiting.length === 1 ? '1 needs you' : `${waiting.length} need you`}</h3>
                <ul className="mt-2 grid gap-2">
                  {waiting.slice(0, SHOWN).map((item) => {
                    const [ws = '', lane = ''] = item.lane.split('/');
                    const place = [workspaces.find((entry) => entry.id === ws)?.name ?? ws, lane ? laneName(lane) : ''].filter(Boolean).join(' · ');
                    return (
                      <li key={item.id} className="text-secondary">
                        <span className="text-fg">{title(item)}</span>
                        {place ? <span className="text-faint"> · {place}</span> : null}
                        <span className="block truncate text-muted">{item.summary}</span>
                      </li>
                    );
                  })}
                </ul>
                {waiting.length > SHOWN ? <p className="mt-2 text-secondary text-faint">and {waiting.length - SHOWN} more</p> : null}
                <Link to="/$org/needs-you" params={{ org }} className="mt-3 inline-flex text-secondary link hover:underline">
                  Open Needs you
                </Link>
              </section>
            ) : null}
            {lines.length ? (
              <section>
                <h3 className="text-label">What your agents did</h3>
                <p className="mt-1 text-secondary text-muted">
                  {plural(runs, 'run')}, {totals.failed} failed or partial{totals.usd ? `, ${usd(totals.usd)} in model spend` : ''}
                </p>
                {chart.some((bar) => bar.events) ? (
                  <figure className="mt-4">
                    <div className="flex h-12 items-end gap-1" aria-hidden="true">
                      {chart.map((bar) => (
                        <span
                          key={bar.label}
                          className="flex-1 rounded-sm bg-fill-strong"
                          style={{ height: `${Math.max(bar.events ? 8 : 2, (bar.events / tallest) * 100)}%` }}
                        />
                      ))}
                    </div>
                    <div className="mono tabular mt-1 flex gap-1 text-caption text-faint" aria-hidden="true">
                      {chart.map((bar) => (
                        <span key={bar.label} className="flex-1 truncate text-center">
                          {bar.label}
                        </span>
                      ))}
                    </div>
                    <figcaption className="sr-only">
                      Events by {weekly ? 'day' : 'four hours'}: {chart.map((bar) => `${bar.label} ${bar.events}`).join(', ')}
                    </figcaption>
                  </figure>
                ) : null}
                <table className="mt-4 w-full text-left text-secondary">
                  <caption className="sr-only">Each workspace</caption>
                  <thead>
                    <tr className="text-faint">
                      <th scope="col" className="py-1 font-normal">
                        Workspace
                      </th>
                      <th scope="col" className="py-1 text-right font-normal">
                        Events
                      </th>
                      <th scope="col" className="hidden py-1 text-right font-normal sm:table-cell">
                        Verified
                      </th>
                      <th scope="col" className="py-1 text-right font-normal">
                        Failed
                      </th>
                      <th scope="col" className="hidden py-1 text-right font-normal sm:table-cell">
                        Decided
                      </th>
                    </tr>
                  </thead>
                  <tbody className="mono tabular">
                    {lines.map((line) => (
                      <tr key={line.name} className="border-t border-line">
                        <th scope="row" className="py-2 pr-2 font-sans font-normal text-fg">
                          {line.name}
                        </th>
                        <td className="py-2 text-right">{line.events}</td>
                        <td className="hidden py-2 text-right sm:table-cell">{line.verified}</td>
                        <td className="py-2 text-right">{line.failed}</td>
                        <td className="hidden py-2 text-right sm:table-cell">{line.decided}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </section>
            ) : null}
          </div>
        )}
      </div>
    </Card>
  );
}
