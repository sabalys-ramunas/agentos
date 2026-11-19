/**
 * One business: its accounts, and for the owner what is still to log in, each with the place to do it.
 * The list of accounts is read for anyone who sees the business. The queue is the owner's alone.
 */
import { Link, useParams } from '@tanstack/react-router';
import { useAccess, useWorkspace, useWorkspaceQueue } from '@/api/hooks';
import type { Lane, QueueLane, Workspace } from '@/api/types';
import { ApiError } from '@/api/http';
import { Button } from '@/components/ui/button';
import { Card, Section } from '@/components/ui/card';
import { PageHeader } from '@/components/ui/misc';
import { StateBadge } from '@/components/ui/state-badge';
import { Status } from '@/components/ui/status';
import { EmptyState, QueryView, Skeleton } from '@/components/ui/states';
import { safeUrl } from '@/lib/format';
import { laneName } from '@/lib/labels';
import { useMe } from '@/lib/use-principal';
import { accountName, accountCondition } from './words';
import { queueSentence, READINESS_WORDS } from './workspace-words';

function AccountRow({ org, lane }: { org: string; lane: Lane }) {
  const me = useMe();
  const condition = accountCondition(lane, me);
  return (
    <li className="flex items-center justify-between gap-4 px-4 py-3 sm:px-6">
      <Link to="/$org/lanes/$ws/$name" params={{ org, ws: lane.workspace, name: lane.name }} className="min-w-0 truncate text-body hover:underline">
        {accountName(lane)}
      </Link>
      <StateBadge state={condition.state} label={condition.label} sentence={condition.sentence} className="shrink-0" />
    </li>
  );
}

function QueueRow({ org, item }: { org: string; item: QueueLane }) {
  // The queue only holds what is not ready, so a row is one of the two waiting words.
  const word = item.readiness === 'needs_attention' ? READINESS_WORDS.needs_attention : READINESS_WORDS.needs_login;
  const site = safeUrl(item.login_url);
  return (
    <li className="grid gap-2 px-4 py-3 sm:px-6">
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1">
        <Link to="/$org/lanes/$ws/$name" params={{ org, ws: item.workspace, name: item.name }} className="min-w-0 truncate text-body hover:underline">
          {laneName(item.name)}
        </Link>
        <Status status={word} />
      </div>
      {item.note ? <p className="text-secondary text-muted">{item.note}</p> : null}
      <div className="flex flex-wrap gap-2">
        <Button size="sm" asChild>
          <Link to="/$org/lanes/$ws/$name" params={{ org, ws: item.workspace, name: item.name }}>
            Open the account
          </Link>
        </Button>
        {site ? (
          <Button size="sm" variant="ghost" asChild>
            <a href={site} target="_blank" rel="noreferrer">
              Open the site
            </a>
          </Button>
        ) : null}
      </div>
    </li>
  );
}

function Queue({ org, ws }: { org: string; ws: string }) {
  const queue = useWorkspaceQueue(org, ws);
  return (
    <Section title="Still to log in" description={queue.data ? queueSentence(queue.data) : undefined}>
      <QueryView
        query={queue}
        card
        skeleton={
          <div role="status" aria-label="Loading" className="grid gap-3 rounded-xl border border-line p-6">
            <Skeleton />
            <Skeleton className="w-2/3" />
          </div>
        }
      >
        {(data) =>
          data.lanes.length === 0 ? (
            <Card>
              <EmptyState title="Every account here is ready" description="Nothing is waiting for a login in this business." />
            </Card>
          ) : (
            <Card>
              <ol className="divide-y divide-line">
                {data.lanes.map((item) => (
                  <QueueRow key={item.id} org={org} item={item} />
                ))}
              </ol>
            </Card>
          )
        }
      </QueryView>
    </Section>
  );
}

function Accounts({ org, business }: { org: string; business: Workspace }) {
  return (
    <Section title="Accounts">
      {business.lanes.length === 0 ? (
        <Card>
          <EmptyState title="No accounts here yet" description="An account is one login on a site. Add one, and your agents can work on it." />
        </Card>
      ) : (
        <Card>
          <ol className="divide-y divide-line">
            {business.lanes.map((lane) => (
              <AccountRow key={lane.id} org={org} lane={lane} />
            ))}
          </ol>
        </Card>
      )}
    </Section>
  );
}

export function WorkspacePage() {
  const { org, ws } = useParams({ from: '/$org/workspaces/$ws' });
  const access = useAccess(org);
  const business = useWorkspace(org, ws);
  const owner = access.can('workspaces.queue');

  if (business.isError && business.error instanceof ApiError && business.error.status === 404) {
    return (
      <EmptyState
        title="There is no business with that name here"
        description="It may have been renamed or removed. Accounts lists every business on this computer."
        action={
          <Button asChild>
            <Link to="/$org/accounts" params={{ org }}>
              Back to Accounts
            </Link>
          </Button>
        }
      />
    );
  }

  return (
    <QueryView
      query={business}
      skeleton={
        <div role="status" aria-label="Loading" className="grid gap-4">
          <Skeleton className="h-8 w-64" />
          <Skeleton className="h-4 w-96" />
        </div>
      }
    >
      {(data) => (
        <>
          <PageHeader
            title={data.name}
            description={`${data.lanes.length} ${data.lanes.length === 1 ? 'account' : 'accounts'} in this business.`}
            actions={
              <Button asChild>
                <Link to="/$org/accounts" params={{ org }}>
                  All accounts
                </Link>
              </Button>
            }
          />
          <div className="grid gap-12">
            {owner ? <Queue org={org} ws={ws} /> : null}
            <Accounts org={org} business={data} />
          </div>
        </>
      )}
    </QueryView>
  );
}

