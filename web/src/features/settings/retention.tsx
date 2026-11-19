import { SegmentedControl } from '@/components/ui/tabs';
import { useParams } from '@tanstack/react-router';
import { Download } from 'lucide-react';
import { useState } from 'react';
import { useExportActivity, useRetention, useWorkspaces } from '@/api/hooks';
import { isCancelled } from '@/api/http';
import type { Retention } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Card, FormCard } from '@/components/ui/card';
import { Select } from '@/components/ui/select';
import { QueryView, Skeleton } from '@/components/ui/states';
import { describeError } from '@/lib/errors';
import { date } from '@/lib/format';
import { SettingsHeader } from './page';

const DAY = 86_400_000;

const plan = (name: string) => `${name.charAt(0).toUpperCase()}${name.slice(1)}`;

/**
 * The kept window as one bar: the plan's days, any longer hold after a downgrade, and where the
 * oldest stored activity sits. Only drawn from what the API said.
 */
function Window({ data }: { data: Retention }) {
  const now = Date.now();
  const span = Math.max(data.kept_days, data.retention_days);
  const planStart = 100 - (data.retention_days / span) * 100;
  const oldest = data.oldest_at ? Math.max(0, Math.min(100, 100 - ((now - new Date(data.oldest_at).getTime()) / DAY / span) * 100)) : null;
  return (
    <figure className="grid gap-2">
      <div className="relative h-6 overflow-hidden rounded-md border border-line bg-inset" aria-hidden="true">
        {data.kept_days > data.retention_days ? (
          <span className="absolute inset-y-0 left-0 bg-hover" style={{ width: `${planStart}%` }} />
        ) : null}
        <span className="absolute inset-y-0 right-0 bg-fill-strong" style={{ left: `${planStart}%` }} />
        {oldest !== null ? <span className="absolute inset-y-0 w-px bg-fg" style={{ left: `${oldest}%` }} /> : null}
      </div>
      <div className="mono tabular flex justify-between text-caption text-faint" aria-hidden="true">
        <span>{span} days ago</span>
        <span>Now</span>
      </div>
      <figcaption className="text-secondary text-muted">
        {data.oldest_at ? `The oldest history kept is from ${date(data.oldest_at)}.` : 'Nothing is stored yet.'} Older than the window is removed by a regular sweep.
      </figcaption>
    </figure>
  );
}

const RANGES = [
  { value: 'all', label: 'All kept' },
  { value: '30', label: '30 days' },
  { value: '7', label: '7 days' },
] as const;

const FORMATS = [
  { value: 'csv', label: 'CSV' },
  { value: 'ndjson', label: 'NDJSON' },
] as const;

function save(blob: Blob, name: string) {
  const link = document.createElement('a');
  link.href = URL.createObjectURL(blob);
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

/** A computer keeps no plan, so nothing is stored past a downgrade: what is stored is all there is. */
function Export({ org, computer }: { org: string; computer: boolean }) {
  const exportNote = computer
    ? 'Everything still stored in the action log. It is your data, and this is the way to take it.'
    : 'Everything still stored, which after a downgrade can be more than the panel shows. It is your data, and this is the way to take it.';
  const workspaces = useWorkspaces(org).data ?? [];
  const [format, setFormat] = useState<'csv' | 'ndjson'>('csv');
  const [range, setRange] = useState<'all' | '30' | '7'>('all');
  const [workspace, setWorkspace] = useState('all');
  const run = useExportActivity(org);
  const start = () =>
    run.mutate(
      {
        format,
        start: range === 'all' ? undefined : new Date(Date.now() - Number(range) * DAY).toISOString(),
        workspace: workspace === 'all' ? undefined : workspace,
      },
      { onSuccess: ({ blob, filename }) => save(blob, filename ?? `agentos-activity-${org}.${format}`) },
    );
  return (
    <FormCard
      title="Export history"
      description={exportNote}
      footnote={
        run.isError && !isCancelled(run.error) ? (
          <span role="alert" className="text-bad">
            {describeError(run.error)}
          </span>
        ) : run.isSuccess ? (
          'Downloaded. The export is in the audit log.'
        ) : (
          'In a spreadsheet, a cell that could run as a formula starts with an apostrophe.'
        )
      }
      action={
        <Button variant="primary" size="sm" icon={<Download className="size-4" />} loading={run.isPending} onClick={start}>
          Export
        </Button>
      }
    >
      <div className="flex flex-wrap items-center gap-2">
        <SegmentedControl label="Format" value={format} options={[...FORMATS]} onValueChange={setFormat} />
        <SegmentedControl label="Range" value={range} options={[...RANGES]} onValueChange={setRange} />
        <Select
          size="sm"
          className="w-44"
          aria-label="Workspace"
          value={workspace}
          onValueChange={setWorkspace}
          options={[{ value: 'all', label: 'Every workspace' }, ...workspaces.map((item) => ({ value: item.id, label: item.name }))]}
        />
      </div>
    </FormCard>
  );
}

function Body({ org, data }: { org: string; data: Retention }) {
  const computer = data.plan === undefined;
  return (
    <div className="grid gap-6">
      <FormCard
        title="History"
        description={
          computer
            ? `History is kept for ${data.retention_days} days on this computer.`
            : `History is kept for ${data.retention_days} days on your ${plan(data.plan ?? '')} plan.`
        }
        footnote={
          computer
            ? 'Only the action log is trimmed to this window.'
            : data.hold_until
              ? `After your plan change, history stored before it is kept until ${date(data.hold_until)}, then the ${data.retention_days} day window applies.`
              : 'A longer plan keeps more. A shorter one keeps what is stored until its old window ends.'
        }
      >
        <div className="flex items-baseline gap-2">
          <span className="mono tabular text-heading">{data.kept_days}</span>
          <span className="text-body text-muted">days kept now</span>
        </div>
        <Window data={data} />
      </FormCard>
      {data.can_export ? (
        <Export org={org} computer={computer} />
      ) : (
        <Card className="px-6 py-4">
          <p className="text-body text-muted">Only the owner can export history, because the export holds every workspace.</p>
        </Card>
      )}
      {computer ? (
        <FormCard title="What the window does not touch" description="Two things are kept whatever the window says.">
          <ul className="grid gap-2 text-body text-muted">
            <li>Runs. Their records stay on this computer.</li>
            <li>An approval still waiting for a person. It is never deleted.</li>
          </ul>
        </FormCard>
      ) : (
        <FormCard title="What is never kept" description="agentos only keeps what it needs to show you history.">
          <ul className="grid gap-2 text-body text-muted">
            <li>Computer logs are passed through to your screen and never stored.</li>
            <li>Passwords, cookies and vault secrets stay on your computers. They are never part of history.</li>
            <li>An approval still waiting for a person is never deleted.</li>
          </ul>
        </FormCard>
      )}
    </div>
  );
}

export function RetentionSettings() {
  const { org } = useParams({ from: '/$org/settings/retention' });
  const retention = useRetention(org);
  return (
    <>
      <SettingsHeader title="Retention and export" description="How long history is kept, and how to take a copy." />
      <QueryView card query={retention} skeleton={<Skeleton className="h-64 rounded-xl" />}>
        {(data) => <Body org={org} data={data} />}
      </QueryView>
    </>
  );
}
