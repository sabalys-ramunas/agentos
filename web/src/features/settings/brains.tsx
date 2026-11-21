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
    <dl className="mono grid min-w-0 gap-1 text-code-caption text-faint">
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
          <Button asChild variant="primary">
            <Link to="/$org/settings/brains/add" params={{ org }}>
              <Plus className="size-4" />
              <span className="max-sm:sr-only">Add an AI service</span>
              <span className="sm:hidden">Add</span>
            </Link>
          </Button>
        }
      />
      <QueryView
        query={brains}
        card
        skeleton={
          <div className="grid gap-4 sm:grid-cols-2" role="status" aria-label="Loading">
            {[0, 1, 2, 3].map((index) => (
              <Skeleton key={index} className="h-36 rounded-xl" />
            ))}
          </div>
        }
      >
        {(items) =>
          items.length ? (
            <ol className="grid gap-4 sm:grid-cols-2">
              {items.map((brain, index) => (
                <li key={brain.name}>
                  <Card className="flex h-full flex-col gap-3 p-5">
                    <div className="flex items-start gap-3">
                      <ServiceMark brain={brain} />
                      <div className="min-w-0 flex-1">
                        <p className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
                          <span className="truncate text-card-title">{serviceName(brain)}</span>
                          <StateBadge state={STATE[brain.state]} label={brainState[brain.state].label} className="shrink-0" />
                        </p>
                        <p className="mt-1 text-secondary text-pretty text-muted">{serviceSentence(brain, workers)}</p>
                      </div>
                    </div>
                    <div className="mt-auto grid gap-2">
                      {brain.computer ? (
                        <p className="flex items-center gap-2 text-secondary text-faint">
                          <Lock aria-hidden="true" className="size-3 shrink-0" />
                          Kept on {computerName(brain.computer)}
                        </p>
                      ) : null}
                      {brain.window_used_pct !== null ? (
                        <div className="flex items-center gap-3">
                          <Meter value={brain.window_used_pct} max={100} label={`${serviceName(brain)} usage window`} className="min-w-0 flex-1" />
                          <span className="tabular shrink-0 text-caption text-muted">{brain.window_used_pct}% of this window used</span>
                        </div>
                      ) : null}
                    </div>
                    {technical ? (
                      <div className="flex items-end justify-between gap-2 border-t border-line pt-3">
                        <Models brain={brain} />
                        <div className="flex shrink-0">
                          <IconButton label={`Move ${brain.name} up`} size="sm" disabled={index === 0} onClick={() => move(items, index, -1)}>
                            <ArrowUp className="size-4" />
                          </IconButton>
                          <IconButton label={`Move ${brain.name} down`} size="sm" disabled={index === items.length - 1} onClick={() => move(items, index, 1)}>
                            <ArrowDown className="size-4" />
                          </IconButton>
                        </div>
                      </div>
                    ) : null}
                  </Card>
                </li>
              ))}
            </ol>
          ) : (
            <Card>
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
            </Card>
          )
        }
      </QueryView>
      <p className="mt-3 flex items-start gap-2 text-secondary text-pretty text-muted">
        <span className="grid h-5 shrink-0 place-items-center text-faint"><Lock aria-hidden="true" className="size-3" /></span>
        Each one lives on a computer of yours, and openajar never holds a key.{technical ? ' Your agents try these from the top down.' : ''}
      </p>
      {access.can('brains.tasks') ? (
        <div className="mt-12">
          <HandedToModels org={org} />
        </div>
      ) : null}
    </>
  );
}
