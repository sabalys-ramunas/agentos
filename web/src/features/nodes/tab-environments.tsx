import { useState } from 'react';
import { useNodeEnvironments, useServiceAction, useServiceLogs } from '@/api/hooks';
import type { EnvLaneRow, EnvServiceRow, LaneState, NodeEnvironments } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Dialog } from '@/components/ui/dialog';
import { Status } from '@/components/ui/status';
import { ErrorState, Skeleton } from '@/components/ui/states';
import { dateTime, plural, timeAgo } from '@/lib/format';
import { laneState, serviceState } from '@/lib/labels';
import { plainReason } from '@/lib/plain-node';
import { useTechnical } from '@/lib/technical';
import { NotReported, Panel } from './parts';

/** The commands for what the app does not do here. Each on its own line, as it is typed. */
const COMMANDS = ['agentos env create <workspace>/<name>', 'agentos env remove <workspace>/<name>', 'agentos env images build dev-box'];

/** The one sentence a person reads first: how many services run, or that none are set up. */
export function environmentsAnswer(data: NodeEnvironments): string {
  const services = data.services.length;
  const running = data.services.filter((service) => service.state === 'running').length;
  if (!services) return 'This machine runs no services yet. A service is a command that stays up, such as a small API.';
  if (running === services) return services === 1 ? 'The one service is running.' : `All ${services} services are running.`;
  return `${running} of ${plural(services, 'service')} running.`;
}

function ServiceRow({ org, node, row, owner, onLogs }: { org: string; node: string; row: EnvServiceRow; owner: boolean; onLogs: (lane: string) => void }) {
  const action = useServiceAction(org, node);
  const running = row.state === 'running' || row.state === 'starting';
  const busy = action.isPending;
  return (
    <div className="flex flex-wrap items-center gap-x-6 gap-y-3 px-4 py-3 sm:px-6">
      <div className="min-w-0 flex-1">
        <p className="text-14">{row.name}</p>
        <p className="mono mt-1 truncate text-12 text-faint">{row.lane}</p>
      </div>
      <div className="min-w-[8rem] text-13">
        <Status status={serviceState[row.state] ?? { label: row.state, tone: 'neutral' }} />
        <p className="mt-1 text-12 text-faint">
          {row.restarts ? `Restarted ${plural(row.restarts, 'time')}` : 'Never restarted'}
          {row.started_at ? `, started ${timeAgo(row.started_at)}` : ''}
        </p>
      </div>
      <div className="flex items-center gap-2">
        {owner ? (
          <Button
            size="sm"
            variant={running ? 'secondary' : 'primary'}
            loading={busy}
            onClick={() => {
              if (!busy) action.mutate({ lane: row.lane, action: running ? 'stop' : 'start' });
            }}
          >
            {running ? 'Stop' : 'Start'}
          </Button>
        ) : null}
        <Button size="sm" variant="ghost" onClick={() => onLogs(row.lane)}>
          Logs
        </Button>
      </div>
    </div>
  );
}

function LaneRow({ row }: { row: EnvLaneRow }) {
  const state = laneState[row.state as LaneState] ?? { label: row.state, tone: 'neutral' as const };
  return (
    <div className="grid gap-1 px-4 py-3 sm:px-6">
      <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-1">
        <p className="mono min-w-0 truncate text-14">{row.id}</p>
        <Status status={state} />
      </div>
      <p className="text-13 text-muted">{row.isolation ?? 'Its isolation is not reported.'}</p>
      <p className="text-12 text-faint">
        Network {row.network}
        {row.base_image ? `, built on ${row.base_image}` : ''}
        {row.last_used ? `, last used ${timeAgo(row.last_used)}` : ''}
      </p>
      {row.quarantine_reason ? <p className="text-13 text-red">{row.quarantine_reason}</p> : null}
    </div>
  );
}

/**
 * What this machine runs for agents: services that stay up, the terminal environments they work in,
 * and the images those are built from. Starting and stopping a service is the owner's, in person.
 */
export function EnvironmentsTab({ org, node, owner }: { org: string; node: string; owner: boolean }) {
  const envs = useNodeEnvironments(org, node);
  const [technical] = useTechnical();
  const [logsOf, setLogsOf] = useState<string | null>(null);
  const logs = useServiceLogs(org, node, logsOf);

  if (envs.isPending) return <Skeleton className="h-96 rounded-xl" />;
  if (envs.isError) return <ErrorState error={envs.error} onRetry={() => void envs.refetch()} />;
  if (envs.data === null) return <NotReported node={node} what="its environments" />;
  const data = envs.data;

  return (
    <div className="grid gap-10">
      <section aria-label="Environments" className="grid gap-3">
        <p className="text-14" aria-live="polite">
          {environmentsAnswer(data)}
        </p>
        <Panel title="Services" description="A command that stays up, restarted when it crashes. Its log is read when you open it, and is not kept here.">
          {data.services.length ? (
            <Card className="divide-y divide-line overflow-hidden">
              {data.services.map((row) => (
                <ServiceRow key={row.lane} org={org} node={node} row={row} owner={owner} onLogs={setLogsOf} />
              ))}
            </Card>
          ) : (
            <Card className="p-4 text-13 text-muted sm:p-6">No services yet. One is made on the machine, in a terminal, with a service preset.</Card>
          )}
        </Panel>
      </section>

      <Panel title="Environments" description="Where an agent works on this machine, each isolated the way it says.">
        {data.lanes.length ? (
          <Card className="divide-y divide-line overflow-hidden">
            {data.lanes.map((row) => (
              <LaneRow key={row.id} row={row} />
            ))}
          </Card>
        ) : (
          <Card className="p-4 text-13 text-muted sm:p-6">None yet.</Card>
        )}
      </Panel>

      <Panel title="Images" description="The base an environment starts from. Building a new version changes every environment made on it after.">
        <div className="grid gap-3">
          {data.images.templates.map((template) => (
            <Card key={template.name} className="grid gap-1 p-4 sm:p-6">
              <p className="mono text-14">{template.name}</p>
              <p className="text-13 text-muted">{template.description ? plainReason(template.description) : 'No description.'}</p>
            </Card>
          ))}
          {data.images.built.length ? (
            <Card className="divide-y divide-line overflow-hidden">
              {data.images.built.map((image) => (
                <div key={image.ref} className="flex flex-wrap items-center justify-between gap-x-6 gap-y-1 px-4 py-3 sm:px-6">
                  <p className="mono text-14">{image.ref}</p>
                  <p className="tabular text-12 text-faint" title={image.built_at ? dateTime(image.built_at) : undefined}>
                    Built {image.built_at ? timeAgo(image.built_at) : 'at an unknown time'}
                  </p>
                </div>
              ))}
            </Card>
          ) : null}
        </div>
        {owner ? (
          <div className="grid gap-2">
            <p className="text-13 text-muted">Making or removing an environment, and building an image, is done on the machine, in a terminal.</p>
            {technical ? (
              <Card className="grid gap-2 p-4 sm:p-6">
                {COMMANDS.map((command) => (
                  <code key={command} className="mono text-13 break-words text-fg">
                    {command}
                  </code>
                ))}
              </Card>
            ) : null}
          </div>
        ) : null}
      </Panel>

      <Dialog
        open={logsOf !== null}
        onOpenChange={(open) => {
          if (!open) setLogsOf(null);
        }}
        title={logsOf ? `Logs of ${logsOf}` : 'Logs'}
        description="Read when you open them. Nothing here keeps them."
        footer={
          <Button size="sm" variant="secondary" loading={logs.isFetching} onClick={() => void logs.refetch()}>
            Read again
          </Button>
        }
      >
        {logs.isPending ? (
          <Skeleton className="h-48 rounded-xl" />
        ) : logs.isError ? (
          <ErrorState error={logs.error} onRetry={() => void logs.refetch()} />
        ) : (
          <pre className="mono max-h-[60vh] overflow-auto rounded-lg bg-subtle p-4 text-12 break-words whitespace-pre-wrap text-muted">
            {logs.data.log || 'No output yet.'}
          </pre>
        )}
      </Dialog>
    </div>
  );
}
