/**
 * One business (spec-16, workspace.png): its mark, its name and one line on how far along it is; for the owner, a
 * banner for each account to look at and one for those still to log in, each with the place to do it; and its
 * accounts as small cards in three columns. The list is read for anyone who sees the business; the queue is the
 * owner's alone.
 */
import { Link, useParams } from '@tanstack/react-router';
import { ExternalLink, Plus } from 'lucide-react';
import { useState } from 'react';
import { useAccess, useNodes, useWorkspace, useWorkspaceQueue, useWorkspaces } from '@/api/hooks';
import type { Lane, LoginQueue, QueueLane, Workspace } from '@/api/types';
import { ApiError, isNotServed } from '@/api/http';
import { BusinessMark } from '@/components/brand/identity';
import { SiteMark } from '@/components/brand/site-mark';
import { usePageCrumb } from '@/components/shell/crumb';
import { Button } from '@/components/ui/button';
import { Card, cardLink } from '@/components/ui/card';
import { PageHeader } from '@/components/ui/misc';
import { StateBadge } from '@/components/ui/state-badge';
import { Banner, EmptyState, ErrorState, Skeleton } from '@/components/ui/states';
import { cn } from '@/lib/cn';
import { plural, safeUrl } from '@/lib/format';
import { laneName } from '@/lib/labels';
import type { PersonState } from '@/lib/states';
import { useMe } from '@/lib/use-principal';
import { NewLaneDialog } from './new-lane-dialog';
import { accountCondition, accountName, accountTitle } from './words';
import { queueSentence } from './workspace-words';

const EDGE: Partial<Record<PersonState, string>> = { stuck: 'border-bad-line hover:border-bad-line', waiting: 'border-warn-line hover:border-warn-line' };
const GRID = 'grid gap-4 md:grid-cols-2 xl:grid-cols-3';

/** One account, small: its tile, its name, the handle (or what is wrong), and its state. The card opens it. */
function AccountTile({ org, lane }: { org: string; lane: Lane }) {
  const me = useMe();
  const condition = accountCondition(lane, me);
  const trouble = condition.state === 'stuck' || condition.state === 'waiting';
  const second = trouble ? condition.sentence : lane.account?.user_id ? accountTitle(lane) : condition.sentence;
  const name = accountName(lane);
  return (
    <li className={cn(cardLink, 'relative flex min-w-0 items-center gap-3 px-5 py-4', EDGE[condition.state])}>
      <SiteMark site={lane.site?.key ?? lane.name} size={40} tile label={false} className="rounded-lg bg-card shadow-lift" />
      <div className="min-w-0 flex-1">
        <h3 className="truncate text-card-title" title={name}>
          <Link
            to="/$org/lanes/$ws/$name"
            params={{ org, ws: lane.workspace, name: lane.name }}
            className="outline-none after:absolute after:inset-0 after:rounded-xl after:content-[''] focus-visible:after:outline-2 focus-visible:after:outline-offset-2 focus-visible:after:outline-(--focus-ring)"
          >
            {name}
          </Link>
        </h3>
        <p className="truncate text-secondary text-muted" title={second ?? undefined}>
          {second}
        </p>
      </div>
      <StateBadge chip state={condition.state} label={condition.label} sentence={condition.sentence} className="max-w-[45%] shrink-0" />
    </li>
  );
}

/** What is left to log in, as banners: each account to look at on its own, and those still to log in together. */
function Queue({ org, queue }: { org: string; queue: LoginQueue }) {
  const look = queue.lanes.filter((item) => item.readiness === 'needs_attention');
  const login = queue.lanes.filter((item) => item.readiness !== 'needs_attention');
  if (!queue.lanes.length) return null;
  return (
    <section aria-label="Still to log in" className="mb-6 grid gap-3">
      {look.map((item) => (
        <LookBanner key={item.id} org={org} item={item} />
      ))}
      {login.length ? (
        <Banner
          tone="warn"
          title={login.length === 1 ? `${laneName(login[0]!.name)} still needs a login.` : `${login.length} accounts still need a login.`}
          action={
            <Button asChild size="sm">
              <Link to="/$org/logins" params={{ org }} search={{ lane: login[0]!.id }}>
                Open the login queue
              </Link>
            </Button>
          }
        >
          {login.length === 1 ? 'Log in once, in its own browser, and the computer keeps the session.' : `${login.map((item) => laneName(item.name)).join(', ')}. Log in once each, and the computer keeps the session.`}
        </Banner>
      ) : null}
    </section>
  );
}

function LookBanner({ org, item }: { org: string; item: QueueLane }) {
  const site = safeUrl(item.login_url);
  const name = laneName(item.name);
  return (
    <Banner
      tone="bad"
      title={`${name} needs a look${item.note ? ':' : '.'}`}
      action={
        <>
          {site ? (
            <Button asChild size="sm" variant="ghost">
              <a href={site} target="_blank" rel="noreferrer">
                <ExternalLink aria-hidden="true" className="size-4" />
                Open the site
              </a>
            </Button>
          ) : null}
          <Button asChild size="sm">
            <Link to="/$org/lanes/$ws/$name" params={{ org, ws: item.workspace, name: item.name }}>
              Open the account
            </Link>
          </Button>
        </>
      }
    >
      {item.note ? `${item.note.charAt(0).toLowerCase()}${item.note.slice(1)}${/[.!?]$/.test(item.note) ? '' : '.'}` : null}
    </Banner>
  );
}

function Loaded({ org, ws, business, owner }: { org: string; ws: string; business: Workspace; owner: boolean }) {
  const queue = useWorkspaceQueue(org, ws, owner);
  const access = useAccess(org);
  const nodes = useNodes(org);
  const workspaces = useWorkspaces(org).data ?? [business];
  const canAdd = access.can('lanes.operate') && !isNotServed(nodes.error);
  const [adding, setAdding] = useState(false);
  usePageCrumb(business.name, { mono: false });
  const count = `${plural(business.lanes.length, 'account')} in this business.`;
  return (
    <>
      <PageHeader
        lead={<BusinessMark name={business.name} color={business.color} size={40} />}
        title={business.name}
        description={owner && queue.data ? `${count} ${queueSentence(queue.data)}` : count}
        actions={
          canAdd ? (
            <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setAdding(true)}>
              Add an account
            </Button>
          ) : (
            <Button asChild>
              <Link to="/$org/accounts" params={{ org }} search={{ workspace: ws }}>
                All accounts
              </Link>
            </Button>
          )
        }
      />
      {owner && queue.data ? <Queue org={org} queue={queue.data} /> : null}
      {owner && queue.isError ? <ErrorState error={queue.error} onRetry={() => void queue.refetch()} className="mb-6" /> : null}
      <section aria-labelledby="business-accounts">
        <h2 id="business-accounts" className="sr-only">
          Accounts
        </h2>
        {business.lanes.length === 0 ? (
          <Card>
            <EmptyState
              dawn
              title="No accounts here yet"
              description="An account is one login on a site. Add one, and your agents can work on it."
              action={
                canAdd ? (
                  <Button variant="primary" onClick={() => setAdding(true)}>
                    Add an account
                  </Button>
                ) : null
              }
            />
          </Card>
        ) : (
          <ol className={GRID}>
            {business.lanes.map((lane) => (
              <AccountTile key={lane.id} org={org} lane={lane} />
            ))}
          </ol>
        )}
      </section>
      <NewLaneDialog org={org} open={adding} onOpenChange={setAdding} workspaces={workspaces} defaultWorkspace={ws} />
    </>
  );
}

export function WorkspacePage() {
  const { org, ws } = useParams({ from: '/$org/workspaces/$ws' });
  const access = useAccess(org);
  const business = useWorkspace(org, ws);
  const owner = access.can('workspaces.queue');

  if (business.isError && business.error instanceof ApiError && business.error.status === 404) {
    return (
      <Card>
        <EmptyState
          heading="h2"
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
      </Card>
    );
  }
  if (business.isError) {
    return (
      <Card>
        <ErrorState error={business.error} onRetry={() => void business.refetch()} />
      </Card>
    );
  }
  if (!business.data) {
    return (
      <div role="status" aria-label="Loading the business" className="grid gap-8">
        <div className="flex items-center gap-4">
          <Skeleton className="size-10 rounded-lg" />
          <div className="grid gap-2">
            <Skeleton className="h-8 w-64" />
            <Skeleton className="w-96 max-w-full" />
          </div>
        </div>
        <div className={GRID}>
          {Array.from({ length: 6 }, (_, index) => (
            <Skeleton key={index} className="h-18 rounded-xl" />
          ))}
        </div>
      </div>
    );
  }
  return <Loaded org={org} ws={ws} business={business.data} owner={owner} />;
}
