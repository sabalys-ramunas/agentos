/**
 * Home (Mono, docs/design/app-design.md section 1): three answers in the first five seconds.
 *
 * 1. One sentence says the state, from real data only (sentence.ts): what runs on how many machines, what
 *    failed and whether it runs again, what waits for you and for how long. Under it, a line with links.
 * 2. Open environments, one compact row each, only when there are some (environments/home-environments.tsx).
 *    Running now: the slider of job cards (running-now.tsx).
 * 3. Needs you and Problems, side by side (home-needs.tsx, problems.tsx).
 * 4. Three quiet links at the foot: earlier today, pages, machines, with real numbers.
 *
 * What Home did before and still does, and where it lives now:
 * - An approver's Home is Needs you: the router sends them there (app/router.tsx), unchanged.
 * - First run (no workspaces): the getting-started card and the empty state with New workspace, as before.
 * - Getting started and the over-limit banner sit under the sentence, as before; the day's plan under the
 *   two panels, only when it has something to say.
 * - A frozen computer or a frozen account is a Problem, with Lift the freeze for whoever may lift it; the
 *   top bar's Frozen chip is unchanged.
 * - A member sees only their workspaces' runs and checks (the node filters them), no brand changes and no
 *   VMs; the AI usage line stays for owners and admins.
 * - The workspace cards and the pictures of what went out moved to Accounts and History; a chosen workspace
 *   narrows everything here and links to its own page.
 */
import { Link, useParams, useSearch } from '@tanstack/react-router';
import { ArrowRight, ChevronRight, Server, SquareTerminal } from 'lucide-react';
import { useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import {
  isStudioMissing,
  useAccess,
  useApprovals,
  useBrandProposals,
  useEnvironments,
  useHistory,
  useJobs,
  useQuestions,
  useStatus,
  useStudioRuns,
  useVms,
  useWorkspaces,
} from '@/api/hooks';
import { isNotServed } from '@/api/http';
import type { StudioRunRow, Workspace } from '@/api/types';
import { OverLimitBanner } from '@/components/plan-notice';
import { useFreezeTarget } from '@/components/shell/freeze-control';
import { openUnfreeze } from '@/components/shell/palette-store';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { KindTile } from '@/components/ui/kind';
import { StatusDot } from '@/components/ui/status';
import { StateGlyph } from '@/components/ui/state-badge';
import { EmptyState, ErrorState, Skeleton } from '@/components/ui/states';
import { useActiveWorkspace } from '@/lib/active-workspace';
import { cn } from '@/lib/cn';
import { money } from '@/features/needs-you/kinds';
import { plural, usd } from '@/lib/format';
import { isOrgWide } from '@/lib/permissions';
import { useMe } from '@/lib/use-principal';
import { useNeedsYouCount } from '@/features/needs-you/count';
import { NewWorkspaceDialog } from '../settings/workspaces';
import { GettingStarted } from '../onboarding/getting-started';
import { HomeEnvironments } from '../environments/home-environments';
import { splitEnvironments } from '../environments/words';
import { DayPlanStrip } from './day-plan';
import { HomeNeeds } from './home-needs';
import type { Waiting } from './home-needs';
import { ProblemsPanel } from './problems';
import type { Problem } from './problems';
import { RunningNow } from './running-now';
import { blockedChecks, ENDED_BADLY, isGoing, machinesOf, pageName, runningItems, standingFailures } from './running';
import type { LinePart, Trouble } from './sentence';
import { homeSentence } from './sentence';
import { accountCondition, accountName, problemSentence } from './words';

const startOfToday = () => {
  const day = new Date();
  day.setHours(0, 0, 0, 0);
  return day.getTime();
};

const inline = 'underline decoration-line-strong underline-offset-4 hover:decoration-fg';

/** One part of the line under the sentence: a link where it leads somewhere. */
function Part({ org, part }: { org: string; part: LinePart }) {
  switch (part.kind) {
    case 'trouble':
      if (part.to === 'check')
        return (
          <Link to="/$org/checks/$id" params={{ org, id: part.id }} className={cn(inline, 'inline-block text-fg')}>
            {part.text}
          </Link>
        );
      if (part.to === 'machine')
        return (
          <Link to="/$org/nodes/$id" params={{ org, id: part.id }} className={cn(inline, 'inline-block text-fg')}>
            {part.text}
          </Link>
        );
      {
        const [ws = '', name = ''] = part.id.split('/');
        return (
          <Link to="/$org/lanes/$ws/$name" params={{ org, ws, name }} className={cn(inline, 'inline-block text-fg')}>
            {part.text}
          </Link>
        );
      }
    case 'needs':
      return (
        <>
          <Link to="/$org/needs-you" params={{ org }} className={cn(inline, 'whitespace-nowrap text-fg')}>
            {part.text}
          </Link>
          {part.rest}
        </>
      );
    default:
      return <>{part.text}</>;
  }
}

const footLink = 'group flex min-w-0 items-center gap-3 rounded-xl border border-line bg-card px-5 py-4 shadow-lift transition-colors duration-(--duration-fast) hover:border-line-strong';

/** What a foot link says: its title with an icon, then the numbers. */
function FootBody({ icon, title, children }: { icon: ReactNode; title: string; children: ReactNode }) {
  return (
    <>
      <span className="grid min-w-0 flex-1 gap-1">
        <span className="flex items-center gap-2 text-caption text-faint [&_svg]:size-3.5">
          {icon}
          {title}
        </span>
        <span className="text-body">{children}</span>
      </span>
      <ChevronRight aria-hidden="true" className="size-4 shrink-0 text-faint transition-transform duration-(--duration-fast) group-hover:translate-x-0.5" />
    </>
  );
}

/** The newest check of each page, from those given. */
function newestPerPage(runs: StudioRunRow[]): StudioRunRow[] {
  const map = new Map<string, StudioRunRow>();
  for (const run of runs) {
    const held = map.get(run.url);
    if (!held || run.created_at > held.created_at) map.set(run.url, run);
  }
  return [...map.values()];
}

function HomeSkeleton() {
  return (
    <div aria-busy="true" aria-label="Loading">
      <Skeleton className="h-3 w-48" />
      <Skeleton className="mt-4 h-10 w-full max-w-2xl" />
      <Skeleton className="mt-4 mb-12 h-5 w-full max-w-lg" />
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {[0, 1, 2].map((index) => (
          <Card key={index} className={cn('overflow-hidden', index > 0 && 'max-sm:hidden', index > 1 && 'max-lg:hidden')}>
            <Skeleton className="h-36 rounded-none" />
            <div className="grid gap-3 p-4">
              <Skeleton className="w-24" />
              <Skeleton className="h-5 w-3/4" />
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
}

export function Home() {
  const { org } = useParams({ from: '/$org/' });
  const { job: firstJob } = useSearch({ from: '/$org/' });
  const workspaces = useWorkspaces(org);
  const [active] = useActiveWorkspace(org);
  const access = useAccess(org);
  const me = useMe();
  const owner = access.role === 'owner';
  const canWatch = access.can('lanes.watch');
  const canSetUp = access.can('workspaces.create');
  const status = useStatus(org).data;
  const [newWorkspace, setNewWorkspace] = useState(false);

  const data = useMemo(() => workspaces.data ?? [], [workspaces.data]);
  const current = data.find((item) => item.id === active) ?? null;
  const scope = current ? current.id : '';
  const shownWorkspaces: Workspace[] = current ? [current] : data;

  const jobs = useJobs(org, { limit: 100 });
  const studio = useStudioRuns(org, { limit: 40 });
  const history = useHistory(org, scope ? { workspace: scope } : {}, 1);
  const approvals = useApprovals(org);
  const questions = useQuestions(org);
  const brand = useBrandProposals(org, owner);
  const vms = useVms(org, owner);
  const envs = useEnvironments(org);
  // The same count as the rail and Needs you (count.ts), narrowed to the chosen workspace.
  const count = useNeedsYouCount(org, scope || null);
  const freeze = useFreezeTarget(org);

  const now = Date.now();
  const since = startOfToday();
  const jobList = useMemo(() => (jobs.data?.available ? jobs.data.jobs : []), [jobs.data]);
  const checkList = useMemo(() => (studio.data && !isStudioMissing(studio.data) ? studio.data.runs : []), [studio.data]);
  const stories = history.data?.pages[0]?.working_now;
  const jobsSettled = !jobs.isPending;
  // The slider waits for the runs and the routine runs; a history that cannot be read counts as none.
  const items = useMemo(
    () => (jobsSettled && (stories || history.isError) && workspaces.data ? runningItems({ jobs: jobList, checks: checkList, stories: stories ?? [], workspaces: data, scope, now }) : null),
    // `now` moves every render; the list is worked out again when its data does.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [jobsSettled, jobList, checkList, stories, history.isError, workspaces.data, data, scope],
  );

  const mine = (lane: string | null | undefined) => !scope || (lane ?? '').startsWith(`${scope}/`);
  const waitingItems: Waiting[] = [
    ...(approvals.data ?? []).filter((item) => item.kind !== 'question' && mine(item.lane)).map((approval) => ({ type: 'approval' as const, id: approval.id, at: approval.created_at, approval })),
    ...(brand.data ?? []).filter((item) => !scope || item.workspace === scope).map((proposal) => ({ type: 'brand' as const, id: proposal.id, at: proposal.created_at, proposal })),
    ...(questions.data ?? []).filter((item) => mine(item.lane)).map((question) => ({ type: 'question' as const, id: question.id, at: question.created_at, question })),
  ];
  const oldestAt = waitingItems.map((item) => item.at).sort()[0] ?? null;

  const failures = jobs.isSuccess ? standingFailures(jobList, scope, since) : [];
  const blocked = blockedChecks(checkList, scope, since);
  const scopedNodes = new Set(shownWorkspaces.flatMap((workspace) => workspace.lanes.map((lane) => lane.node)));
  const downNodes = (status?.nodes ?? []).filter((node) => node.state === 'down' && (!scope || scopedNodes.has(node.id)));
  const stuck = shownWorkspaces.flatMap((workspace) =>
    workspace.lanes.filter((lane) => accountCondition(lane, me).state === 'stuck').map((lane) => ({ lane, workspace, text: `${workspace.name}'s ${problemSentence(lane, me) ?? `${accountName(lane)} is stuck`}` })),
  );
  const problems: Problem[] = [
    ...failures.map(({ job, failure }) => ({ type: 'run' as const, id: `run:${job.id}`, job, failure })),
    ...blocked.map((run) => ({ type: 'check' as const, id: `check:${run.id}`, run })),
    ...(freeze.frozen ? [{ type: 'frozen' as const, id: `frozen:${freeze.machine}`, name: freeze.machine || 'This computer' }] : []),
    ...downNodes.map((node) => ({ type: 'machine' as const, id: `machine:${node.id}`, name: node.id })),
    ...stuck.map(({ lane, workspace, text }) => ({ type: 'account' as const, id: `account:${lane.id}`, lane, workspace, text })),
  ];
  const troubles: Trouble[] = [
    ...blocked.map((run): Trouble => ({ text: `${pageName(run.url)} has ${plural(run.counts.blocker, 'blocker')}`, to: 'check', id: run.id })),
    ...(freeze.frozen && freeze.machine ? [{ text: `${freeze.machine} is frozen`, to: 'machine' as const, id: freeze.machine }] : []),
    ...downNodes.map((node): Trouble => ({ text: `${node.id} is not answering`, to: 'machine', id: node.id })),
    ...stuck.map(({ lane, text }): Trouble => ({ text, to: 'account', id: lane.id })),
  ];

  if (workspaces.isError) {
    return (
      <Card>
        <ErrorState error={workspaces.error} onRetry={() => void workspaces.refetch()} />
      </Card>
    );
  }
  if (!workspaces.data) return <HomeSkeleton />;

  const date = new Date(now).toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long' });

  if (data.length === 0) {
    const openEnvs = envs.data ? splitEnvironments(envs.data.environments).open.length : 0;
    return (
      <>
        <header className="mb-10">
          <p className="font-mono text-code-caption text-faint">{date}</p>
          {openEnvs > 0 ? (
            <>
              <h1 className="mt-3 text-page-title-compact text-balance sm:text-display">
                Your agents are working in {plural(openEnvs, 'environment')}.
              </h1>
              <p className="mt-3 max-w-2xl text-lede text-muted">Each is a computer of its own, deleted when the work is done. Accounts and routines show here once you add a workspace.</p>
            </>
          ) : (
            <>
              <h1 className="mt-3 text-page-title-compact text-balance sm:text-display">Nothing is running yet.</h1>
              <p className="mt-3 max-w-2xl text-lede text-muted">Give your agents a workspace and a computer, and this fills up as they work.</p>
            </>
          )}
        </header>
        <HomeEnvironments org={org} />
        <GettingStarted org={org} />
        <Card className="overflow-hidden">
          <EmptyState
            icon={<StateGlyph state="sleeping" size={24} />}
            title="No workspaces yet"
            description={
              canSetUp
                ? 'A workspace is one business or person: Aster Coffee, or your own accounts. Connect a computer and choose what your AI should do first, and agentos adds the accounts for you.'
                : 'A workspace is one business or person. An owner or admin of this organization sets them up, and yours appear here.'
            }
            action={
              canSetUp ? (
                <div className="flex flex-wrap justify-center gap-2">
                  <Button variant="primary" onClick={() => setNewWorkspace(true)}>
                    New workspace
                  </Button>
                  <Button asChild>
                    <Link to="/onboarding/$org/$step" params={{ org, step: 'node' }}>
                      Use the setup guide
                    </Link>
                  </Button>
                </div>
              ) : undefined
            }
          />
        </Card>
        <NewWorkspaceDialog org={org} open={newWorkspace} onOpenChange={setNewWorkspace} />
      </>
    );
  }

  const sentence = items
    ? homeSentence({
        running: items.filter(isGoing).length,
        machines: machinesOf(items),
        knowsRuns: jobs.isSuccess && jobs.data.available,
        failures: failures.map((item) => item.failure),
        troubles,
        waiting: count.loaded ? count.total : null,
        oldestAt,
        now,
      })
    : null;

  // Earlier today, from the runs and checks the node serves.
  const endedToday = jobList.filter((job) => job.state !== 'running' && (!scope || job.workspace === scope) && Date.parse(job.started_at) >= since);
  const failedToday = endedToday.filter((job) => ENDED_BADLY.has(job.state)).length;
  const checksToday = checkList.filter((run) => run.kind === 'check' && Date.parse(run.created_at) >= since && (!scope || (run.profile ?? '').startsWith(`${scope}/`)));
  const latest = newestPerPage(checksToday);
  const openFindings = latest.reduce((sum, run) => sum + run.counts.total, 0);
  const share = [...checkList].sort((a, b) => b.created_at.localeCompare(a.created_at)).find((run) => run.system_share !== null)?.system_share ?? null;
  const nodes = status?.nodes ?? [];
  const online = nodes.filter((node) => node.state !== 'down').length;
  const liveVms = vms.data?.available ? vms.data.vms.filter((vm) => vm.state !== 'deleted' && vm.state !== 'failed') : [];
  const vmCost = liveVms.reduce((sum, vm) => sum + (vm.cost_eur_so_far ?? 0), 0);

  return (
    <>
      <header className="relative isolate mb-10 pt-2">
        <div
          aria-hidden="true"
          className="pointer-events-none absolute inset-x-0 -top-8 -z-10 h-56 bg-[radial-gradient(var(--line-strong)_1px,transparent_1px)] [background-size:16px_16px] [mask-image:linear-gradient(to_bottom,black,transparent)] opacity-60"
        />
        <p className="flex min-w-0 flex-wrap items-center gap-2 font-mono text-code-caption text-faint">
          {items && items.some(isGoing) ? <StatusDot tone="live" pulse /> : null}
          <span>{date}</span>
          <span aria-hidden="true">·</span>
          {current ? (
            <Link to="/$org/workspaces/$ws" params={{ org, ws: current.id }} className={inline}>
              {current.name}
            </Link>
          ) : (
            <span>{data.length === 1 ? data[0]!.name : plural(data.length, 'workspace')}</span>
          )}
        </p>
        {sentence ? (
          <>
            <h1 className="mt-3 max-w-4xl text-page-title-compact text-balance sm:text-display">{sentence.lead}</h1>
            {sentence.line.length ? (
              <p className="mt-3 max-w-3xl text-lede text-pretty text-muted">
                {sentence.line.map((part, index) => (
                  <span key={index}>
                    <Part org={org} part={part} />.{index < sentence.line.length - 1 ? ' ' : ''}
                  </span>
                ))}
              </p>
            ) : null}
          </>
        ) : (
          <div aria-busy="true">
            <h1 className="sr-only">Home</h1>
            <Skeleton className="mt-4 h-10 w-full max-w-2xl" />
            <Skeleton className="mt-4 h-5 w-full max-w-lg" />
          </div>
        )}
      </header>

      <HomeEnvironments org={org} />
      <GettingStarted org={org} />
      <OverLimitBanner org={org} />

      <RunningNow org={org} items={items} jobs={jobs.data} studio={studio.data} canWatch={canWatch} first={firstJob} />

      <div className="mt-10 grid items-start gap-6 lg:grid-cols-12">
        <div className="min-w-0 lg:col-span-7">
          <HomeNeeds org={org} items={waitingItems} total={count.loaded ? count.total : count.failedToLoad ? (status?.needs_you ?? 0) : null} oldestAt={oldestAt} workspaces={data} />
        </div>
        <div className="min-w-0 lg:col-span-5">
          <ProblemsPanel org={org} problems={problems} loading={jobs.isPending || studio.isPending} canLift={freeze.canLift} onLift={openUnfreeze} />
        </div>
      </div>

      <div className="mt-10">
        <DayPlanStrip org={org} workspace={scope} />
      </div>

      <nav aria-label="Earlier today" className="mt-10 grid gap-4 md:grid-cols-3">
        <Link to="/$org/runs" params={{ org }} className={footLink}>
          <FootBody icon={<SquareTerminal aria-hidden="true" />} title="Earlier today">
          {jobs.isError ? (
            isNotServed(jobs.error) ? 'Runs are shown on your computer' : 'Could not read the runs'
          ) : !jobs.data ? (
            <Skeleton className="w-40" />
          ) : !jobs.data.available ? (
            'No runs on this computer'
          ) : endedToday.length ? (
            <>
              {plural(endedToday.length, 'run')} ended
              {failedToday ? (
                <>
                  , <span className="text-bad">{failedToday} failed</span>
                </>
              ) : (
                ', none failed'
              )}
            </>
          ) : (
            'No runs ended today'
          )}
        </FootBody>
        </Link>
        <Link to="/$org/checks" params={{ org }} className={footLink}>
          <FootBody icon={<KindTile kind="design" size="xs" className="shadow-none" />} title="Pages">
          {studio.isError ? (
            isNotServed(studio.error) ? 'Checks are shown on your computer' : 'Could not read the checks'
          ) : !studio.data ? (
            <Skeleton className="w-40" />
          ) : isStudioMissing(studio.data) ? (
            'Studio is not on this computer'
          ) : latest.length ? (
            `${plural(latest.length, 'page')} checked today, ${openFindings ? plural(openFindings, 'finding') : 'no findings'}${share !== null ? `, ${Math.round(share * 100)}% on the design system` : ''}`
          ) : (
            'No page checked today'
          )}
        </FootBody>
        </Link>
        <Link to="/$org/nodes" params={{ org }} className={footLink}>
          <FootBody icon={<Server aria-hidden="true" />} title="Machines">
          {!status ? (
            <Skeleton className="w-40" />
          ) : nodes.length ? (
            <>
              {online} of {nodes.length} online
              {liveVms.length ? `, ${plural(liveVms.length, 'VM')} rented${vmCost ? `, ${money(vmCost, 'EUR')} so far` : ''}` : ''}
            </>
          ) : (
            'No machines yet'
          )}
        </FootBody>
        </Link>
      </nav>

      {isOrgWide(access.role) && status ? (
        <p className="mt-10 flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-line pt-6 text-secondary text-faint">
          AI usage today: <span className="font-mono text-code-caption text-muted tabular">{usd(status.cost_today.usd)}</span>
          <Link to="/$org/settings/brains" params={{ org }} className={cn(inline, 'inline-flex min-h-8 items-center text-muted hover:text-fg md:min-h-0')}>
            AI models
          </Link>
          {current ? (
            <Link to="/$org/workspaces/$ws" params={{ org, ws: current.id }} className={cn(inline, 'ml-auto inline-flex min-h-8 items-center gap-1 text-muted hover:text-fg md:min-h-0')}>
              {current.name}'s accounts
              <ArrowRight aria-hidden="true" className="size-3.5" />
            </Link>
          ) : null}
        </p>
      ) : null}
    </>
  );
}

