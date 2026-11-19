/**
 * AI models: who thinks. Each row is one AI service, with its models under it, and it lives on a
 * computer of the person's own. Model names, the try order and plan details wait for Technical
 * details. There is no field here for a key: adding one hands over to the computer.
 */
import { Link, useParams } from '@tanstack/react-router';
import { ArrowDown, ArrowUp, Brain as BrainIcon, Cpu, Lock, Plus } from 'lucide-react';
import { useAccess, useBrains, useSetBrainOrder, useWorkers } from '@/api/hooks';
import type { Brain } from '@/api/types';
import { AgentMark } from '@/components/brand/identity';
import { Button, IconButton } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Meter } from '@/components/ui/misc';
import { StateBadge } from '@/components/ui/state-badge';
import { EmptyState, QueryView, Skeleton } from '@/components/ui/states';
import { computerName } from '@/lib/computers';
import { brainState } from '@/lib/labels';
import type { PersonState } from '@/lib/states';
import { useTechnical } from '@/lib/technical';
import { HandedToModels } from '@/features/workers/handed-to-models';
import { modelsAnswer, serviceName, serviceSentence } from '@/features/workers/words';
import { SettingsHeader } from './page';

const STATE: Record<Brain['state'], PersonState> = { ready: 'good', cooling_down: 'sleeping', needs_auth: 'waiting', down: 'stuck' };
const KIND: Record<Brain['kind'], string> = { subscription: 'Subscription', local: 'Local', metered: 'Metered' };

function ServiceMark({ brain }: { brain: Brain }) {
  if (brain.provider === 'anthropic') return <AgentMark principal={{ kind: 'agent', id: 'claude', thread: null }} size={32} />;
  return (
    <span aria-hidden="true" className="grid size-8 shrink-0 place-items-center rounded-lg border border-line bg-inset text-secondary font-medium text-muted">
      {brain.kind === 'local' ? <Cpu className="size-4" /> : serviceName(brain).charAt(0)}
    </span>
  );
}

function Models({ brain }: { brain: Brain }) {
  return (
    <dl className="mono mt-2 grid gap-1 text-caption text-faint">
      <div className="flex flex-wrap gap-x-2">
        <dt>{brain.name}</dt>
        <dd>
          {KIND[brain.kind]} · {brain.provider}
          {brain.seats ? ` · ${brain.seats.in_use} of ${brain.seats.total} seats in use` : ''}
        </dd>
      </div>
      {brain.models?.map((model) => (
        <div key={model.id} className="flex flex-wrap gap-x-2">
          <dt className="text-muted">{model.id}</dt>
          <dd>
            {model.can.join(', ')}
            {model.usd_per_step ? ` · $${model.usd_per_step.toFixed(4)} a step` : ''}
          </dd>
        </div>
      ))}
    </dl>
  );
}

export function BrainsSettings() {
  const { org } = useParams({ from: '/$org/settings/brains' });
  const brains = useBrains(org);
  const workers = useWorkers(org).data;
  const setOrder = useSetBrainOrder(org);
  const [technical] = useTechnical();
  const access = useAccess(org);

  const move = (items: Brain[], index: number, delta: number) => {
    const order = items.map((item) => item.name);
    const [name] = order.splice(index, 1);
    if (name) order.splice(index + delta, 0, name);
    setOrder.mutate(order);
  };

  return (
    <>
      <SettingsHeader
        title="AI models"
        description={brains.data?.length ? modelsAnswer(brains.data) : 'What thinks for your agents and your workers.'}
        action={
          <Button asChild icon={<Plus className="size-4" />}>
            <Link to="/$org/settings/brains/add" params={{ org }}>
              <span className="max-sm:sr-only">Add an AI service</span>
              <span className="sm:hidden">Add</span>
            </Link>
          </Button>
        }
      />
      <Card>
        <QueryView
          query={brains}
          skeleton={
            <div className="grid gap-4 p-6">
              <Skeleton />
              <Skeleton />
              <Skeleton />
            </div>
          }
        >
          {(items) =>
            items.length ? (
              <ol className="divide-y divide-line">
                {items.map((brain, index) => (
                  <li key={brain.name} className="flex items-start gap-3 px-4 py-4 sm:gap-4 sm:px-6">
                    <ServiceMark brain={brain} />
                    <div className="min-w-0 flex-1">
                      <div className="flex min-w-0 items-center justify-between gap-4">
                        <p className="truncate text-label">{serviceName(brain)}</p>
                        <StateBadge state={STATE[brain.state]} label={brainState[brain.state].label} className="shrink-0" />
                      </div>
                      <p className="mt-1 max-w-xl text-secondary text-pretty text-muted">{serviceSentence(brain, workers)}</p>
                      <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-secondary text-faint">
                        {brain.computer ? <span>Kept on {computerName(brain.computer)}</span> : null}
                        {brain.window_used_pct !== null ? (
                          <span className="flex items-center gap-2">
                            <Meter value={brain.window_used_pct} max={100} label={`${serviceName(brain)} usage window`} className="w-24 shrink-0" />
                            <span className="tabular">{brain.window_used_pct}% of this window</span>
                          </span>
                        ) : null}
                      </div>
                      {technical ? <Models brain={brain} /> : null}
                    </div>
                    {technical ? (
                      <div className="flex shrink-0">
                        <IconButton label={`Move ${brain.name} up`} size="sm" disabled={index === 0} onClick={() => move(items, index, -1)}>
                          <ArrowUp className="size-4" />
                        </IconButton>
                        <IconButton label={`Move ${brain.name} down`} size="sm" disabled={index === items.length - 1} onClick={() => move(items, index, 1)}>
                          <ArrowDown className="size-4" />
                        </IconButton>
                      </div>
                    ) : null}
                  </li>
                ))}
              </ol>
            ) : (
              <EmptyState
                icon={<BrainIcon />}
                title="Nothing thinks here yet"
                description="An AI service lives on a computer of yours. Add one and your agents and workers can use it."
                action={
                  <Button asChild variant="primary">
                    <Link to="/$org/settings/brains/add" params={{ org }}>Add an AI service</Link>
                  </Button>
                }
              />
            )
          }
        </QueryView>
      </Card>
      <p className="mt-3 flex items-start gap-2 text-secondary text-pretty text-muted">
        <span className="grid h-5 shrink-0 place-items-center text-faint"><Lock aria-hidden="true" className="size-3" /></span>
        Each one lives on a computer of yours, and agentos never holds a key.{technical ? ' Your agents try these from the top down.' : ''}
      </p>
      {access.can('brains.tasks') ? (
        <div className="mt-12">
          <HandedToModels org={org} />
        </div>
      ) : null}
    </>
  );
}
