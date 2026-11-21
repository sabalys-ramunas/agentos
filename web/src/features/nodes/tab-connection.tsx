import { useState } from 'react';
import type { ConnectionKind, NodeConnections, NodeDetail, NodeSystem } from '@/api/types';
import { Card } from '@/components/ui/card';
import { CopyButton } from '@/components/ui/misc';
import { StatusDot } from '@/components/ui/status';
import { Skeleton } from '@/components/ui/states';
import { date, dateTime, plural, timeAgo } from '@/lib/format';
import type { Tone } from '@/lib/labels';
import { IdentityPanel } from './tab-identity';
import { Facts, NONE, NotReported, Panel } from './parts';

const KIND: Record<ConnectionKind, { word: string; tone: Tone }> = {
  connected: { word: 'Connected', tone: 'ok' },
  recovered: { word: 'Recovered', tone: 'ok' },
  backlog_flushed: { word: 'Caught up', tone: 'neutral' },
  degraded: { word: 'Degraded', tone: 'warn' },
  disconnected: { word: 'Went away', tone: 'bad' },
};

/**
 * Thirty days, one bar each. Every bar is a whole day, and the time away is a red cap on top,
 * scaled up so a few minutes still show. The exact figure is in the line above on hover.
 */
function UptimeBars({ connections }: { connections: NodeConnections }) {
  const [active, setActive] = useState<number | null>(null);
  const day = active === null ? null : connections.days[active];
  const incidents = connections.days.reduce((sum, item) => sum + item.incidents, 0);
  return (
    <Card className="p-4 sm:p-6">
      <div className="flex items-baseline justify-between gap-4">
        <p className="mono tabular text-heading font-normal">{connections.uptime_pct}%</p>
        {/* A fixed line, so reading a bar never moves the chart. */}
        <p className="tabular truncate text-secondary text-muted" aria-live="polite">
          {day ? `${date(day.day)}: ${day.up_pct}% up${day.incidents ? `, ${plural(day.incidents, 'incident')}` : ''}` : `${plural(incidents, 'incident')} in 30 days`}
        </p>
      </div>
      <div
        role="img"
        aria-label={`Up ${connections.uptime_pct}% over 30 days, with ${plural(incidents, 'incident')}.`}
        className="mt-4 flex h-16 items-end gap-1"
        onPointerLeave={() => setActive(null)}
      >
        {connections.days.map((item, index) => (
          <span
            key={item.day}
            onPointerEnter={() => setActive(index)}
            className="relative block h-full flex-1 overflow-hidden rounded-[2px] bg-fill-strong transition-opacity duration-(--duration-fast) ease-standard"
            style={{ opacity: active !== null && active !== index ? 0.45 : 1 }}
          >
            {item.incidents || item.up_pct < 100 ? <span className="absolute inset-x-0 top-0 bg-bad" style={{ height: `${Math.min(100, Math.max(12, (100 - item.up_pct) * 8))}%` }} /> : null}
          </span>
        ))}
      </div>
      <div className="mt-2 flex justify-between text-caption text-faint">
        <span>30 days ago</span>
        <span>Today</span>
      </div>
    </Card>
  );
}

export function ConnectionTab({ org, node, system, connections, owner }: { org: string; node: NodeDetail; system: NodeSystem | null | undefined; connections: NodeConnections | null | undefined; owner: boolean }) {
  const events = connections ? [...connections.events].sort((a, b) => Date.parse(b.at) - Date.parse(a.at)) : [];
  const reconnects = events.filter((event) => event.kind === 'connected').length;
  return (
    <div className="grid gap-10">
      {/* Still loading is undefined; a node that does not report it answers null, which says so rather than loading. */}
      <Panel title="Uptime" description="How much of each day this machine was connected to openajar.">
        {connections === undefined ? <Skeleton className="h-36 rounded-xl" /> : connections === null ? <NotReported node={node.id} what="its uptime" /> : <UptimeBars connections={connections} />}
      </Panel>
      <Panel title="The link" description="How this machine proves who it is to openajar. Its private key never leaves it.">
        <Facts
          items={[
            { label: 'Link version', value: system ? `Version ${system.protocol}` : NONE },
            { label: 'Last report', value: timeAgo(node.last_seen) },
            { label: 'Reconnects, 30 days', value: connections ? String(reconnects) : NONE },
            { label: 'Enrolled', value: system?.enrolled_at ? date(system.enrolled_at) : NONE },
            { label: 'Enrolled by', value: system?.enrolled_by ?? NONE },
            { label: 'openajar', value: node.version, mono: true },
            { label: 'Clock', value: system?.timezone ?? node.timezone ?? NONE },
            { label: 'Architecture', value: system?.arch ?? node.arch, mono: true },
          ]}
        />
        {system?.key_fingerprint ? (
          <Card className="flex items-center gap-3 py-2 pr-2 pl-4">
            <span className="shrink-0 text-caption text-faint">Key</span>
            <code className="mono min-w-0 flex-1 truncate text-secondary">{system.key_fingerprint}</code>
            <CopyButton text={system.key_fingerprint} label="Copy the key fingerprint" />
          </Card>
        ) : null}
      </Panel>
      <IdentityPanel org={org} node={node.id} owner={owner} />
      {/* Said once, in Uptime above: a node that reports no connections has no log either. */}
      {connections === null ? null : (
        <Panel title="Connection log" description="What openajar saw, newest first.">
          {connections ? (
            <Card className="overflow-hidden">
              <ol className="divide-y divide-line">
                {events.map((event) => (
                  <li key={`${event.at}:${event.kind}`} className="flex items-center gap-3 px-4 py-3 sm:px-6">
                    <StatusDot tone={KIND[event.kind].tone} />
                    <span className="w-24 shrink-0 text-body">{KIND[event.kind].word}</span>
                    <span className="min-w-0 flex-1 truncate text-secondary text-muted">{event.detail ?? ''}</span>
                    <time dateTime={event.at} title={dateTime(event.at)} className="tabular shrink-0 text-caption text-faint">
                      {timeAgo(event.at)}
                    </time>
                  </li>
                ))}
              </ol>
            </Card>
          ) : (
            <Skeleton className="h-48 rounded-xl" />
          )}
        </Panel>
      )}
    </div>
  );
}
