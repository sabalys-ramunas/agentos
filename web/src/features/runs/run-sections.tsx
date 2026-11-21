/** The run page's side: what failed (read from the output) and what the machine recorded about the run. */
import { ArrowRight, Check, CircleAlert, Clock, FlaskConical } from 'lucide-react';
import type { ReactNode, Ref } from 'react';
import type { Job } from '@/api/types';
import { Card, CardHeader } from '@/components/ui/card';
import { StatusDot } from '@/components/ui/status';
import { clock, dateTime, plural } from '@/lib/format';
import type { Tone } from '@/lib/labels';
import { plain } from './failures';
import type { Stream } from './failures';
import type { RunText } from './output-card';
import { JOB_STATUS, bytes } from './words';

const RUNNER: Record<string, string> = { pytest: 'pytest', vitest: 'Vitest', jest: 'Jest', swift: 'swift test', go: 'go test' };

interface SectionProps {
  ref?: Ref<HTMLElement>;
  job: Job;
  text: RunText;
  onJump: (stream: Stream, line: number) => void;
}

function Calm({ icon, title, children }: { icon: ReactNode; title: string; children?: ReactNode }) {
  return (
    <div className="flex gap-3 px-5 py-4">
      <span aria-hidden="true" className="mt-px grid size-6 shrink-0 place-items-center rounded-md border border-line text-muted [&_svg]:size-3.5">
        {icon}
      </span>
      <div className="grid min-w-0 gap-1">
        <p className="text-label text-fg">{title}</p>
        {children ? <p className="text-secondary text-muted">{children}</p> : null}
      </div>
    </div>
  );
}

export function FailuresCard({ ref, job, text, trimmed, onJump }: SectionProps & { trimmed: boolean }) {
  const tests = job.tests;
  const failures = text.failures ?? [];
  const count = tests?.failed ?? 0;
  let body: ReactNode;
  if (!tests) {
    body =
      job.state === 'running' ? (
        <Calm icon={<Clock />} title="Nothing to read yet">
          Failed tests are read from the runner&apos;s summary, which it prints when it ends.
        </Calm>
      ) : (
        <Calm icon={<FlaskConical />} title="No test summary">
          openajar found no test runner&apos;s summary in this output (pytest, Vitest, Jest, swift test or go test), so no tests are listed.
          {job.state === 'failed' ? ' The output shows what went wrong.' : ''}
        </Calm>
      );
  } else if (count === 0) {
    body = (
      <Calm icon={<Check className="text-ok" />} title="Nothing failed">
        {tests.passed.toLocaleString('en')} passed{tests.skipped ? ` and ${tests.skipped} skipped` : ''}, as {RUNNER[tests.runner]} reported.
      </Calm>
    );
  } else if (!failures.length) {
    body = (
      <Calm icon={<CircleAlert className="text-bad" />} title={`${plural(count, 'test')} failed`}>
        {RUNNER[tests.runner]} says so in its summary, but the lines about each failure are not in a form openajar reads
        {trimmed ? ', or they were in the part of the output this page let go' : ''}. Filter the output for them.
      </Calm>
    );
  } else {
    body = (
      <>
        <ol className="divide-y divide-line">
          {failures.map((failure, index) => (
            <li key={`${failure.file ?? ''}:${failure.name}:${index}`} className="grid gap-1 px-5 py-4">
              <span className="min-w-0 text-label [overflow-wrap:anywhere] text-bad">{failure.name}</span>
              {failure.file ? (
                <span title={failure.file} className="truncate font-mono text-code-caption text-muted">
                  {failure.file}
                  {failure.line ? `:${failure.line}` : ''}
                </span>
              ) : null}
              {failure.message ? <p className="line-clamp-3 font-mono text-code-caption [overflow-wrap:anywhere] text-fg">{failure.message}</p> : null}
              <button
                type="button"
                onClick={() => onJump(failure.at.stream, failure.at.line)}
                className="group mt-1 inline-flex w-fit items-center gap-1 rounded-sm text-secondary font-medium text-muted hover:text-fg"
              >
                Show in the output
                <ArrowRight aria-hidden="true" className="size-3.5 transition-transform duration-(--duration-fast) group-hover:translate-x-0.5" />
                <span className="sr-only">: {failure.name}</span>
              </button>
            </li>
          ))}
          {count > failures.length ? (
            <li className="px-5 py-3 text-secondary text-muted">
              {plural(count - failures.length, 'more failure')} in the summary had no lines openajar could read.
            </li>
          ) : null}
        </ol>
        <p className="border-t border-line px-5 py-3 text-secondary text-faint">Read from the {RUNNER[tests.runner]} output.</p>
      </>
    );
  }
  return (
    <section ref={ref} aria-labelledby="run-failed" className="scroll-mt-20">
      <Card>
        <CardHeader
          title={<span id="run-failed">What failed</span>}
          meta={tests && count ? (job.state === 'running' ? `${count} so far` : plural(count, 'test')) : undefined}
        />
        {body}
      </Card>
    </section>
  );
}

interface Step {
  key: string;
  tone?: Tone;
  at?: string | null;
  title: ReactNode;
  detail?: ReactNode;
  action?: ReactNode;
}

/**
 * What the machine recorded: who started it, what was sent, the first failure in the output, how it
 * ended, and its note. The agent's own steps before and after are not recorded, and the card says so.
 */
export function HappenedCard({ ref, job, text, onJump }: SectionProps) {
  const steps: Step[] = [];
  const sync = job.sync;
  if (sync) {
    steps.push({
      key: 'sync',
      title: `Sent ${plural(sync.changed, 'changed file')} to ${job.node}`,
      detail: (
        <>
          {sync.files.toLocaleString('en')} files in <span className="font-mono text-code-caption">{sync.dest}</span>
          {sync.deleted ? `, ${sync.deleted} deleted` : ''}, {bytes(sync.bytes)}
        </>
      ),
    });
  }
  steps.push({
    key: 'start',
    at: job.started_at,
    title: (
      <>
        {job.started_by ?? 'Someone the machine did not record'} started <span className="font-mono text-code">{job.command}</span>
      </>
    ),
    detail: (
      <>
        in <span className="font-mono text-code-caption">{job.lane}</span>
        {job.cwd ? (
          <>
            , in <span className="font-mono text-code-caption">{job.cwd}</span>
          </>
        ) : null}
      </>
    ),
  });
  const first = text.first;
  if (first && (job.state === 'failed' || (job.tests?.failed ?? 0) > 0 || job.state === 'running')) {
    const lines = first.stream === 'stdout' ? text.stdout : text.stderr;
    steps.push({
      key: 'first',
      title: 'The first failure in the output',
      detail: <span className="line-clamp-2 font-mono text-code-caption [overflow-wrap:anywhere]">{plain(lines[first.line] ?? '').trim()}</span>,
      action: (
        <button type="button" onClick={() => onJump(first.stream, first.line)} className="justify-self-start text-secondary font-medium text-muted underline-offset-2 hover:text-fg hover:underline">
          Show line {first.line + 1}
        </button>
      ),
    });
  }
  if (job.state === 'running') {
    steps.push({ key: 'end', tone: 'live', title: 'Running now', detail: `It stops on its own after ${Math.round(job.timeout_s / 60)} min.` });
  } else {
    const status = JOB_STATUS[job.state];
    steps.push({
      key: 'end',
      tone: status.tone,
      at: job.finished_at,
      title:
        job.state === 'passed'
          ? 'It passed, with exit code 0'
          : job.state === 'failed'
            ? job.exit_code !== null
              ? `It failed, with exit code ${job.exit_code}`
              : `It failed${job.signal ? `, stopped by ${job.signal}` : ''}`
            : status.label,
      detail: job.note ? `The machine said: ${job.note}` : undefined,
    });
  }

  return (
    <section ref={ref} aria-labelledby="run-happened" className="scroll-mt-20">
      <Card>
        <CardHeader title={<span id="run-happened">What happened</span>} />
        <ol className="divide-y divide-line px-5">
          {steps.map((step) => (
            <li key={step.key} className="grid grid-cols-[44px_minmax(0,1fr)] gap-3 py-3">
              {step.at ? (
                <time dateTime={step.at} title={dateTime(step.at)} className="text-body text-muted tabular">
                  {clock(step.at)}
                </time>
              ) : (
                <span aria-hidden="true" />
              )}
              <div className="grid min-w-0 gap-1">
                <p className="flex min-w-0 items-baseline gap-2 text-body [overflow-wrap:anywhere] text-fg">
                  {step.tone ? <StatusDot tone={step.tone} className="shrink-0 self-center" /> : null}
                  <span className="min-w-0">{step.title}</span>
                </p>
                {step.detail ? <div className="text-secondary text-muted">{step.detail}</div> : null}
                {step.action}
              </div>
            </li>
          ))}
        </ol>
        <p className="border-t border-line px-5 py-3 text-secondary text-faint">
          As the machine recorded it. What an agent did before or after it, such as its edits and its next steps, is not
          recorded, so it is not shown.
        </p>
      </Card>
    </section>
  );
}
