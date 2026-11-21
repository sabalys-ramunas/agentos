/**
 * Running now: Home's slider of job cards (Mono, docs/design/app-design.md "Running now: the slider").
 * One card per running thing, each with its kind's colour and word, what it is, who runs it and where,
 * how long, and one action that says what you will see. A picture only when it answers faster than a
 * sentence: the last lines of a running job's output, the page a check just looked at, an account's live
 * screen. Filter chips by kind, and failing, once there are enough cards to need them.
 */
import { Link } from '@tanstack/react-router';
import { ArrowRight, Check } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import type { ReactElement, ReactNode } from 'react';
import { isStudioMissing, useJobFollow, useStudioRun } from '@/api/hooks';
import { studioShotUrl } from '@/api/client';
import type { Job, JobList, StudioMissing, StudioRuns } from '@/api/types';
import { AgentMark } from '@/components/brand/identity';
import { Chip } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { KINDS, KindEyebrow } from '@/components/ui/kind';
import type { Kind } from '@/components/ui/kind';
import { CodeBlock } from '@/components/ui/misc';
import { Slider } from '@/components/ui/slider';
import { Status } from '@/components/ui/status';
import { Skeleton } from '@/components/ui/states';
import { parseAnsi } from '@/components/ui/terminal';
import { cn } from '@/lib/cn';
import { plural } from '@/lib/format';
import { routineName } from '@/lib/principal';
import { useTechnical } from '@/lib/technical';
import type { RunningItem } from './running';
import { pageName } from './running';
import { accountName } from './words';

/** "0:42", "12:05", "1:02:09": how long, ticking. */
export function clock(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = String(total % 60).padStart(2, '0');
  return hours ? `${hours}:${String(minutes).padStart(2, '0')}:${seconds}` : `${minutes}:${seconds}`;
}

/** Time since a moment, read again every second while on screen. */
function Elapsed({ since }: { since: string }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, []);
  return <span className="shrink-0 font-mono text-code-caption text-faint tabular">{clock(now - Date.parse(since))}</span>;
}

/** The last lines of a running job's output, drawn as a small dark terminal: a picture, read by the card's words. */
function MiniTerminal({ head, command, lines }: { head: string; command: string; lines: string[] }) {
  return (
    <div aria-hidden="true" className="flex h-full flex-col justify-end overflow-hidden bg-terminal px-4 py-3 font-mono text-code-caption text-terminal-text">
      <div className="truncate text-terminal-dim">{head}</div>
      <div className="truncate">
        <span className="text-terminal-dim">$ </span>
        {command}
      </div>
      {lines.map((line, index) => (
        <div key={index} className="truncate">
          {parseAnsi(line).map((seg, i) => (
            <span key={i} className={cn(seg.color === 'red' && 'text-ansi-red', seg.color === 'green' && 'text-ansi-green', seg.color === 'yellow' && 'text-ansi-yellow', seg.color === 'dim' && 'text-terminal-dim')}>
              {seg.text}
            </span>
          ))}
          {line ? null : ' '}
        </div>
      ))}
    </div>
  );
}

const lastLines = (text: string, count: number) =>
  text
    .split('\n')
    .map((line) => line.replace(/\r/g, ''))
    .filter((line, index, all) => line.trim() || index < all.length - 1)
    .slice(-count);

interface ShellProps {
  kind: Kind | RunningItem['kind'];
  label?: string;
  status: ReactNode;
  title: string;
  command?: string | null;
  who: ReactNode;
  progress?: ReactNode;
  picture?: ReactNode;
  action: ReactNode;
}

/** The card every running thing shares: the kind on the left, its status on the right, one action. */
function JobCardShell({ kind, label, status, title, command, who, progress, picture, action }: ShellProps) {
  const hue = kind in KINDS && KINDS[kind].hue ? (kind as Kind) : undefined;
  return (
    <Card kind={hue} aria-label={`${label ?? KINDS[kind].word}: ${title}`} className="flex h-full min-w-0 flex-col overflow-hidden">
      {picture ? <div className="h-36 shrink-0 overflow-hidden border-b border-line bg-inset">{picture}</div> : null}
      <div className="flex min-w-0 flex-1 flex-col gap-2 p-4">
        <div className="flex min-w-0 items-center gap-2">
          <KindEyebrow kind={kind} label={label} />
          <span className="ml-auto shrink-0">{status}</span>
        </div>
        <h3 title={title} className="truncate text-section-title">
          {title}
        </h3>
        {command ? (
          <p title={command} className="truncate font-mono text-code-caption text-muted">
            <span aria-hidden="true">$ </span>
            {command}
          </p>
        ) : null}
        <div className="flex min-w-0 items-center gap-2 text-secondary text-muted">{who}</div>
        {progress ? <div className="text-secondary text-muted">{progress}</div> : null}
        <div className="mt-auto pt-2">{action}</div>
      </div>
    </Card>
  );
}

/** The card's one action: a small button around the link given. */
function CardLink({ children }: { children: ReactElement }) {
  return (
    <Button asChild size="sm">
      {children}
    </Button>
  );
}

const arrow = <ArrowRight aria-hidden="true" className="size-3.5" />;

function testsWords(job: Job): ReactNode {
  const tests = job.tests;
  if (!tests) return null;
  return (
    <span className="flex items-center gap-2">
      <span className="font-mono text-code-caption tabular">{plural(tests.passed, 'test')} passed</span>
      {tests.failed ? <span className="font-mono text-code-caption text-bad tabular">{tests.failed} failed</span> : null}
    </span>
  );
}

/** A running `openajar run` job, with the last lines of its output while the first cards are in view. */
function JobCard({ org, job, kind, follow }: { org: string; job: Job; kind: RunningItem['kind']; follow: boolean }) {
  const output = useJobFollow(org, job.id, follow);
  const lines = lastLines(`${output.stdout}${output.stderr ? `\n${output.stderr}` : ''}`, 6);
  const failed = job.tests?.failed ?? 0;
  const title = job.source?.branch ? `${job.lane_name} on ${job.source.branch}` : job.lane_name;
  return (
    <JobCardShell
      kind={kind}
      label={kind === 'lane' ? 'Run' : undefined}
      status={failed ? <Status status={{ label: `${failed} failed`, tone: 'bad' }} /> : <Status status={{ label: 'Running', tone: 'live', pulse: true }} />}
      title={title}
      command={job.command}
      who={
        <>
          <span className="truncate">
            {job.started_by ? `${job.started_by} on ` : 'On '}
            <span className="font-mono text-code-caption">{job.node}</span>
          </span>
          <span className="ml-auto">
            <Elapsed since={job.started_at} />
          </span>
        </>
      }
      progress={testsWords(job)}
      picture={follow && lines.length ? <MiniTerminal head={`${job.node} · ${job.lane}`} command={job.command} lines={lines} /> : null}
      action={
        <CardLink>
          <Link to="/$org/runs/$id" params={{ org, id: job.id }}>
            {failed ? `See the ${failed === 1 ? 'failure' : `${failed} failures`}` : 'Watch the output'}
            {arrow}
          </Link>
        </CardLink>
      }
    />
  );
}

/** A design check that just ended: the page it looked at, and what it found. */
function CheckCard({ org, item }: { org: string; item: Extract<RunningItem, { type: 'check' }> }) {
  const { run } = item;
  const detail = useStudioRun(org, run.id);
  const [broken, setBroken] = useState(false);
  const screen = detail.data?.images_gone ? null : (detail.data?.screen_list.find((entry) => entry.image && entry.theme === 'light' && entry.width >= 1000) ?? detail.data?.screen_list.find((entry) => entry.image));
  const found = run.counts.total;
  return (
    <JobCardShell
      kind="design"
      status={
        run.counts.blocker ? (
          <Status status={{ label: plural(run.counts.blocker, 'blocker'), tone: 'bad' }} />
        ) : found ? (
          <Status status={{ label: plural(found, 'finding'), tone: 'warn' }} />
        ) : (
          <Status status={{ label: 'Clean', tone: 'ok' }} />
        )
      }
      title={`Check of ${pageName(run.url)}`}
      command={run.profile ? `ui_check ${run.url} · ${run.profile}` : `ui_check ${run.url}`}
      who={
        <>
          <span className="truncate">Ended {plural(Math.max(1, Math.round((Date.now() - Date.parse(run.created_at)) / 60_000)), 'minute')} ago</span>
        </>
      }
      progress={`${plural(run.screens - run.screens_failed, 'screen')} checked${run.screens_failed ? `, ${run.screens_failed} did not load` : ''}`}
      picture={
        screen && !broken ? (
          <img
            src={studioShotUrl(org, run.capture, screen.key, 640)}
            alt={`The page as the check saw it, ${screen.width} px ${screen.theme}`}
            onError={() => setBroken(true)}
            className="size-full object-cover object-top"
          />
        ) : null
      }
      action={
        <CardLink>
          <Link to="/$org/checks/$id" params={{ org, id: run.id }}>
            {found ? 'See the findings' : 'Open the check'}
            {arrow}
          </Link>
        </CardLink>
      }
    />
  );
}

function StoryCard({ org, item }: { org: string; item: Extract<RunningItem, { type: 'story' }> }) {
  const { story } = item;
  const routine = item.kind === 'routine' && story.technical.flow ? routineName(story.technical.flow) : null;
  return (
    <JobCardShell
      kind={item.kind}
      status={<Status status={{ label: 'Running', tone: 'live', pulse: true }} />}
      title={story.headline ?? story.sentence}
      command={routine}
      who={
        <>
          <span className="truncate">{story.place}</span>
          <span className="ml-auto">
            <Elapsed since={story.started_at} />
          </span>
        </>
      }
      progress={story.steps.length ? `${plural(story.steps.filter((step) => step.status !== 'running').length, 'step')} done` : null}
      action={
        <CardLink>
          <Link to="/$org/activity" params={{ org }} search={{ run: story.id }}>
            Follow it
            {arrow}
          </Link>
        </CardLink>
      }
    />
  );
}

function LaneCard({ org, item, canWatch }: { org: string; item: Extract<RunningItem, { type: 'lane' }>; canWatch: boolean }) {
  const { lane, workspace } = item;
  const [broken, setBroken] = useState(false);
  const picture = canWatch && lane.live_picture && !broken ? lane.live_picture : null;
  return (
    <JobCardShell
      kind="account"
      status={<Status status={{ label: 'Working', tone: 'live', pulse: true }} />}
      title={`${workspace.name}'s ${accountName(lane)}`}
      who={
        <>
          {lane.claim?.holder ? <AgentMark principal={lane.claim.holder} size={16} /> : null}
          <span className="truncate">
            An agent is acting in it, on <span className="font-mono text-code-caption">{lane.node}</span>
          </span>
        </>
      }
      picture={picture ? <img src={picture} alt={`${accountName(lane)} as it looks right now`} onError={() => setBroken(true)} className="size-full object-cover object-top" /> : null}
      action={
        <CardLink>
          <Link to="/$org/lanes/$ws/$name" params={{ org, ws: lane.workspace, name: lane.name }}>
            Watch it
            {arrow}
          </Link>
        </CardLink>
      }
    />
  );
}

type Filter = 'all' | 'failing' | RunningItem['kind'];

const CHIP_KINDS: { kind: RunningItem['kind']; label: string }[] = [
  { kind: 'test', label: 'Tests' },
  { kind: 'design', label: 'Design checks' },
  { kind: 'build', label: 'Builds' },
  { kind: 'server', label: 'Dev servers' },
  { kind: 'lane', label: 'Other runs' },
  { kind: 'routine', label: 'Routines' },
  { kind: 'account', label: 'Accounts' },
];

/** How many cards get a live terminal. Each follows its run's output, so the rest show words only. */
const FOLLOWED = 6;
/** Chips appear once there are this many cards. */
const CHIPS_FROM = 4;

interface RunningNowProps {
  org: string;
  items: RunningItem[] | null;
  jobs: JobList | undefined;
  studio: StudioRuns | StudioMissing | undefined;
  canWatch: boolean;
  /** A job brought to the front (Home's `?job=`). */
  first?: string;
}

/** "ended at 10:51 and passed": what a person reads; the command itself only under Technical details. */
const JOB_ENDED: Partial<Record<Job['state'], string>> = { passed: 'passed', failed: 'did not pass', cancelled: 'was stopped', timed_out: 'ran out of time' };

export function lastRunWords(last: Job, technical: boolean): string {
  const at = new Date(last.finished_at ?? last.started_at).toLocaleTimeString('en-GB', { hour: '2-digit', minute: '2-digit' });
  const how = JOB_ENDED[last.state] ?? last.state.replaceAll('_', ' ');
  return technical ? `The last run ended at ${at}: ${last.command}, ${how}.` : `The last run ended at ${at} and ${how}.`;
}

export function RunningNow({ org, items, jobs, studio, canWatch, first }: RunningNowProps) {
  const [filter, setFilter] = useState<Filter>('all');
  const [technical] = useTechnical();
  const ordered = useMemo(() => {
    if (!items || !first) return items;
    const pick = items.find((item) => item.id === first);
    return pick ? [pick, ...items.filter((item) => item !== pick)] : items;
  }, [items, first]);
  const going = (ordered ?? []).filter((item) => item.type !== 'check');
  const machines = new Set(going.map((item) => item.machine).filter(Boolean)).size;
  const meta = !ordered ? null : going.length ? `${plural(going.length, 'job')}${machines ? ` on ${plural(machines, 'machine')}` : ''}` : 'nothing going';
  const shown = (ordered ?? []).filter((item) => filter === 'all' || (filter === 'failing' ? item.failing : item.kind === filter));
  const chips = (ordered?.length ?? 0) >= CHIPS_FROM;
  const head = (
    <>
      <h2 id="home-running" className="text-card-title">
        Running now
      </h2>
      {meta ? <span className="text-secondary text-faint">{meta}</span> : null}
      {chips && ordered ? (
        <div role="group" aria-label="Show" className="flex items-center gap-2 max-sm:-mx-4 max-sm:w-[calc(100%+32px)] max-sm:overflow-x-auto max-sm:px-4 max-sm:[scrollbar-width:none] sm:flex-wrap">
          <Chip pressed={filter === 'all'} label="All" count={ordered.length} onClick={() => setFilter('all')} />
          {ordered.some((item) => item.failing) ? (
            <Chip pressed={filter === 'failing'} label="Failing" count={ordered.filter((item) => item.failing).length} onClick={() => setFilter('failing')} />
          ) : null}
          {CHIP_KINDS.filter(({ kind }) => ordered.some((item) => item.kind === kind)).map(({ kind, label }) => (
            <Chip
              key={kind}
              pressed={filter === kind}
              label={label}
              kind={KINDS[kind].hue ? (kind as Kind) : undefined}
              count={ordered.filter((item) => item.kind === kind).length}
              onClick={() => setFilter(kind)}
            />
          ))}
        </div>
      ) : null}
    </>
  );

  if (!ordered) {
    return (
      // Not a region while it loads: the slider that replaces it is the "Running now" region, so nothing
      // (a screen reader's landmarks, a test) holds on to an element that is about to go.
      <div aria-busy="true" className="grid min-h-90 content-start gap-4">
        <div className="flex items-center gap-3">{head}</div>
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {[0, 1, 2].map((index) => (
            <Card key={index} className={cn('overflow-hidden', index > 0 && 'max-sm:hidden', index > 1 && 'max-lg:hidden')}>
              <Skeleton className="h-36 rounded-none" />
              <div className="grid gap-3 p-4">
                <Skeleton className="w-24" />
                <Skeleton className="h-5 w-3/4" />
                <Skeleton className="w-1/2" />
              </div>
            </Card>
          ))}
        </div>
      </div>
    );
  }

  if (ordered.length === 0) {
    const last = jobs?.jobs.find((job) => job.state !== 'running');
    const studioHere = studio && !isStudioMissing(studio);
    return (
      <section aria-labelledby="home-running" className="grid gap-4">
        <div className="flex items-center gap-3">{head}</div>
        <Card className="flex flex-col gap-4 p-5 md:flex-row md:items-center">
          <span aria-hidden="true" className="grid size-10 shrink-0 place-items-center rounded-lg border border-line bg-card text-muted shadow-lift">
            <Check className="size-5" />
          </span>
          <div className="min-w-0 flex-1">
            <p className="text-card-title">Nothing is running.</p>
            <p className="text-secondary text-muted">
              {jobs && !jobs.available
                ? (jobs.reason ?? 'This computer runs no terminal lanes, so it has no runs.')
                : last
                  ? lastRunWords(last, technical)
                  : 'When an agent runs tests, a build or a dev server, it shows here with the last lines it printed.'}
              {studioHere ? ' A design check shows here for half an hour after it ends.' : ''}
            </p>
          </div>
          {technical ? <CodeBlock code="openajar run -- pnpm test" className="md:w-72" /> : null}
        </Card>
      </section>
    );
  }

  return (
    <Slider key={filter} label="Running now" head={head} className="min-h-90">
      {shown.length
        ? shown.map((item, index) => {
            switch (item.type) {
              case 'job':
                return <JobCard key={item.id} org={org} job={item.job} kind={item.kind} follow={index < FOLLOWED} />;
              case 'check':
                return <CheckCard key={item.id} org={org} item={item} />;
              case 'story':
                return <StoryCard key={item.id} org={org} item={item} />;
              case 'lane':
                return <LaneCard key={item.id} org={org} item={item} canWatch={canWatch} />;
            }
          })
        : [
            <Card key="none" className="p-5 text-secondary text-muted">
              Nothing here matches this filter.
            </Card>,
          ]}
    </Slider>
  );
}
