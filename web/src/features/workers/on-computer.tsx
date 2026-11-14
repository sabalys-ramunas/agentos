/**
 * A computer's Workers tab: what it runs, what it cannot run and why, and which AI services it keeps.
 * Read here, changed in Settings, so a worker has one switch per computer and one place to find it.
 */
import { Link } from '@tanstack/react-router';
import { ArrowRight, Radio } from 'lucide-react';
import { useBrains, useWorkersOnComputer } from '@/api/hooks';
import type { NodeDetail } from '@/api/types';
import { Card } from '@/components/ui/card';
import { StateBadge } from '@/components/ui/state-badge';
import { Skeleton } from '@/components/ui/states';
import { computerName } from '@/lib/computers';
import { plural } from '@/lib/format';
import { Panel } from '@/features/nodes/parts';
import { plainReason } from '@/lib/plain-node';
import { StepLineMini } from './step-line';
import { inWords, serviceName, thingLine, WORKER_NAME } from './words';

export function WorkersTab({ org, node }: { org: string; node: NodeDetail }) {
  const report = useWorkersOnComputer(org, node.id);
  const brains = useBrains(org).data ?? [];
  const data = report.data;
  if (report.isPending) return <Skeleton className="h-64 rounded-xl" />;
  if (!data) return null;
  const name = computerName(node.id);
  const keeps = data.keeps.flatMap((id) => {
    const brain = brains.find((item) => item.name === id);
    return brain ? [serviceName(brain)] : [];
  });
  const away = node.state === 'down';
  return (
    <div className="grid gap-8">
      <Panel
        title="What it runs"
        description={away ? 'This computer is offline, so nothing runs here now.' : undefined}
        action={
          <Link to="/$org/settings/workers" params={{ org }} className="inline-flex items-center gap-1 text-13 text-muted hover:text-fg">
            Workers
            <ArrowRight aria-hidden="true" className="size-3" />
          </Link>
        }
      >
        <Card>
          {data.runs ? (
            <div className="grid gap-3 px-4 py-4 sm:px-6">
              <div className="flex items-center justify-between gap-4">
                <p className="text-14 font-medium">{WORKER_NAME[data.runs.kind]}</p>
                <StateBadge state={away ? 'stuck' : data.runs.latest?.state === 'working' ? 'working' : data.runs.latest?.state === 'asked' ? 'waiting' : 'good'} label={away ? 'Offline' : undefined} />
              </div>
              {data.runs.latest ? <StepLineMini thing={data.runs.latest} /> : null}
              <p className="text-13 text-pretty text-muted">
                {data.runs.latest ? `${thingLine(data.runs.latest)} ` : ''}
                {plural(data.runs.things_today, 'thing')} done today.
              </p>
            </div>
          ) : (
            <div className="flex items-start gap-3 px-4 py-4 sm:px-6">
              <span className="grid h-5 shrink-0 place-items-center text-faint"><Radio aria-hidden="true" className="size-4" /></span>
              <p className="min-w-0 text-14 text-pretty text-muted">{name} runs no workers yet. Agents do every step here themselves.</p>
            </div>
          )}
        </Card>
      </Panel>
      {data.cannot.length ? (
        <Panel title="What it cannot run">
          <Card>
            <ul className="divide-y divide-line">
              {data.cannot.map((item) => (
                <li key={item.kind} className="grid gap-1 px-4 py-3 sm:grid-cols-[160px_minmax(0,1fr)] sm:gap-4 sm:px-6">
                  <p className="text-14 font-medium">{WORKER_NAME[item.kind]}</p>
                  <p className="text-14 text-pretty text-muted">{plainReason(item.why)}.</p>
                </li>
              ))}
            </ul>
          </Card>
        </Panel>
      ) : null}
      <Panel title="AI services kept here">
        <p className="text-14 text-pretty text-muted">
          {keeps.length ? `${inWords(keeps)}. Keys stay on this computer.` : 'None yet.'}{' '}
          <Link to="/$org/settings/brains" params={{ org }} className="link">AI models</Link>
        </p>
      </Panel>
    </div>
  );
}
