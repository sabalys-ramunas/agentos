/**
 * How it opened (concept): the bring-up as it happened. A sentence first (how long, and from what), then
 * each step on one time scale with its seconds, steps that ran together overlapping, and the steps a
 * prepared, warm or paused machine made unnecessary hatched with what they cost the last time they ran.
 * A cold open of the same repository sits beside it on its own scale, so the saving is a picture. On the
 * right, the app: its command and port, listening with its link, or why not with the end of its log.
 * While it is starting, the step running now grows in ink.
 */
import { Link } from '@tanstack/react-router';
import { ArrowUpRight, Layers } from 'lucide-react';
import type { EnvApp, EnvBringUp, EnvBringUpStep, EnvDetail } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { CopyButton } from '@/components/ui/misc';
import { Status } from '@/components/ui/status';
import { Terminal } from '@/components/ui/terminal';
import { cn } from '@/lib/cn';
import { safeUrl } from '@/lib/format';
import { appStatus, nodeSentence, openedEyebrow, openedWords, otherPortWords, scaleEnd, seconds, skipped, stepName } from './opened-words';
import { shortCommit } from './words';
import './environments.css';

/** The line that says why, marked: the last one that names a port. */
function lastLineMark(tail: string): Record<number, 'warn'> {
  const lines = tail.split('\n');
  for (let index = lines.length - 1; index >= 0; index -= 1) if (/port/i.test(lines[index] ?? '')) return { [index + 1]: 'warn' };
  return {};
}

const pct = (value: number, end: number) => `${Math.min(100, Math.max(0, (value / end) * 100))}%`;

export function OpenedCard({ org, env }: { org: string; env: EnvDetail }) {
  const bringup = env.bringup;
  if (!bringup && !env.prepared && !env.app) return null;
  const words = openedWords(env);
  const starting = env.state === 'starting';
  const aside = !starting || Boolean(env.app);
  return (
    <Card className="overflow-hidden" aria-labelledby="env-opened">
      <div className={cn('grid gap-x-8 gap-y-5 px-5 py-5', aside && 'lg:grid-cols-[minmax(0,1fr)_340px]')}>
        <div className="grid min-w-0 content-start gap-4">
          <div className="grid gap-1">
            <h2 id="env-opened" className="text-caption text-faint">
              {openedEyebrow(env)}
            </h2>
            <p className="text-section-title text-fg">{words.lead}</p>
            {words.detail ? <p className="max-w-[68ch] text-secondary text-muted">{words.detail}</p> : null}
          </div>
          {bringup ? <Steps bringup={bringup} live={starting} /> : null}
        </div>
        {aside ? <aside className="grid min-w-0 content-start gap-5 border-line max-lg:border-t max-lg:pt-5 lg:border-l lg:pl-8">
          {bringup?.cold_s && !starting ? <Against bringup={bringup} /> : null}
          {env.app ? <AppState env={env} app={env.app} /> : starting ? null : <NoApp env={env} />}
          {env.prepared?.used && env.prepared.from ? (
            <p className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-caption text-faint">
              <Layers aria-hidden="true" className="size-4" />
              Built from <span className="font-mono text-code-caption text-muted">{shortCommit(env.prepared.from.commit ?? '')}</span>
              {env.prepared.from.age ? <span>{env.prepared.from.age} ago</span> : null}
              <Link to="/$org/environments/prepared" params={{ org }} className="text-fg underline decoration-line-strong underline-offset-4 hover:decoration-fg">
                Prepared machines
              </Link>
            </p>
          ) : null}
        </aside> : null}
      </div>
      {env.app && !env.app.listening && env.app.log_tail ? (
        <div className="grid gap-2 border-t border-line px-5 py-4">
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h3 className="text-caption text-faint">The end of its output</h3>
            {env.app.job_id ? (
              <Link to="/$org/runs/$id" params={{ org, id: env.app.job_id }} className="text-secondary text-fg underline decoration-line-strong underline-offset-4 hover:decoration-fg">
                Its whole output
              </Link>
            ) : null}
          </div>
          <Terminal lines={env.app.log_tail.split('\n')} label={`The end of ${env.app.command}'s output`} command={env.app.command} heightClass="max-h-64" highlight={lastLineMark(env.app.log_tail)} />
        </div>
      ) : null}
    </Card>
  );
}

/** Each step on one scale: a row per step, a bar from when it began for as long as it took. */
function Steps({ bringup, live }: { bringup: EnvBringUp; live: boolean }) {
  const end = scaleEnd(Math.max(bringup.total_s, live ? (bringup.expected_s ?? 0) : 0, ...bringup.steps.filter((step) => !step.skipped).map((step) => step.at_s + step.seconds)));
  const ticks = [0, end / 2, end];
  const saved = skipped(bringup);
  return (
    <figure className="grid gap-2" aria-label={`The steps of the bring-up, on a scale of ${seconds(end)}`}>
      <div aria-hidden="true" className="grid grid-cols-[136px_minmax(0,1fr)_88px] items-end gap-3 max-sm:grid-cols-[104px_minmax(0,1fr)_72px]">
        <span />
        <span className="relative h-4">
          {ticks.map((tick, index) => (
            <span key={tick} className={cn('absolute bottom-0 font-mono text-code-caption text-faint tabular', index === 0 ? 'left-0' : index === 1 ? 'left-1/2 -translate-x-1/2' : 'right-0')}>
              {tick === 0 ? '0' : seconds(tick)}
            </span>
          ))}
        </span>
        <span />
      </div>
      <ol className="grid gap-1">
        {bringup.steps.map((step) => (
          <StepRow key={step.step} step={step} end={end} running={live && bringup.now?.step === step.step} />
        ))}
      </ol>
      {saved.steps.length ? (
        <figcaption className="flex items-center gap-2 pt-1 text-caption text-faint">
          <span aria-hidden="true" className="env-hatch inline-block h-2 w-6 rounded-sm border border-line" />
          Skipped: {seconds(saved.seconds)} of work it did not need this time.
        </figcaption>
      ) : null}
    </figure>
  );
}

function StepRow({ step, end, running }: { step: EnvBringUpStep; end: number; running: boolean }) {
  const failed = step.ok === false;
  return (
    <li className="group grid min-h-6 grid-cols-[136px_minmax(0,1fr)_88px] items-center gap-3 max-sm:grid-cols-[104px_minmax(0,1fr)_72px]">
      <span className={cn('truncate text-secondary', step.skipped || step.pending ? 'text-faint' : 'text-fg')} title={step.step}>
        {stepName(step.step)}
      </span>
      <span className="relative h-2 rounded-sm bg-chart-grid/40">
        {step.pending ? null : step.skipped ? (
          <span
            className="env-hatch absolute inset-y-0 left-0 right-0 rounded-sm border border-line"
            title={`Skipped thanks to the ${step.skipped}. It took ${seconds(step.seconds)} the last time it ran.`}
          />
        ) : (
          <span
            className={cn('absolute inset-y-0 rounded-sm', failed ? 'border border-warn-line bg-warn-fill' : 'bg-chart-bar-strong', running && 'env-bar-live')}
            style={{ left: pct(step.at_s, end), width: `max(3px, ${pct(step.seconds, end)})` }}
          />
        )}
      </span>
      <span className={cn('text-right font-mono text-code-caption tabular', step.skipped || step.pending ? 'text-faint' : failed ? 'text-warn' : 'text-muted')}>
        {step.pending ? <span className="font-sans text-caption">to come</span> : step.skipped ? <span className="font-sans text-caption">{step.skipped === 'prepared machine' ? 'prepared' : step.skipped.replace(' machine', '')}</span> : running ? `${seconds(step.seconds)} so far` : seconds(step.seconds)}
      </span>
    </li>
  );
}

/** This open against a cold one of the same repository, on one scale. */
function Against({ bringup }: { bringup: EnvBringUp }) {
  const cold = bringup.cold_s ?? 0;
  const end = Math.max(cold, bringup.total_s);
  const times = Math.round(cold / Math.max(0.1, bringup.total_s));
  return (
    <figure className="grid gap-3" aria-label={`This open took ${seconds(bringup.total_s)}, a cold open ${seconds(cold)}`}>
      <figcaption className="text-caption text-faint">Against a cold open of the same repository</figcaption>
      {[
        { label: 'This open', value: bringup.total_s, strong: true },
        { label: 'Cold', value: cold, strong: false },
      ].map((row) => (
        <div key={row.label} className="grid grid-cols-[72px_minmax(0,1fr)_72px] items-center gap-3">
          <span className="text-secondary text-muted">{row.label}</span>
          <span className="relative h-2 rounded-sm bg-chart-grid/40">
            <span className={cn('absolute inset-y-0 left-0 rounded-sm', row.strong ? 'bg-chart-bar-strong' : 'bg-chart-bar')} style={{ width: `max(3px, ${pct(row.value, end)})` }} />
          </span>
          <span className="text-right font-mono text-code-caption text-fg tabular">{seconds(row.value)}</span>
        </div>
      ))}
      {times >= 2 ? <p className="text-secondary text-muted">About {times} times faster.</p> : null}
    </figure>
  );
}

function AppState({ env, app }: { env: EnvDetail; app: EnvApp }) {
  const status = appStatus(app);
  const link = safeUrl(app.link ?? null) ?? (app.link?.startsWith('/') ? app.link : null);
  const other = otherPortWords(app);
  return (
    <section aria-label="The app" className="grid min-w-0 gap-3">
      <div className="flex min-w-0 items-center justify-between gap-3">
        <div className="grid min-w-0 gap-1">
          <span className="text-caption text-faint">The app</span>
          <span className="flex min-w-0 items-center gap-2">
            <code className="truncate font-mono text-code text-fg">{app.command}</code>
            <span className="shrink-0 font-mono text-code-caption text-muted">:{app.port}</span>
          </span>
        </div>
        <Status status={status} />
      </div>
      {app.listening ? (
        <div className="flex flex-wrap items-center justify-between gap-2">
          <span className="text-secondary text-muted">{app.seconds !== undefined ? `Up in ${seconds(app.seconds)} after its command.` : 'It listens.'}</span>
          {link ? (
            <Button asChild size="sm" icon={<ArrowUpRight className="size-4" />}>
              <a href={link} target="_blank" rel="noreferrer" aria-label={`Open the app on port ${app.port}`}>
                Open the app
              </a>
            </Button>
          ) : null}
        </div>
      ) : (
        <div className="grid min-w-0 gap-3">
          {app.why ? <p className="text-secondary text-fg">{nodeSentence(app.why.replace(/\s*\(check it listens[^)]*\)/, ''))}</p> : null}
          {other ? (
            <div className="grid gap-2 rounded-lg border border-line px-3 py-3">
              <p className="text-secondary text-muted">{other.sentence}</p>
              <div className="flex min-w-0 items-center gap-2">
                <span className="grid min-w-0 flex-1">
                  <span className="truncate font-mono text-code-caption text-faint">.agentos/environment.toml</span>
                  <code className="font-mono text-code text-fg">{other.fix}</code>
                </span>
                <CopyButton text={other.fix} label="Copy the line" />
              </div>
            </div>
          ) : null}
          <p className="text-caption text-faint">The open did not fail for this: {env.agent.name} can see it and fix it.</p>
        </div>
      )}
    </section>
  );
}

function NoApp({ env }: { env: EnvDetail }) {
  if (env.prepared?.used === false && env.prepared.why?.includes('has no')) {
    return (
      <section aria-label="The app" className="grid gap-1">
        <span className="text-caption text-faint">The app</span>
        <p className="text-secondary text-muted">
          Nothing was started for it. With a <code className="font-mono text-code-caption text-fg">.agentos/environment.toml</code> in the repository, the next open runs its setup and
          starts the app, and the ones after start from a prepared machine.
        </p>
      </section>
    );
  }
  return null;
}
