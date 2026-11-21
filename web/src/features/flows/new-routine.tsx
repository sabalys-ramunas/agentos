/**
 * Starting a routine: a job a person recognises, not a blank canvas. Each card shows the job's tile, its
 * name, the job as a sentence and who it is for, and one click opens it ready to go.
 */
import { Link, useNavigate, useParams, useSearch } from '@tanstack/react-router';
import { ArrowLeft, ArrowRight, Camera, Euro, Mail, Package, Plus, Sheet, ShieldCheck, Users, Webhook } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { useAccess, useMailboxes, useNodes, useRoutineCatalog, useWorkspaces } from '@/api/hooks';
import type { RoutineTemplate } from '@/api/types';
import { BusinessMark } from '@/components/brand/identity';
import { usePageCrumb } from '@/components/shell/crumb';
import { Button } from '@/components/ui/button';
import { Card, cardLink } from '@/components/ui/card';
import { PageHeader } from '@/components/ui/misc';
import { AppTile } from '@/components/ui/tile';
import type { TileTone } from '@/components/ui/tile';
import { Select } from '@/components/ui/select';
import { NoAccess, QueryView, Skeleton } from '@/components/ui/states';
import { useActiveWorkspace } from '@/lib/active-workspace';
import { cn } from '@/lib/cn';

/** Each starting job wears the wrapper tile of what it does, as drawn. */
const TEMPLATE_TILE: Record<RoutineTemplate['icon'], { icon: LucideIcon; tone: TileTone }> = {
  order: { icon: Package, tone: 'amber' },
  invoice: { icon: Euro, tone: 'green' },
  newsletter: { icon: Mail, tone: 'blue' },
  outreach: { icon: Users, tone: 'teal' },
  post: { icon: Camera, tone: 'pink' },
  webhook: { icon: Webhook, tone: 'ink' },
  new_row: { icon: Sheet, tone: 'violet' },
};

function TemplateCard({ org, template, workspace, noMailbox }: { org: string; template: RoutineTemplate; workspace: string | null; noMailbox: boolean }) {
  const needsMail = template.needs?.includes('mailbox') === true;
  const tile = TEMPLATE_TILE[template.icon];
  return (
    <Link
      to="/$org/flows/new/$template"
      params={{ org, template: template.id }}
      search={{ workspace: workspace ?? undefined }}
      aria-label={`${template.name}: ${template.sentence}`}
      className={cn(cardLink, 'group/job flex flex-col gap-3 p-5')}
    >
      <AppTile tone={tile.tone} icon={tile.icon} />
      <span className="grid gap-1">
        <span className="flex items-center justify-between gap-2 text-section-title">
          {template.name}
          <ArrowRight aria-hidden="true" className="size-4 text-faint opacity-0 transition-opacity duration-(--duration-fast) ease-standard group-hover/job:opacity-100 group-focus-visible/job:opacity-100" />
        </span>
        <span className="text-body text-pretty text-fg">{template.sentence}</span>
      </span>
      <span className="mt-auto text-secondary text-pretty text-muted">{template.note}</span>
      {needsMail && noMailbox ? <span className="text-secondary text-warn">Needs a mailbox first. Connect one in Email.</span> : null}
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
  usePageCrumb('New routine', { mono: false });

  if (!canOperate) {
    return (
      <Card>
        <NoAccess />
      </Card>
    );
  }

  const chosen = workspaces.find((item) => item.id === workspace);

  return (
    <>
      <Link to="/$org/flows" params={{ org }} className="mb-6 inline-flex items-center gap-1 rounded-sm text-secondary text-muted hover:text-fg">
        <ArrowLeft className="size-3.5" /> Routines
      </Link>
      <PageHeader
        eyebrow={
          chosen ? (
            <span className="inline-flex items-center gap-2 text-body text-muted">
              <BusinessMark name={chosen.name} color={chosen.color} size={20} />
              {chosen.name}
            </span>
          ) : undefined
        }
        title="What should happen on its own?"
        description="Pick a job you already do. It opens ready to go, and you can change any part of it."
        actions={
          options.length > 1 && workspace ? (
            <Select aria-label="For which business" value={workspace} onValueChange={(next) => void navigate({ search: { workspace: next }, replace: true })} options={options} className="md:w-56" />
          ) : undefined
        }
      />
      <div className="grid gap-6">
        {nodes.length === 0 ? <p className="text-body text-muted">Connect a computer to run these. You can still look around.</p> : null}
        <QueryView
          query={catalog}
          card
          skeleton={
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {Array.from({ length: 6 }, (_, index) => (
                <Skeleton key={index} className="h-56 rounded-xl" />
              ))}
            </div>
          }
        >
          {(data) => (
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {data.templates.map((template) => (
                <TemplateCard key={template.id} org={org} template={template} workspace={workspace} noMailbox={noMailbox} />
              ))}
            </div>
          )}
        </QueryView>
        <p className="flex items-start gap-3 rounded-xl border border-line bg-inset px-5 py-4 text-body text-pretty text-fg">
          <ShieldCheck aria-hidden="true" className="mt-1 size-4 shrink-0 text-muted" />
          Anything that emails people stops at a daily limit, skips anyone who asked not to hear from you, and only goes to a list you gave it.
        </p>
        <div>
          <Button asChild>
            <Link to="/$org/flows/new/$template" params={{ org, template: 'blank' }} search={{ workspace: workspace ?? undefined }}>
              <Plus aria-hidden="true" className="size-4" />
              Start from nothing
            </Link>
          </Button>
        </div>
      </div>
    </>
  );
}
