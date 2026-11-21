/**
 * Accounts (spec-09, accounts.png): every account by workspace. The states as a segmented control with counts and
 * the workspaces as chips; then each workspace as a heading with its mark and count, one banner that says what
 * needs a look in a sentence, and its accounts as cards in three columns. It is the everyday list of what the
 * engine calls lanes; an account's own page is the lane page.
 */
import { Link, useNavigate, useParams, useSearch } from '@tanstack/react-router';
import { Plus } from 'lucide-react';
import { useMemo, useState } from 'react';
import { useAccess, useActivity, useLaneAskFirst, useNodes, useWorkspaces } from '@/api/hooks';
import { isNotServed } from '@/api/http';
import type { Lane, LaneAskFirst, TraceEvent, Workspace } from '@/api/types';
import { BusinessMark } from '@/components/brand/identity';
import { Chip } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { PageHeader } from '@/components/ui/misc';
import { Banner, EmptyState, ErrorState, Skeleton } from '@/components/ui/states';
import { SegmentedControl } from '@/components/ui/tabs';
import { useActiveWorkspace } from '@/lib/active-workspace';
import { plural } from '@/lib/format';
import type { LaneContext, PersonState } from '@/lib/states';
import { STATE_WORDS } from '@/lib/states';
import { useMe } from '@/lib/use-principal';
import { AccountCard } from './cards';
import { NewLaneDialog } from './new-lane-dialog';
import { accountCondition, accountName, isAccount, isStory, nextStep, problemSentence } from './words';

type Filter = 'all' | 'needs' | 'working' | 'resting';

const FILTERS: { value: Filter; label: string; states: PersonState[] }[] = [
  { value: 'all', label: 'All', states: ['stuck', 'waiting', 'working', 'good', 'sleeping'] },
  { value: 'needs', label: 'Need a hand', states: ['stuck', 'waiting'] },
  { value: 'working', label: 'Working now', states: ['working'] },
  { value: 'resting', label: STATE_WORDS.sleeping, states: ['sleeping'] },
];

const ORDER: PersonState[] = ['stuck', 'waiting', 'working', 'good', 'sleeping'];

const lower = (text: string) => text.charAt(0).toLowerCase() + text.slice(1);

const GRID = 'grid gap-4 md:grid-cols-2 xl:grid-cols-3';

/**
 * What needs a look in a workspace, as one sentence with the one thing to do: "Reddit is switched off for safety: it
 * is signed in as someone else." Two or more are counted, and their own cards say what each needs.
 */
function WorkspaceProblem({ org, lanes, me }: { org: string; lanes: Lane[]; me: LaneContext['me'] }) {
  const problems = lanes.filter((lane) => {
    const state = accountCondition(lane, me).state;
    return state === 'stuck' || state === 'waiting';
  });
  const first = problems[0];
  if (!first) return null;
  const stuck = problems.some((lane) => accountCondition(lane, me).state === 'stuck');
  if (problems.length > 1) {
    return (
      <Banner tone={stuck ? 'bad' : 'warn'} className="mb-4">
        {`${problems.length} accounts here need a look: ${problems.map(accountName).join(', ')}. Each card says what it needs.`}
      </Banner>
    );
  }
  const condition = accountCondition(first, me);
  const lead = problemSentence(first, me) ?? `${accountName(first)} needs a look`;
  // Switched off says why, from the account's own reason ("switched off for safety: it is signed in as someone else").
  // Every other lead already is the reason.
  const why = first.health?.state === 'quarantined' && first.frozen !== true && !lead.toLowerCase().includes(lower(condition.sentence));
  const more = why ? `: it is ${lower(condition.sentence).replace(/^(it is|it's) /, '')}` : '';
  const step = nextStep(first);
  return (
    <Banner
      tone={stuck ? 'bad' : 'warn'}
      className="mb-4"
      action={
        step ? (
          <Button asChild size="sm">
            <Link to="/$org/lanes/$ws/$name" params={{ org, ws: first.workspace, name: first.name }} aria-label={`${step.label}: ${accountName(first)}`}>
              {step.label}
            </Link>
          </Button>
        ) : null
      }
    >
      {`${lead}${more}.`}
    </Banner>
  );
}

function Group({ org, workspace, lanes, stories, asks }: { org: string; workspace: Workspace; lanes: Lane[]; stories: TraceEvent[]; asks: LaneAskFirst[] }) {
  const me = useMe();
  const sorted = [...lanes].sort((a, b) => ORDER.indexOf(accountCondition(a, me).state) - ORDER.indexOf(accountCondition(b, me).state));
  const accounts = sorted.filter(isAccount);
  const tools = sorted.filter((lane) => !isAccount(lane));
  const cards = (list: Lane[]) => (
    <div className={GRID}>
      {list.map((lane) => (
        <AccountCard key={lane.id} org={org} lane={lane} last={stories.find((item) => item.lane === lane.id)} ask={asks.find((item) => item.lane === lane.id)} />
      ))}
    </div>
  );
  return (
    <section aria-labelledby={`ws-${workspace.id}`} className="mb-12 last:mb-0">
      <div className="mb-4 flex min-w-0 items-center gap-3">
        <BusinessMark name={workspace.name} color={workspace.color} size={32} />
        <h2 id={`ws-${workspace.id}`} className="min-w-0 truncate text-heading">
          <Link to="/$org/workspaces/$ws" params={{ org, ws: workspace.id }} className="hover:underline hover:underline-offset-4">
            {workspace.name}
          </Link>
        </h2>
        <span className="shrink-0 text-body text-muted tabular">{plural(workspace.lanes.length, 'account')}</span>
      </div>
      <WorkspaceProblem org={org} lanes={lanes} me={me} />
      {accounts.length ? cards(accounts) : null}
      {tools.length ? (
        <>
          <h3 className="mt-8 mb-3 text-label text-muted">Tools on the computer</h3>
          {cards(tools)}
        </>
      ) : null}
    </section>
  );
}

/** The heading and cards, shaped like what comes, so nothing moves when it does. */
function AccountsSkeleton() {
  return (
    <div role="status" aria-label="Loading accounts" className="grid gap-4">
      <div className="flex items-center gap-3">
        <Skeleton className="size-8 rounded-md" />
        <Skeleton className="h-6 w-40" />
      </div>
      <div className={GRID}>
        {Array.from({ length: 6 }, (_, index) => (
          <Card key={index} className="grid h-48 content-start gap-4 p-5">
            <div className="flex items-center gap-3">
              <Skeleton className="size-10 rounded-lg" />
              <div className="grid flex-1 gap-2">
                <Skeleton className="w-28" />
                <Skeleton className="h-3 w-36" />
              </div>
            </div>
            <Skeleton className="w-3/4" />
            <Skeleton className="h-3 w-1/2" />
          </Card>
        ))}
      </div>
    </div>
  );
}

const NONE: Record<Exclude<Filter, 'all'>, { title: string; description: string }> = {
  needs: { title: 'No account needs a hand', description: 'Every account can do its job. We will tell you when one needs you.' },
  working: { title: 'No agent is working right now', description: 'An account shows here while an agent uses it.' },
  resting: { title: 'No account is sleeping', description: 'Every account is awake. One that has nothing to do goes to sleep on its own.' },
};

export function Accounts() {
  const { org } = useParams({ from: '/$org/accounts' });
  const search = useSearch({ from: '/$org/accounts' });
  const navigate = useNavigate({ from: '/$org/accounts' });
  const me = useMe();
  const [active] = useActiveWorkspace(org);
  const workspaces = useWorkspaces(org);
  const access = useAccess(org);
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
  const states = FILTERS.find((item) => item.value === filter)!.states;
  const count = (value: Filter) => all.filter((lane) => FILTERS.find((item) => item.value === value)!.states.includes(accountCondition(lane, me).state)).length;
  const groups = inScope.map((workspace) => ({ workspace, lanes: workspace.lanes.filter((lane) => states.includes(accountCondition(lane, me).state)) })).filter((group) => group.lanes.length);

  const body = () => {
    if (workspaces.isError) {
      return (
        <Card>
          <ErrorState error={workspaces.error} onRetry={() => void workspaces.refetch()} />
        </Card>
      );
    }
    if (!workspaces.data) return <AccountsSkeleton />;
    if (all.length === 0) {
      return (
        <Card>
          <EmptyState
            dawn
            heading="h2"
            title="No accounts yet"
            description="An account is one login on a site: your inbox, your accounting, a supplier's order page, your Instagram. Add one, and your agents can work on it."
            action={
              canAddLane && data.length ? (
                <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setAdding(true)}>
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
      );
    }
    if (groups.length === 0 && filter !== 'all') {
      const none = NONE[filter];
      return (
        <Card>
          <EmptyState
            dawn={filter === 'needs' ? true : 'grey'}
            heading="h2"
            title={none.title}
            description={none.description}
            action={<Button onClick={() => setFilter('all')}>Show all {all.length}</Button>}
          />
        </Card>
      );
    }
    return groups.map(({ workspace, lanes }) => <Group key={workspace.id} org={org} workspace={workspace} lanes={lanes} stories={stories} asks={asks} />);
  };

  return (
    <>
      <PageHeader
        title="Accounts"
        description="Every account your agents use, by workspace: how it is, what it did last, and whether it runs on its own or asks you first."
        actions={
          canAddLane && data.length && all.length ? (
            <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setAdding(true)}>
              Add an account
            </Button>
          ) : null
        }
      />

      {all.length ? (
        <div className="mb-8 grid gap-3">
          <SegmentedControl<Filter>
            label="Show"
            size="md"
            value={filter}
            onValueChange={setFilter}
            className="max-w-full justify-self-start overflow-x-auto"
            options={FILTERS.map((item) => ({
              value: item.value,
              label: (
                <>
                  {item.label}
                  <span className="tabular text-secondary font-normal text-faint">{item.value === 'all' ? all.length : count(item.value)}</span>
                </>
              ),
            }))}
          />
          {data.length > 1 ? (
            <div role="group" aria-label="Workspace" className="scrollbar-none -mx-4 flex gap-2 overflow-x-auto px-4 sm:mx-0 sm:flex-wrap sm:px-0">
              <Chip pressed={!chosen} label="All workspaces" onClick={() => void navigate({ search: {} })} />
              {data.map((item) => (
                <Chip key={item.id} pressed={chosen === item.id} label={item.name} onClick={() => void navigate({ search: { workspace: item.id } })} />
              ))}
            </div>
          ) : null}
        </div>
      ) : null}

      {body()}
      <NewLaneDialog org={org} open={adding} onOpenChange={setAdding} workspaces={data} defaultWorkspace={chosen} />
    </>
  );
}
