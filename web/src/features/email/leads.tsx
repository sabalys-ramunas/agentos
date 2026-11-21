/**
 * Leads: what an agent has proposed, and the lists a person has made. A batch waits as a sheet of dots,
 * so its size is seen before anything is read. Concept stage: answered by the mock only.
 */
import { Link, useParams } from '@tanstack/react-router';
import { ArrowRight, Users } from 'lucide-react';
import { useLeadBatches, useMailboxes, useMailLists, useWorkspaces } from '@/api/hooks';
import { isNotServed } from '@/api/http';
import type { LeadBatch, MailList, Workspace } from '@/api/types';
import { BusinessMark } from '@/components/brand/identity';
import { SiteMark } from '@/components/brand/site-mark';
import { Card } from '@/components/ui/card';
import { PageHeader } from '@/components/ui/misc';
import { EmptyState, ErrorState, NotLive, Skeleton } from '@/components/ui/states';
import { plural } from '@/lib/format';
import { EmailTabs } from './shared';
import { agoWords } from './words';

function Dots({ batch }: { batch: LeadBatch }) {
  return (
    <span aria-hidden="true" className="grid w-max grid-cols-[repeat(13,8px)] gap-1">
      {batch.leads.map((lead) => (
        <span key={lead.id} className="size-2 rounded-full bg-fg data-[look]:bg-(--wait-dot)" data-look={lead.look ? '' : undefined} />
      ))}
      {(batch.suppressed ?? []).map((lead) => (
        <span key={lead.id} className="size-2 rounded-full shadow-[inset_0_0_0_1px_var(--control)]" />
      ))}
    </span>
  );
}

function Waiting({ org, batch, workspace }: { org: string; batch: LeadBatch; workspace: Workspace | undefined }) {
  const looks = batch.leads.filter((lead) => lead.look).length;
  return (
    <Link to="/$org/email/leads/$batch" params={{ org, batch: batch.id }} className="picture-stage group mt-8 grid items-center gap-6 rounded-(--radius-card) border border-line p-6 transition-colors duration-(--duration-fast) ease-standard hover:border-line-strong md:grid-cols-[auto_minmax(0,1fr)_auto]">
      <Dots batch={batch} />
      <span className="min-w-0">
        <span className="flex items-center gap-2 text-caption text-faint">
          {workspace ? <BusinessMark name={workspace.name} color={workspace.color} size={14} /> : null}
          Found by {batch.by}, {agoWords(batch.at)}
        </span>
        <span className="mt-1 block text-heading text-balance">{batch.found} people for {batch.list_name}</span>
        <span className="mt-1 block text-body text-pretty text-muted">{plural(batch.leads.length, 'is', 'are')} yours to decide, {looks} worth a look. Nobody gets an email until you have looked.</span>
      </span>
      <span className="flex items-center gap-1 text-label">
        Look and decide
        <ArrowRight aria-hidden="true" className="size-4 transition-transform duration-(--duration-fast) ease-standard group-hover:translate-x-1" />
      </span>
    </Link>
  );
}

function ListRow({ org, list, workspace }: { org: string; list: MailList; workspace: Workspace | undefined }) {
  // The lists page has no people or days from the node: only the size, and the picture when there is one.
  const people = list.people ?? [];
  const replied = people.filter((person) => person.state === 'replied').length;
  const out = people.filter((person) => person.state === 'unsubscribed').length;
  const reached = people.filter((person) => person.state !== 'planned' && person.state !== null).length;
  const shape = (list.days ?? []).slice(-8);
  const most = Math.max(1, ...shape.map((day) => day.first + day.follow));
  return (
    <li>
      <Link to="/$org/email/lists/$list" params={{ org, list: list.id }} className="group grid items-center gap-x-6 gap-y-1 px-4 py-3 transition-colors duration-(--duration-fast) ease-standard hover:bg-hover md:grid-cols-[minmax(0,1fr)_7rem_16rem_auto]">
        <span className="min-w-0">
          <span className="block truncate text-label">{list.name}</span>
          <span className="flex items-center gap-1 text-caption text-faint">
            {workspace ? <BusinessMark name={workspace.name} color={workspace.color} size={14} /> : null}
            <span className="truncate">{workspace?.name}{list.address ? ` · ${list.address}` : ''}</span>
          </span>
        </span>
        {shape.length ? (
          <span aria-hidden="true" className="hidden h-6 items-end gap-px md:flex">
            {shape.map((day) => <span key={day.at} className="w-2 rounded-t-[2px] bg-fg" style={{ height: `${Math.max(2, ((day.first + day.follow) / most) * 24)}px` }} />)}
          </span>
        ) : <span aria-hidden="true" className="hidden md:block" />}
        <span className="tabular text-secondary text-muted">{list.people === null ? plural(list.size, 'person', 'people') + ' on this list' : reached ? `${reached} written to, ${plural(replied, 'reply', 'replies')}, ${out} out` : `${plural(people.length, 'person', 'people')}, nothing sent yet`}</span>
        <ArrowRight aria-hidden="true" className="hidden size-4 text-faint transition-transform duration-(--duration-fast) ease-standard group-hover:translate-x-1 md:block" />
      </Link>
    </li>
  );
}

export function Leads() {
  const { org } = useParams({ from: '/$org/email/leads' });
  const batches = useLeadBatches(org);
  const lists = useMailLists(org);
  const fresh = useMailboxes(org).data?.reduce((sum, item) => sum + (item.new_count ?? 0), 0);
  const workspaces = useWorkspaces(org).data;
  const business = (id: string) => workspaces?.find((item) => item.id === id);

  if (isNotServed(batches.error)) return <Card><NotLive /></Card>;
  if (batches.error) return <ErrorState error={batches.error} onRetry={() => void batches.refetch()} />;
  if (!batches.data || !lists.data) {
    return (
      <div className="grid gap-8">
        <Skeleton className="h-24 max-w-2xl rounded-lg" />
        <Skeleton className="h-36 rounded-(--radius-card)" />
        <Skeleton className="h-40 rounded-(--radius-card)" />
      </div>
    );
  }

  const waiting = batches.data.filter((item) => item.status === 'waiting');
  const running = lists.data;
  if (!waiting.length && !running.length) {
    return (
      <div>
        <PageHeader title="Email" description="Leads: people an agent found for you to look at." />
        <Card>
          <EmptyState
            icon={<Users />}
            title="Nobody here yet"
            description="Ask an agent to find people on a site you use. What it finds waits here for you to look at, and nobody is written to until you say so."
          />
        </Card>
      </div>
    );
  }

  return (
    <div className="pb-16">
      <PageHeader
        title="Email"
        description={`${waiting.length ? `${plural(waiting.length, 'batch is', 'batches are')} waiting for a look. ` : ''}${plural(running.length, 'list')} running.`}
      />
      <EmailTabs org={org} here="leads" newCount={fresh} waiting={waiting.length} />
      {waiting.map((batch) => <Waiting key={batch.id} org={org} batch={batch} workspace={business(batch.workspace)} />)}
      {running.length ? (
        <section aria-labelledby="lists" className="mt-8">
          <h2 id="lists" className="mb-3 text-section-title">Your lists</h2>
          <ul className="divide-y divide-line overflow-hidden rounded-(--radius-card) border border-line bg-card">
            {running.map((list) => <ListRow key={list.id} org={org} list={list} workspace={business(list.workspace)} />)}
          </ul>
        </section>
      ) : null}
      <p className="mt-6 flex items-center gap-2 text-secondary text-muted">
        <SiteMark site="linkedin" size={14} label={false} />
        <span className="min-w-0">Agents find people at your account's own pace, and a site's challenge stops them. Only a person adds anyone to a list.</span>
      </p>
    </div>
  );
}
