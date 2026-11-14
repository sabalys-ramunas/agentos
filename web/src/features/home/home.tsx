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
import { ArrowRight } from 'lucide-react';
import { useMemo } from 'react';
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
import type { Approval, BrandProposal, Job, Question, Status, StudioRunRow, VmList, Workspace } from '@/api/types';
import { OverLimitBanner } from '@/components/plan-notice';
import { useFreezeTarget } from '@/components/shell/freeze-control';
import { openUnfreeze } from '@/components/shell/palette-store';
import { Card } from '@/components/ui/card';
import { ErrorState, Skeleton } from '@/components/ui/states';
import { useActiveWorkspace } from '@/lib/active-workspace';
import { cn } from '@/lib/cn';
import { plural, usd } from '@/lib/format';
import { isOrgWide } from '@/lib/permissions';
import { useMe } from '@/lib/use-principal';
import { useNeedsYouCount } from '@/features/needs-you/count';
import { GettingStarted } from '../onboarding/getting-started';
import { HomeEnvironments } from '../environments/home-environments';
import { HomeNeeds } from './home-needs';
import type { Waiting } from './home-needs';
import { ProblemsPanel } from './problems';
import type { Problem } from './problems';
import { RunningNow } from './running-now';
import { blockedChecks, ENDED_BADLY, isGoing, machinesOf, pageName, runningItems, standingFailures } from './running';
import type { Trouble } from './sentence';
import { homeSentence } from './sentence';
import { accountCondition, accountName, problemSentence } from './words';
import { EarlierToday, HomeHeader, NoWorkspaces, inline } from './home-parts';

const startOfToday = () => {
  const day = new Date();
  day.setHours(0, 0, 0, 0);
  return day.getTime();
};

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

  const waitingItems = waitingOf(scope, approvals.data, brand.data, questions.data);
  const oldestAt = waitingItems.map((item) => item.at).sort()[0] ?? null;

  const { failures, problems, troubles } = problemsOf({ jobs, jobList, checkList, scope, since, shownWorkspaces, status, freeze, me });

  if (workspaces.isError) {
    return (
      <Card>
        <ErrorState error={workspaces.error} onRetry={() => void workspaces.refetch()} />
      </Card>
    );
  }
  if (!workspaces.data) return <HomeSkeleton />;

  const date = new Date(now).toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long' });

  if (data.length === 0) return <NoWorkspaces org={org} date={date} envs={envs.data} canSetUp={canSetUp} />;

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

  const today = todayOf({ jobList, checkList, scope, since, status, vms: vms.data });

  return (
    <>
      <HomeHeader org={org} date={date} going={Boolean(items && items.some(isGoing))} current={current} workspaces={data} sentence={sentence} />

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

      <EarlierToday org={org} jobs={jobs} studio={studio} status={status} today={today} />

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

/** Earlier today, from the runs and checks the node serves. */
export function todayOf({ jobList, checkList, scope, since, status, vms }: { jobList: Job[]; checkList: StudioRunRow[]; scope: string; since: number; status: Status | undefined; vms: VmList | undefined }) {
  const endedToday = jobList.filter((job) => job.state !== 'running' && (!scope || job.workspace === scope) && Date.parse(job.started_at) >= since);
  const failedToday = endedToday.filter((job) => ENDED_BADLY.has(job.state)).length;
  const checksToday = checkList.filter((run) => run.kind === 'check' && Date.parse(run.created_at) >= since && (!scope || (run.profile ?? '').startsWith(`${scope}/`)));
  const latest = newestPerPage(checksToday);
  const openFindings = latest.reduce((sum, run) => sum + run.counts.total, 0);
  const share = [...checkList].sort((a, b) => b.created_at.localeCompare(a.created_at)).find((run) => run.system_share !== null)?.system_share ?? null;
  const nodes = status?.nodes ?? [];
  const online = nodes.filter((node) => node.state !== 'down').length;
  const liveVms = vms?.available ? vms.vms.filter((vm) => vm.state !== 'deleted' && vm.state !== 'failed') : [];
  const vmCost = liveVms.reduce((sum, vm) => sum + (vm.cost_eur_so_far ?? 0), 0);
  return { endedToday, failedToday, latest, openFindings, share, nodes, online, liveVms, vmCost };
}

interface ProblemInput {
  jobs: ReturnType<typeof useJobs>;
  jobList: Job[];
  checkList: StudioRunRow[];
  scope: string;
  since: number;
  shownWorkspaces: Workspace[];
  status: Status | undefined;
  freeze: ReturnType<typeof useFreezeTarget>;
  me: ReturnType<typeof useMe>;
}

/** What is wrong, twice: as the problem cards, and as the troubles Home's sentence names. */
function problemsOf({ jobs, jobList, checkList, scope, since, shownWorkspaces, status, freeze, me }: ProblemInput) {
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
  return { failures, problems, troubles };
}

/** What waits for a person in this scope, oldest first in the list Home shows. */
function waitingOf(scope: string, approvals: Approval[] | undefined, brand: BrandProposal[] | undefined, questions: Question[] | undefined): Waiting[] {
  const mine = (lane: string | null | undefined) => !scope || (lane ?? '').startsWith(`${scope}/`);
  return [
    ...(approvals ?? []).filter((item) => item.kind !== 'question' && mine(item.lane)).map((approval) => ({ type: 'approval' as const, id: approval.id, at: approval.created_at, approval })),
    ...(brand ?? []).filter((item) => !scope || item.workspace === scope).map((proposal) => ({ type: 'brand' as const, id: proposal.id, at: proposal.created_at, proposal })),
    ...(questions ?? []).filter((item) => mine(item.lane)).map((question) => ({ type: 'question' as const, id: question.id, at: question.created_at, question })),
  ];
}
