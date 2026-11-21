/**
 * The top of a machine's page, as drawn: its tile, its name with its state as a chip, what it does in one
 * plain sentence and one line of facts, then two cards beside the tabs: About it (state, last report, size,
 * what it runs, its time) and Load (busy, memory, disk, warm accounts, how often it was away). Every number
 * is what the node reports; one it does not report says so.
 */
import type { ReactNode } from 'react';
import type { FreezeRecord, NodeConnections, NodeDetail, NodeSystem } from '@/api/types';
import type { Band } from '@/components/charts/time-chart';
import { Card, CardHeader } from '@/components/ui/card';
import { KindEyebrow, KindTile } from '@/components/ui/kind';
import { Detail, Meter, PageHeader } from '@/components/ui/misc';
import { StateBadge } from '@/components/ui/state-badge';
import { Status } from '@/components/ui/status';
import { Skeleton } from '@/components/ui/states';
import { clock } from '@/lib/format';
import { frozenSentence } from '@/lib/freeze';
import { nodeState } from '@/lib/labels';
import { useNow } from '@/lib/use-now';
import { FREEZE_WORDS } from '@/lib/vocabulary';
import { zoneCity, zoneClock } from '@/lib/zone';
import { span, uptime } from './describe';
import { gbShort } from './model';

interface HeaderProps {
  node: NodeDetail;
  sentence: string | null;
  system: NodeSystem | null | undefined;
  actions: ReactNode;
  /** The freeze on this whole computer, when there is one. */
  frozen?: FreezeRecord | null;
}

export function NodeHeader({ node, sentence, system, actions, frozen }: HeaderProps) {
  const now = useNow(true);
  const zone = system?.timezone ?? node.timezone ?? null;
  const facts = [
    system?.os ?? node.os,
    system?.cores ? `${system.cores} cores` : null,
    system?.mem_total_mb ? `${gbShort(system.mem_total_mb)} memory` : node.mem.total_mb ? `${gbShort(node.mem.total_mb)} memory` : null,
    zone ? `${zoneCity(zone)} time, ${zoneClock(now, zone)}` : null,
  ].filter(Boolean);

  return (
    <PageHeader
      lead={<KindTile kind="machine" size="xl" />}
      eyebrow={<KindEyebrow kind="machine" tile={false} />}
      title={node.id}
      aside={
        <>
          <Status chip status={nodeState[node.state]} />
          {frozen ? <StateBadge state="stuck" label={FREEZE_WORDS.frozen.word} sentence={frozenSentence(frozen)} className="shrink-0" /> : null}
        </>
      }
      description={
        <>
          {sentence ? <span className="text-pretty">{sentence}</span> : <Skeleton className="h-5 w-96 max-w-full" />}
          <span className="text-secondary text-muted tabular">{facts.join(' · ')}</span>
        </>
      }
      actions={actions}
    />
  );
}

/** About it: the facts a person asks first, label on the left and the answer on the right. */
export function AboutCard({ node, system, bands }: { node: NodeDetail; system: NodeSystem | null | undefined; bands: Band[] }) {
  const now = useNow(true);
  const down = node.state === 'down';
  const lastSeen = Date.parse(node.last_seen);
  const away = bands.find((band) => band.to >= now - 1_000 && band.from <= now);
  const ago = Math.max(0, Math.round((now - lastSeen) / 1000));
  const zone = system?.timezone ?? node.timezone ?? null;
  const size = [system?.cpu_model, system?.cores ? `${system.cores} cores` : null, gbShort(system?.mem_total_mb ?? node.mem.total_mb)].filter(Boolean);
  return (
    <Card>
      <CardHeader title="About it" />
      <dl className="divide-y divide-line px-5 py-1">
        <Detail label="State">
          <Status chip status={nodeState[node.state]} />
        </Detail>
        <Detail label="Last report">
          {down || away ? (
            <span className="text-bad">
              Away since {clock(new Date(away?.from ?? lastSeen).toISOString())} ({span(now - (away?.from ?? lastSeen))})
            </span>
          ) : (
            <span className="tabular">{ago < 5 ? 'Just now' : `${ago} s ago`}</span>
          )}
        </Detail>
        {system?.uptime_s ? (
          <Detail label="Up for">
            <span className="tabular">{uptime(system.uptime_s)}</span>
          </Detail>
        ) : null}
        <Detail label="Size">{size.join(' · ')}</Detail>
        <Detail label="Runs">
          {[system?.os ?? node.os, system?.arch ?? node.arch].filter(Boolean).join(', ')} · openajar {node.version}
        </Detail>
        {zone ? (
          <Detail label="Time">
            <span className="tabular">{zoneClock(now, zone)}</span> {zoneCity(zone)}
          </Detail>
        ) : null}
      </dl>
    </Card>
  );
}

function LoadRow({ label, value, meter }: { label: string; value: ReactNode; meter?: { value: number; max: number } }) {
  return (
    <div className="grid gap-2">
      <div className="flex items-baseline justify-between gap-4 text-secondary">
        <span className="text-muted">{label}</span>
        <span className="text-right text-fg tabular">{value}</span>
      </div>
      {meter ? <Meter value={meter.value} max={meter.max} label={label} className="w-full" /> : null}
    </div>
  );
}

/** Load: what it is using now, each against what it has, and how often it was away this month. */
export function LoadCard({ node, system, connections }: { node: NodeDetail; system: NodeSystem | null | undefined; connections: NodeConnections | null | undefined }) {
  const down = node.state === 'down';
  const diskTotal = system?.disk_total_mb;
  const diskFree = node.load.disk_free_mb;
  const incidents = connections ? connections.days.reduce((sum, item) => sum + item.incidents, 0) : null;
  return (
    <Card>
      <CardHeader title="Load" meta={down ? 'Not answering, so nothing is current' : 'Right now'} />
      <div className="grid gap-4 px-5 py-4">
        <LoadRow label="Busy" value={down ? '–' : `${node.cpu}%`} meter={down ? undefined : { value: node.cpu, max: 100 }} />
        <LoadRow
          label="Memory"
          value={down ? '–' : `${gbShort(node.mem.used_mb)} of ${gbShort(node.mem.total_mb)}`}
          meter={down || !node.mem.total_mb ? undefined : { value: node.mem.used_mb, max: node.mem.total_mb }}
        />
        <LoadRow
          label="Disk"
          value={diskTotal ? `${gbShort(diskTotal - diskFree)} of ${gbShort(diskTotal)}` : `${gbShort(diskFree)} free`}
          meter={diskTotal ? { value: diskTotal - diskFree, max: diskTotal } : undefined}
        />
        <LoadRow
          label="Warm accounts"
          value={`${node.warm_lanes} of ${node.max_warm_lanes}`}
          meter={node.max_warm_lanes ? { value: node.warm_lanes, max: node.max_warm_lanes } : undefined}
        />
      </div>
      <p className="border-t border-line px-5 py-3 text-secondary text-muted">
        {connections ? (
          <>
            Up <span className="text-fg tabular">{connections.uptime_pct}%</span> of the last 30 days
            {incidents ? `, away ${incidents === 1 ? 'once' : `${incidents} times`}` : ', never away'}.
          </>
        ) : (
          'It does not report how often it was away yet.'
        )}
      </p>
    </Card>
  );
}
