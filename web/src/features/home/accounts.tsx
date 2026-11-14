/**
 * Accounts: every account by workspace, each with its site's mark, its state in words, its last
 * picture, what it did last and the one thing it needs (structure.md section 3). It is the
 * everyday list of what the engine calls lanes; an account's own page is the lane page.
 */
import { Link, useNavigate, useParams, useSearch } from '@tanstack/react-router';
import { Plus } from 'lucide-react';
import { useMemo, useState } from 'react';
import { useAccess, useActivity, useLaneAskFirst, useNodes, useWorkspaces } from '@/api/hooks';
import { isNotServed } from '@/api/http';
import type { Lane, LaneAskFirst, TraceEvent, Workspace } from '@/api/types';
import { BusinessMark } from '@/components/brand/identity';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { PageHeader } from '@/components/ui/misc';
import { StateGlyph } from '@/components/ui/state-badge';
import { EmptyState, ErrorState, Skeleton } from '@/components/ui/states';
import { useActiveWorkspace } from '@/lib/active-workspace';
import { cn } from '@/lib/cn';
import { STATE_WORDS, type PersonState } from '@/lib/states';
import { useMe } from '@/lib/use-principal';
import { AccountCard } from './cards';
import { NewLaneDialog } from './new-lane-dialog';
import { accountCondition, businessSentence, isAccount, isStory } from './words';

type Filter = 'all' | 'needs' | 'working' | 'resting';

const FILTERS: { value: Filter; label: string; states: PersonState[] }[] = [
  { value: 'all', label: 'All', states: ['stuck', 'waiting', 'working', 'good', 'sleeping'] },
  { value: 'needs', label: 'Need a hand', states: ['stuck', 'waiting'] },
  { value: 'working', label: 'Working now', states: ['working'] },
  { value: 'resting', label: STATE_WORDS.sleeping, states: ['sleeping'] },
];

const ORDER: PersonState[] = ['stuck', 'waiting', 'working', 'good', 'sleeping'];

function Group({ org, workspace, lanes, stories, canWatch, offset, asks }: { org: string; workspace: Workspace; lanes: Lane[]; stories: TraceEvent[]; canWatch: boolean; offset: number; asks: LaneAskFirst[] }) {
  const me = useMe();
  const line = businessSentence(workspace, { me });
  const sorted = [...lanes].sort((a, b) => ORDER.indexOf(accountCondition(a).state) - ORDER.indexOf(accountCondition(b).state));
  const accounts = sorted.filter(isAccount);
  const tools = sorted.filter((lane) => !isAccount(lane));
  const cards = (list: Lane[], start: number) => (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3 lg:gap-4">
      {list.map((lane, index) => (
        <AccountCard key={lane.id} org={org} lane={lane} last={stories.find((item) => item.lane === lane.id)} canWatch={canWatch} index={start + index} workspaceName={workspace.name} ask={asks.find((item) => item.lane === lane.id)} />
      ))}
    </div>
  );
  return (
    <section aria-labelledby={`ws-${workspace.id}`} className="mb-12">
      <div className="mb-4 flex flex-wrap items-center gap-x-3 gap-y-1">
        <BusinessMark name={workspace.name} color={workspace.color} size={24} />
        <h2 id={`ws-${workspace.id}`} className="text-16 font-semibold">
          <Link to="/$org/workspaces/$ws" params={{ org, ws: workspace.id }} className="hover:underline">
            {workspace.name}
          </Link>
        </h2>
        <span className="inline-flex min-w-0 items-center gap-2 text-13 text-muted">
          <StateGlyph state={line.state} size={14} />
          {line.text}
        </span>
      </div>
      {accounts.length ? cards(accounts, offset) : null}
      {tools.length ? (
        <>
          <h3 className="mt-6 mb-3 text-13 text-faint">Tools on the computer</h3>
          {cards(tools, offset + accounts.length)}
        </>
      ) : null}
    </section>
  );
}

export function Accounts() {
  const { org } = useParams({ from: '/$org/accounts' });
  const search = useSearch({ from: '/$org/accounts' });
  const navigate = useNavigate({ from: '/$org/accounts' });
  const [active] = useActiveWorkspace(org);
  const workspaces = useWorkspaces(org);
  const access = useAccess(org);
  const canWatch = access.can('lanes.watch');
  const asks = useLaneAskFirst(org).data ?? [];
  const nodes = useNodes(org);
  const canAddLane = access.can('lanes.operate') && !isNotServed(nodes.error);
  const [filter, setFilter] = useState<Filter>('all');
  const [adding, setAdding] = useState(false);
  const chosen = search.workspace ?? active;
  const feed = useActivity(org, chosen ? { workspace: chosen } : {});
  const stories = useMemo(() => (feed.data?.pages.flatMap((page) => page.items) ?? []).filter(isStory), [feed.data]);

  const data = workspaces.data ?? [];
  const inScope = data.filter((item) => !chosen || item.id === chosen);
  const all = inScope.flatMap((item) => item.lanes);
  const count = (value: Filter) => all.filter((lane) => FILTERS.find((item) => item.value === value)!.states.includes(accountCondition(lane).state)).length;
  const states = FILTERS.find((item) => item.value === filter)!.states;

  let offset = 0;
  return (
    <>
      <PageHeader
        title="Accounts"
        description="Every account your agents use, by workspace: how it is, what it did last, and whether it runs on its own or asks you first."
        actions={
          <div className="flex flex-wrap items-center gap-2">
            {data.length ? (
              <Button asChild variant="secondary">
                <Link to="/$org/accounts/engagement" params={{ org }}>
                  Engagement
                </Link>
              </Button>
            ) : null}
            {canAddLane && data.length ? (
              <Button icon={<Plus className="size-4" />} onClick={() => setAdding(true)}>
                Add an account
              </Button>
            ) : null}
          </div>
        }
      />

      {data.length > 1 ? (
        <div className="mb-8 flex flex-wrap items-center gap-2">
          <div role="radiogroup" aria-label="Show" className="flex rounded-lg border border-line p-1">
            {FILTERS.map((item) => {
              const n = item.value === 'all' ? all.length : count(item.value);
              return (
                <button
                  key={item.value}
                  type="button"
                  role="radio"
                  aria-checked={filter === item.value}
                  onClick={() => setFilter(item.value)}
                  className={cn(
                    'inline-flex h-7 items-center gap-2 rounded-md px-3 text-13 text-muted transition-colors duration-(--duration-fast) hover:text-fg',
                    filter === item.value && 'bg-fill-2 text-fg',
                  )}
                >
                  {item.label}
                  <span className="tabular text-12 text-faint">{n}</span>
                </button>
              );
            })}
          </div>
          <div className="flex flex-wrap items-center gap-1">
            <button
              type="button"
              aria-pressed={!chosen}
              onClick={() => void navigate({ search: {} })}
              className={cn('h-8 rounded-full px-3 text-13 text-muted hover:bg-fill-1 hover:text-fg', !chosen && 'bg-fill-2 text-fg')}
            >
              All workspaces
            </button>
            {data.map((item) => (
              <button
                key={item.id}
                type="button"
                aria-pressed={chosen === item.id}
                onClick={() => void navigate({ search: { workspace: item.id } })}
                className={cn('inline-flex h-8 items-center gap-2 rounded-full px-3 text-13 text-muted hover:bg-fill-1 hover:text-fg', chosen === item.id && 'bg-fill-2 text-fg')}
              >
                <BusinessMark name={item.name} color={item.color} size={14} />
                {item.name}
              </button>
            ))}
          </div>
        </div>
      ) : null}

      {workspaces.isError ? (
        <Card>
          <ErrorState error={workspaces.error} onRetry={() => void workspaces.refetch()} />
        </Card>
      ) : !workspaces.data ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3" aria-busy="true" aria-label="Loading">
          {Array.from({ length: 6 }, (_, index) => (
            <div key={index} className="rounded-(--radius-card) border border-line p-2">
              <Skeleton className="aspect-[16/10] h-auto rounded-lg" />
              <Skeleton className="m-3 w-32" />
            </div>
          ))}
        </div>
      ) : all.length === 0 ? (
        <Card>
          <EmptyState
            title="No accounts yet"
            description="An account is one login on a site: your inbox, your accounting, a supplier's order page, your Instagram. Add one, and your agents can work on it."
            action={
              canAddLane && data.length ? (
                <Button variant="primary" onClick={() => setAdding(true)}>
                  Add an account
                </Button>
              ) : (
                <Button asChild>
                  <Link to="/$org" params={{ org }}>
                    Back to Home
                  </Link>
                </Button>
              )
            }
          />
        </Card>
      ) : (
        (() => {
          const groups = inScope
            .map((workspace) => ({ workspace, lanes: workspace.lanes.filter((lane) => states.includes(accountCondition(lane).state)) }))
            .filter((group) => group.lanes.length);
          if (groups.length === 0) {
            return (
              <Card className="flex flex-col items-center px-6 py-14 text-center">
                <StateGlyph state={filter === 'needs' ? 'good' : 'sleeping'} size={40} />
                <p className="mt-4 text-14 font-medium">{filter === 'needs' ? 'No account needs a hand' : filter === 'working' ? 'No agent is working right now' : 'No account is resting'}</p>
                <p className="mt-1 max-w-sm text-14 text-muted">
                  {filter === 'needs' ? 'Every account can do its job. We will tell you when one needs you.' : 'Everything is as it should be.'}
                </p>
              </Card>
            );
          }
          return groups.map(({ workspace, lanes }) => {
            const start = offset;
            offset += lanes.length;
            return <Group key={workspace.id} org={org} workspace={workspace} lanes={lanes} stories={stories} canWatch={canWatch} offset={start} asks={asks} />;
          });
        })()
      )}
      <NewLaneDialog org={org} open={adding} onOpenChange={setAdding} workspaces={data} defaultWorkspace={chosen} />
    </>
  );
}
