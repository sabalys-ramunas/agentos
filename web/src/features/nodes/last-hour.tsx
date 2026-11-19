/**
 * The last hour: one line per machine, one bar per real run (`agentos run`) with its start and end,
 * in its status colour. Each bar is a link to its run and says what it was. Drawn only from runs the
 * node keeps, so a machine with no runs has an empty line, not a made-up one.
 */
import { Link } from '@tanstack/react-router';
import type { CSSProperties } from 'react';
import { Card, CardHeader } from '@/components/ui/card';
import { StatusDot } from '@/components/ui/status';
import { cn } from '@/lib/cn';
import { clock } from '@/lib/format';
import type { BarTone, Lane } from './model';
import { BAR_WORDS, HOUR_MS } from './model';

const FILL: Record<BarTone, string> = {
  live: 'bg-chart-bar-strong',
  ok: 'bg-ok-dot',
  bad: 'bg-bad-dot',
  idle: 'bg-idle-dot',
};

const TICKS = [0, 0.25, 0.5, 0.75, 1];

/** Over this many machines, only those with runs in the hour get a line, and the rest are counted. */
const QUIET_AFTER = 6;

export function LastHour({ org, lanes: all, now }: { org: string; lanes: Lane[]; now: number }) {
  const start = now - HOUR_MS;
  const lanes = all.length > QUIET_AFTER ? all.filter((lane) => lane.bars.length) : all;
  const quiet = all.length - lanes.length;
  return (
    <Card>
      <CardHeader
        title="The last hour"
        meta={<span className="max-md:hidden">each bar is a run; point at one for what it was</span>}
        action={
          <span className="flex flex-wrap items-center gap-3 text-caption text-faint max-sm:hidden">
            <span className="inline-flex items-center gap-1">
              <StatusDot tone="live" />
              running
            </span>
            <span className="inline-flex items-center gap-1">
              <StatusDot tone="ok" />
              passed
            </span>
            <span className="inline-flex items-center gap-1">
              <StatusDot tone="bad" />
              failed
            </span>
            <span className="inline-flex items-center gap-1">
              <StatusDot tone="idle" />
              cancelled
            </span>
          </span>
        }
      />
      <div className="grid gap-2 px-5 py-4">
        {lanes.map(({ row, bars }) => (
          <div key={row.id} className="grid grid-cols-[minmax(0,8rem)_minmax(0,1fr)] items-center gap-3 sm:grid-cols-[minmax(0,10rem)_minmax(0,1fr)]">
            <span className="truncate font-mono text-code text-muted" title={row.name}>
              {row.name}
            </span>
            <div
              role="list"
              aria-label={`Runs on ${row.name} in the last hour`}
              className={cn('relative h-6 rounded-sm bg-hover', row.node?.state === 'down' && 'opacity-60')}
            >
              {bars.map((bar) => {
                const words = `${bar.job.command}, ${BAR_WORDS[bar.tone]}, from ${clock(bar.job.started_at)}${bar.job.finished_at ? ` to ${clock(bar.job.finished_at)}` : ' until now'}`;
                const share = `${(bar.to - bar.from) * 100}%`;
                const drawn: CSSProperties = { left: `${bar.from * 100}%`, width: `max(4px, ${share})` };
                // The link is at least 24 px wide around the bar, so a short run is still easy to hit.
                const target: CSSProperties = { left: `calc(${bar.from * 100}% + (max(4px, ${share}) - max(24px, ${share})) / 2)`, width: `max(24px, ${share})` };
                return (
                  <span role="listitem" key={bar.job.id} className="group">
                    <span aria-hidden="true" style={drawn} className={cn('pointer-events-none absolute inset-y-1 rounded-sm group-hover:opacity-80', FILL[bar.tone], bar.tone === 'live' && 'rounded-r-none')} />
                    <Link to="/$org/runs/$id" params={{ org, id: bar.job.id }} aria-label={words} title={words} style={target} className="absolute inset-y-0 rounded-sm" />
                  </span>
                );
              })}
              {bars.length === 0 ? <span className="sr-only">No runs.</span> : null}
            </div>
          </div>
        ))}
        {quiet ? <p className="text-secondary text-faint">{quiet === 1 ? 'One other machine' : `The other ${quiet} machines`} ran nothing in the hour.</p> : null}
        <div className="grid grid-cols-[minmax(0,8rem)_minmax(0,1fr)] gap-3 sm:grid-cols-[minmax(0,10rem)_minmax(0,1fr)]" aria-hidden="true">
          <span />
          <div className="relative h-4 font-mono text-micro text-faint">
            {TICKS.map((tick) => (
              <span
                key={tick}
                className={cn('absolute top-0', tick === 0 ? 'left-0' : tick === 1 ? 'right-0' : '-translate-x-1/2', tick === 0.25 || tick === 0.75 ? 'max-sm:hidden' : '')}
                style={tick > 0 && tick < 1 ? { left: `${tick * 100}%` } : undefined}
              >
                {tick === 1 ? 'now' : clock(new Date(start + tick * HOUR_MS).toISOString())}
              </span>
            ))}
          </div>
        </div>
      </div>
    </Card>
  );
}
