/**
 * Problems, beside Needs you on Home (Mono): what broke, worst first, each with its kind, the evidence and
 * one link. A failed run shows its test summary and the failing lines it printed; a check its blocker with
 * the crop of the element; a machine that does not answer; an account that is stuck or frozen; the
 * computer frozen. Who is on it is said only when the data says so: a later run of the same command.
 */
import { Link } from '@tanstack/react-router';
import { ArrowRight, Check } from 'lucide-react';
import { useState } from 'react';
import type { ReactNode } from 'react';
import { useJob, useStudioRun } from '@/api/hooks';
import { studioCropUrl } from '@/api/client';
import type { Job, Lane, StudioRunRow, Workspace } from '@/api/types';
import { Count } from '@/components/ui/badge';
import { Card, CardHeader } from '@/components/ui/card';
import { KindEyebrow } from '@/components/ui/kind';
import type { AnyKind } from '@/components/ui/kind';
import { StatusDot } from '@/components/ui/status';
import { EmptyState, Skeleton } from '@/components/ui/states';
import { cn } from '@/lib/cn';
import { plural } from '@/lib/format';
import type { Failure } from './sentence';
import { jobKind, pageName } from './running';

export type Problem =
  | { type: 'run'; id: string; job: Job; failure: Failure }
  | { type: 'check'; id: string; run: StudioRunRow }
  | { type: 'machine'; id: string; name: string }
  | { type: 'account'; id: string; lane: Lane; workspace: Workspace; text: string }
  | { type: 'frozen'; id: string; name: string };

/** Severity: a failure is red, a machine or an account that waits is amber. */
const tone = (problem: Problem) => (problem.type === 'run' || problem.type === 'check' ? 'bad' : 'warn');

/** The lines a run printed that say what failed, from the first part of its output. */
export function failingLines(output: string, max = 3): string[] {
  // eslint-disable-next-line no-control-regex
  const lines = output.split('\n').map((line) => line.replace(/\u001b\[[0-9;]*m/g, '').trimEnd());
  const picked = lines.filter((line) => /(\bFAIL\b|×|✗|AssertionError|Error:|\bfailed\b|FAILED|panic:|expected .+ to)/.test(line) && !/^\s*(Tests?|Test Files)\s/.test(line));
  return picked.slice(0, max);
}

function Row({ problem, title, kind, kindLabel, children, link }: { problem: Problem; title: string; kind: AnyKind; kindLabel?: string; children?: ReactNode; link: ReactNode }) {
  return (
    <li className="flex gap-3 px-5 py-4">
      <span className="flex h-6 shrink-0 items-center">
        <StatusDot tone={tone(problem)} />
      </span>
      <div className="grid min-w-0 flex-1 gap-2">
        <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
          <h3 className="min-w-0 text-label break-words">{title}</h3>
          <KindEyebrow kind={kind} label={kindLabel} />
        </div>
        {children}
        <div>{link}</div>
      </div>
    </li>
  );
}

const more = 'link inline-flex min-h-8 items-center gap-1 text-secondary';
const arrow = <ArrowRight aria-hidden="true" className="size-3.5" />;

function RunProblem({ org, problem }: { org: string; problem: Extract<Problem, { type: 'run' }> }) {
  const { job, failure } = problem;
  const detail = useJob(org, job.id);
  const lines = detail.data ? failingLines(`${detail.data.output.stdout}\n${detail.data.output.stderr}`) : [];
  const tests = job.tests;
  const title = tests?.failed
    ? `${plural(tests.failed, 'test')} failed in ${job.lane_name}`
    : job.state === 'timed_out'
      ? `${job.lane_name} ran out of time`
      : job.state === 'lost'
        ? `${job.lane_name} stopped when its machine did`
        : `${job.lane_name} failed${job.exit_code !== null ? `, exit ${job.exit_code}` : ''}`;
  return (
    <Row
      problem={problem}
      title={title}
      kind={jobKind(job)}
      kindLabel={jobKind(job) === 'lane' ? 'Run' : undefined}
      link={
        <Link to="/$org/runs/$id" params={{ org, id: job.id }} className={more}>
          {tests?.failed ? 'See the failures' : 'See the output'}
          {arrow}
        </Link>
      }
    >
      <p title={`$ ${job.command}${tests ? ` · ${tests.passed} passed, ${tests.failed} failed of ${tests.total}` : ''}`} className="truncate font-mono text-code-caption text-muted">
        $ {job.command}
        {tests ? ` · ${tests.passed} passed, ${tests.failed} failed of ${tests.total}` : ''}
      </p>
      {lines.length ? (
        <div aria-label="What it printed about the failure" className="grid gap-px overflow-hidden rounded-md border border-line font-mono text-code-caption">
          {lines.map((line, index) => (
            <span key={index} title={line} className="truncate bg-diff-del px-3 py-1 text-fg">
              {line.trim()}
            </span>
          ))}
        </div>
      ) : null}
      <p className="text-secondary text-muted">
        {failure.again ? (
          <span className="inline-flex items-center gap-2">
            <StatusDot tone="live" pulse />
            {failure.againBy ? `${failure.againBy} is running it again` : 'It is running again'}
          </span>
        ) : (
          'It has not run again since.'
        )}
      </p>
    </Row>
  );
}

function CheckProblem({ org, problem }: { org: string; problem: Extract<Problem, { type: 'check' }> }) {
  const { run } = problem;
  const detail = useStudioRun(org, run.id);
  const [broken, setBroken] = useState(false);
  const blocker = detail.data?.groups.flatMap((group) => group.findings).find((finding) => finding.severity === 'blocker');
  const crop = blocker && blocker.crop_screens.length && !detail.data?.images_gone && !broken ? studioCropUrl(org, run.id, blocker.id, blocker.crop_screens[0]) : null;
  return (
    <Row
      problem={problem}
      title={`${pageName(run.url)} has ${plural(run.counts.blocker, 'blocker')}`}
      kind="design"
      link={
        <Link to="/$org/checks/$id" params={{ org, id: run.id }} className={more}>
          See the finding
          {arrow}
        </Link>
      }
    >
      <div className="flex min-w-0 gap-3">
        <div className="min-w-0 flex-1">
          {blocker ? (
            <p className="text-secondary break-words text-muted">
              {blocker.fix ?? `${blocker.check} on ${blocker.where.name ?? blocker.where.selector}`}
              {blocker.seen_at.length > 1 ? ` On ${plural(blocker.seen_at.length, 'screen')}.` : ''}
            </p>
          ) : detail.isPending ? (
            <Skeleton className="w-3/4" />
          ) : null}
        </div>
        {crop ? (
          <img
            src={crop}
            alt={`The element with the blocker${blocker?.where.name ? `: ${blocker.where.name}` : ''}, outlined`}
            onError={() => setBroken(true)}
            className="h-20 w-28 shrink-0 rounded-lg border border-line bg-inset object-contain"
          />
        ) : null}
      </div>
    </Row>
  );
}

export function ProblemsPanel({ org, problems, loading, canLift, onLift }: { org: string; problems: Problem[]; loading: boolean; canLift: boolean; onLift: () => void }) {
  const bad = problems.filter((problem) => tone(problem) === 'bad').length;
  const warn = problems.length - bad;
  return (
    <Card role="region" aria-labelledby="home-problems" className="min-w-0">
      <CardHeader
        title={
          <>
            <span id="home-problems">Problems</span>
            {problems.length ? <Count value={problems.length} tone="bad" /> : null}
          </>
        }
        meta={!loading && problems.length === 0 ? 'none' : null}
        action={
          problems.length ? (
            <span role="img" aria-label={[bad ? `${bad} failed` : null, warn ? `${warn} waiting on a person or a machine` : null].filter(Boolean).join(', ')} className="flex h-4 items-stretch gap-1">
              {problems.map((problem) => (
                <i key={problem.id} className={cn('block w-1 rounded-full', tone(problem) === 'bad' ? 'bg-bad-dot' : 'bg-warn-dot')} />
              ))}
            </span>
          ) : null
        }
      />
      {loading && problems.length === 0 ? (
        <div className="grid gap-3 p-5" aria-busy="true">
          <Skeleton className="w-2/3" />
          <Skeleton className="w-1/2" />
        </div>
      ) : problems.length === 0 ? (
        <EmptyState icon={<Check className="text-ok" />} title="Nothing is wrong" description="Every run today passed or ran again, every check is clear of blockers, and every machine answers." className="py-10" />
      ) : (
        <ul className="divide-y divide-line">
          {problems.map((problem) => {
            switch (problem.type) {
              case 'run':
                return <RunProblem key={problem.id} org={org} problem={problem} />;
              case 'check':
                return <CheckProblem key={problem.id} org={org} problem={problem} />;
              case 'machine':
                return (
                  <Row
                    key={problem.id}
                    problem={problem}
                    title={`${problem.name} is not answering`}
                    kind="machine"
                    link={
                      <Link to="/$org/nodes/$id" params={{ org, id: problem.id.replace(/^machine:/, '') }} className={more}>
                        Open the machine
                        {arrow}
                      </Link>
                    }
                  >
                    <p className="text-secondary text-muted">The node has not heard from it. Work in its lanes waits until it is back.</p>
                  </Row>
                );
              case 'account':
                return (
                  <Row
                    key={problem.id}
                    problem={problem}
                    title={problem.text}
                    kind="account"
                    link={
                      <Link to="/$org/lanes/$ws/$name" params={{ org, ws: problem.lane.workspace, name: problem.lane.name }} className={more}>
                        Open the account
                        {arrow}
                      </Link>
                    }
                  />
                );
              case 'frozen':
                return (
                  <Row
                    key={problem.id}
                    problem={problem}
                    title={`${problem.name} is frozen`}
                    kind="machine"
                    link={
                      canLift ? (
                        <button type="button" onClick={onLift} className="link inline-flex min-h-8 items-center gap-1 text-secondary">
                          Lift the freeze
                          <ArrowRight aria-hidden="true" className="size-3.5" />
                        </button>
                      ) : null
                    }
                  >
                    <p className="text-secondary text-muted">No agent may act on it until the freeze is lifted.</p>
                  </Row>
                );
            }
          })}
        </ul>
      )}
    </Card>
  );
}
