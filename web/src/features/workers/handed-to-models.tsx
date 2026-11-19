/**
 * What agents and workers handed to an AI model, live: each task's state in words, and for the
 * owner, stopping one that is still going and carrying on from one that stopped. The list is read
 * again on its own while any of them is working (`useBrainTasks`).
 */
import { useState } from 'react';
import { useBrains, useBrainTask, useCancelBrainTask, useResumeBrainTask, useBrainTasks, useWorkspaces } from '@/api/hooks';
import type { BrainTask } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Card, Section } from '@/components/ui/card';
import { ConfirmDialog } from '@/components/ui/dialog';
import { StateBadge } from '@/components/ui/state-badge';
import { EmptyState, QueryView, Skeleton } from '@/components/ui/states';
import { timeAgo, usd } from '@/lib/format';
import { useTechnical } from '@/lib/technical';
import { canResume, canStop, TASK_WORDS } from './handed-words';
import { serviceName } from './words';

function TechnicalLine({ org, task }: { org: string; task: BrainTask }) {
  const [open, setOpen] = useState(false);
  const detail = useBrainTask(org, task.id, open);
  return (
    <div className="grid gap-2">
      <p className="tabular text-caption text-faint">
        Attempt {task.attempt} · {task.turns} {task.turns === 1 ? 'turn' : 'turns'} · {task.tokens.toLocaleString('en')} tokens
        {task.cost_usd !== null ? ` · ${usd(task.cost_usd)}` : ''}
        {task.status_reason ? ` · ${task.status_reason}` : ''}
      </p>
      {open ? (
        detail.isPending ? (
          <Skeleton className="h-16 w-full" />
        ) : detail.isError ? (
          <p className="text-secondary text-muted">Could not read what it said. Try again in a moment.</p>
        ) : detail.data.transcript_tail.length ? (
          <pre className="mono max-h-48 overflow-auto rounded-lg border border-line bg-inset p-3 text-caption whitespace-pre-wrap text-muted">
            {detail.data.transcript_tail.slice(-12).join('\n')}
          </pre>
        ) : (
          <p className="text-secondary text-muted">Nothing was written down yet.</p>
        )
      ) : null}
      <div>
        <Button size="sm" variant="ghost" aria-expanded={open} onClick={() => setOpen((value) => !value)}>
          {open ? 'Hide what it said' : 'Show what it said'}
        </Button>
      </div>
    </div>
  );
}

export function HandedToModels({ org }: { org: string }) {
  const tasks = useBrainTasks(org);
  const brains = useBrains(org);
  const workspaces = useWorkspaces(org);
  const cancel = useCancelBrainTask(org);
  const resume = useResumeBrainTask(org);
  const [technical] = useTechnical();
  const [stopping, setStopping] = useState<BrainTask | null>(null);

  const nameOf = (task: BrainTask) => {
    const business = workspaces.data?.find((item) => item.id === task.workspace);
    return business?.name ?? 'Your agents';
  };
  const serviceOf = (task: BrainTask) => {
    if (!task.brain) return 'No AI model yet';
    const brain = brains.data?.find((item) => item.name === task.brain);
    return brain ? serviceName(brain) : 'An AI model';
  };

  return (
    <Section
      title="Handed to an AI model"
      description="What agents and workers gave a model to do. Stop one that is still going, or carry on from one that stopped."
    >
      <QueryView
        query={tasks}
        card
        skeleton={
          <div role="status" aria-label="Loading" className="grid gap-4 rounded-xl border border-line p-6">
            <Skeleton />
            <Skeleton className="w-2/3" />
          </div>
        }
      >
        {(rows) =>
          rows.length === 0 ? (
            <Card>
              <EmptyState
                title="Nothing has been handed to a model"
                description="When an agent or a worker gives an AI model a job, it shows up here with its state. You can stop it, or carry on from where it stopped."
              />
            </Card>
          ) : (
            <Card>
              <ol className="divide-y divide-line">
                {rows.map((task) => {
                  const word = TASK_WORDS[task.status];
                  return (
                    <li key={task.id} className="grid gap-3 px-4 py-4 sm:px-6">
                      <div className="flex min-w-0 items-start justify-between gap-4">
                        <div className="min-w-0">
                          <p className="line-clamp-2 text-body">{task.summary || 'A task with no words'}</p>
                          <p className="mt-1 truncate text-secondary text-faint">
                            {nameOf(task)} · {serviceOf(task)}
                            {task.started_at ? ` · started ${timeAgo(task.started_at)}` : ''}
                          </p>
                        </div>
                        <StateBadge state={word.state} label={word.label} sentence={word.sentence} className="shrink-0" />
                      </div>
                      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
                        <p className="min-w-0 text-secondary text-muted">{word.sentence}</p>
                        <div className="flex shrink-0 gap-2">
                          {canStop(task) ? (
                            <Button size="sm" variant="danger" onClick={() => setStopping(task)}>
                              Stop
                            </Button>
                          ) : null}
                          {canResume(task) ? (
                            <Button size="sm" loading={resume.isPending && resume.variables === task.id} onClick={() => resume.mutate(task.id)}>
                              Carry on
                            </Button>
                          ) : null}
                        </div>
                      </div>
                      {technical ? <TechnicalLine org={org} task={task} /> : null}
                    </li>
                  );
                })}
              </ol>
            </Card>
          )
        }
      </QueryView>
      <ConfirmDialog
        open={stopping !== null}
        onOpenChange={(open) => {
          if (!open) setStopping(null);
        }}
        title="Stop this task?"
        description="The model stops where it is. What it has already done stays done, and nothing else is started."
        confirmLabel="Stop task"
        danger
        loading={cancel.isPending}
        onConfirm={async () => {
          if (stopping) await cancel.mutateAsync(stopping.id);
          setStopping(null);
        }}
      />
    </Section>
  );
}
