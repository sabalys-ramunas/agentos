/**
 * Starting a routine: a job a person recognises, not a blank canvas. Each card shows the job as a
 * sentence and the real sites it would use in this business, and one click opens it ready to go.
 */
import { Link, useNavigate, useParams, useSearch } from '@tanstack/react-router';
import { ArrowLeft, ArrowRight, Lock, Plus } from 'lucide-react';
import { useAccess, useMailboxes, useNodes, useRoutineCatalog, useWorkspaces } from '@/api/hooks';
import type { RoutineCatalog, RoutineStep, RoutineTemplate } from '@/api/types';
import { Card } from '@/components/ui/card';
import { Select } from '@/components/ui/select';
import { NoAccess, QueryView, Skeleton } from '@/components/ui/states';
import { useActiveWorkspace } from '@/lib/active-workspace';
import { fillTemplate, flatSteps } from '@/lib/routines';
import { StepMark, TriggerMark } from './flow-map';

function Chain({ template, catalog, workspace }: { template: RoutineTemplate; catalog: RoutineCatalog; workspace: string | null }) {
  // A picture of the job, so a site this business has not connected yet still shows what it means.
  const filled = fillTemplate(template, catalog, workspace);
  const anywhere = flatSteps(template.steps);
  const shown = flatSteps(filled.steps)
    .filter((step: RoutineStep) => step.kind !== 'branch' && step.kind !== 'loop')
    .map((step) => {
      const tag = anywhere.find((item) => item.id === step.id);
      if (step.kind !== 'action' || step.operation || tag?.kind !== 'action') return step;
      return { ...step, operation: catalog.operations.find((item) => tag.operation?.startsWith('tag:') && item.tags.includes(tag.operation.slice(4)))?.id ?? null };
    })
    .slice(0, 3);
  return (
    <div className="picture-stage flex h-32 items-center justify-center gap-0 border-b border-line">
      <TriggerMark trigger={template.trigger} size={40} />
      {shown.map((step) => (
        <span key={step.id} className="flex items-center">
          <span aria-hidden="true" className="h-px w-4 bg-(--board-wire-strong)" />
          <StepMark step={step} catalog={catalog} size={40} />
        </span>
      ))}
    </div>
  );
}

function TemplateCard({ org, template, catalog, workspace, noMailbox }: { org: string; template: RoutineTemplate; catalog: RoutineCatalog; workspace: string | null; noMailbox: boolean }) {
  const needsMail = template.needs?.includes('mailbox') === true;
  return (
    <Link
      to="/$org/flows/new/$template"
      params={{ org, template: template.id }}
      search={{ workspace: workspace ?? undefined }}
      aria-label={`${template.name}: ${template.sentence}`}
      className="group/job flex flex-col overflow-hidden rounded-(--radius-card) border border-line bg-bg shadow-(--shadow-card) transition-[box-shadow,border-color,transform] duration-(--duration-settle) ease-(--ease-out) hover:-translate-y-px hover:border-line-strong hover:shadow-(--shadow-hover) motion-reduce:hover:translate-y-0"
    >
      <Chain template={template} catalog={catalog} workspace={workspace} />
      <span className="flex flex-1 flex-col p-4">
        <span className="flex items-center justify-between gap-2 text-16 font-semibold">
          {template.name}
          <ArrowRight aria-hidden="true" className="size-4 text-faint opacity-0 transition-opacity duration-(--duration-fast) ease-standard group-hover/job:opacity-100" />
        </span>
        <span className="mt-1 text-14 text-pretty text-muted">{template.sentence}</span>
        <span className="mt-auto pt-3 text-13 text-faint">{template.note}</span>
        {needsMail && noMailbox ? <span className="pt-1 text-13 text-amber">Needs a mailbox first. Connect one in Email.</span> : null}
      </span>
    </Link>
  );
}

export function NewRoutine() {
  const { org } = useParams({ from: '/$org/flows/new' });
  const search = useSearch({ from: '/$org/flows/new' });
  const navigate = useNavigate({ from: '/$org/flows/new' });
  const catalog = useRoutineCatalog(org);
  const workspaces = useWorkspaces(org).data ?? [];
  const nodes = useNodes(org).data ?? [];
  // Mail goes out from a mailbox connected on this computer. Nothing is said until the list has answered.
  const mailboxes = useMailboxes(org).data;
  const noMailbox = mailboxes !== undefined && mailboxes.length === 0;
  const canOperate = useAccess(org).can('lanes.operate');
  const [active] = useActiveWorkspace(org);
  // No workspace chosen in the top bar reads as an empty string.
  const workspace = search.workspace || active || workspaces[0]?.id || null;
  const options = workspaces.map((item) => ({ value: item.id, label: item.name }));

  if (!canOperate) {
    return (
      <Card>
        <NoAccess />
      </Card>
    );
  }

  return (
    <div className="grid gap-10">
      <div>
        <Link to="/$org/flows" params={{ org }} className="mb-6 inline-flex items-center gap-1 text-13 text-muted hover:text-fg">
          <ArrowLeft className="size-3" /> Routines
        </Link>
        <header className="grid gap-4 md:grid-cols-[minmax(0,1fr)_auto] md:items-end">
          <div className="max-w-2xl">
            <h1 className="text-32 font-semibold text-balance sm:text-[40px] sm:leading-[44px] sm:tracking-[-0.045em]">
              What should happen <span className="text-faint">on its own?</span>
            </h1>
            <p className="mt-3 text-16 text-pretty text-muted">Pick a job you already do. It opens ready to go, and you can change any part of it.</p>
          </div>
          {options.length > 1 && workspace ? (
            <Select aria-label="For which business" value={workspace} onValueChange={(next) => void navigate({ search: { workspace: next }, replace: true })} options={options} className="md:w-56" />
          ) : null}
        </header>
      </div>
      {nodes.length === 0 ? <p className="text-14 text-muted">Connect a computer to run these. You can still look around.</p> : null}
      <QueryView
        query={catalog}
        card
        skeleton={
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {Array.from({ length: 6 }, (_, index) => (
              <Skeleton key={index} className="h-64 rounded-(--radius-card)" />
            ))}
          </div>
        }
      >
        {(data) => (
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {data.templates.map((template) => (
              <TemplateCard key={template.id} org={org} template={template} catalog={data} workspace={workspace} noMailbox={noMailbox} />
            ))}
          </div>
        )}
      </QueryView>
      <div className="flex flex-col gap-4 border-t border-line pt-6 sm:flex-row sm:items-center sm:justify-between">
        <p className="flex items-start gap-2 text-13 text-pretty text-muted">
          <Lock aria-hidden="true" className="size-4 shrink-0 translate-y-0.5 text-faint" />
          Anything that emails people stops at a daily limit, skips anyone who asked not to hear from you, and only goes to a list you gave it.
        </p>
        <Link to="/$org/flows/new/$template" params={{ org, template: 'blank' }} search={{ workspace: workspace ?? undefined }} className="inline-flex shrink-0 items-center gap-1 text-14 text-fg hover:underline">
          <Plus aria-hidden="true" className="size-4" />
          Start from nothing
        </Link>
      </div>
    </div>
  );
}
