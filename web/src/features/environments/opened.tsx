/**
 * How it opened: the bring-up as it happened. A sentence first (how long, and from what), then each step
 * on one time scale with its seconds, steps that ran together overlapping, and the steps a prepared, warm
 * or paused machine made unnecessary hatched with what they took the last time they ran. The same open
 * without those steps sits beside it on its own scale, so the saving is a picture. On the right, the app:
 * its command and port, listening with its link, or why not with the end of its log. While it starts the
 * steps running now grow in ink. Once it is up, it folds to one line that opens again, unless the app did
 * not come up or the open failed.
 */
import { Link } from '@tanstack/react-router';
import { ArrowUpRight, ChevronDown, Layers } from 'lucide-react';
import { useId, useState } from 'react';
import type { EnvApp, EnvBringUp, EnvDetail } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { CopyButton } from '@/components/ui/misc';
import { Status } from '@/components/ui/status';
import { Terminal } from '@/components/ui/terminal';
import { cn } from '@/lib/cn';
import { safeUrl } from '@/lib/format';
import { appStatus, nodeSentence, openedEyebrow, openedWords, otherPortWords, scaleEnd, seconds, skipped, stepName, stepViews, withoutSkips } from './opened-words';
import type { StepView } from './opened-words';
import { shortCommit } from './words';
import './environments.css';

/** The line that says why, marked: the last one that names a port. */
function lastLineMark(tail: string): Record<number, 'warn'> {
  const lines = tail.split('\n');
  for (let index = lines.length - 1; index >= 0; index -= 1) if (/port/i.test(lines[index] ?? '')) return { [index + 1]: 'warn' };
  return {};
}

const pct = (value: number, end: number) => `${Math.min(100, Math.max(0, (value / end) * 100))}%`;

/** Open by default while it starts, when it failed, and when the app did not come up: then it is the news. */
const unfolded = (env: EnvDetail) => env.state === 'starting' || env.state === 'failed' || Boolean(env.app && !env.app.listening);

export function OpenedCard({ org, env }: { org: string; env: EnvDetail }) {
  const bringup = env.bringup;
  const [open, setOpen] = useState(() => unfolded(env));
  const body = useId();
  if (!bringup && !env.prepared && !env.app) return null;
  const words = openedWords(env);
  const starting = env.state === 'starting';
  const shown = open || starting;
  return (
    <Card className="@container overflow-hidden" aria-labelledby="env-opened">
      {shown ? null : <FoldedLine env={env} lead={words.lead} controls={body} onOpen={() => setOpen(true)} />}
      {shown ? (
        <div id={body}>
          <Unfolded org={org} env={env} words={words} bringup={bringup ?? null} onFold={starting ? undefined : () => setOpen(false)} controls={body} />
          {env.app && !env.app.listening && env.app.log_tail ? <AppLog org={org} app={env.app} /> : null}
        </div>
      ) : null}
    </Card>
  );
}

/** The fold: the sentence, the app's state and what it skipped, on one line that opens the rest. */
function FoldedLine({ env, lead, controls, onOpen }: { env: EnvDetail; lead: string; controls: string; onOpen: () => void }) {
  const saved = env.bringup ? skipped(env.bringup).seconds : 0;
  return (
    <button
      type="button"
      aria-expanded={false}
      aria-controls={controls}
      onClick={onOpen}
      className="group flex min-h-14 w-full min-w-0 flex-wrap items-center gap-x-4 gap-y-1 px-5 py-3 text-left transition-colors duration-(--duration-fast) hover:bg-hover"
    >
      <h2 id="env-opened" className="sr-only">
        {openedEyebrow(env)}
      </h2>
      <span className="text-label text-fg">{lead}</span>
      {env.app ? (
        <span className="inline-flex min-w-0 items-center gap-2 text-secondary text-muted">
          <Status status={appStatus(env.app)} />
          <code className="truncate font-mono text-code-caption">
            {env.app.command} :{env.app.port}
          </code>
        </span>
      ) : null}
      {saved > 0 ? (
        <span className="inline-flex items-center gap-2 text-secondary text-muted">
          <span aria-hidden="true" className="env-hatch inline-block h-2 w-5 rounded-sm border border-line" />
          {seconds(saved)} skipped
        </span>
      ) : null}
      <span className="ml-auto inline-flex items-center gap-1 text-secondary text-muted group-hover:text-fg">
        The steps
        <ChevronDown aria-hidden="true" className="size-4 transition-transform duration-(--duration-fast) group-hover:translate-y-px" />
      </span>
    </button>
  );
}

function Unfolded({ org, env, words, bringup, onFold, controls }: { org: string; env: EnvDetail; words: { lead: string; detail: string | null }; bringup: EnvBringUp | null; onFold?: () => void; controls: string }) {
  const starting = env.state === 'starting';
  const without = bringup && !starting ? withoutSkips(bringup) : null;
  const built = Boolean(env.prepared?.used && env.prepared.from);
  // the side holds the saving, the app and where it was built from: without any of them it is not drawn
  const aside = Boolean(without || env.app || built || (!starting && noAppWords(env)));
  return (
    <div className={cn('grid gap-x-8 gap-y-5 px-5 py-5', aside && '@3xl:grid-cols-[minmax(0,1fr)_300px] @5xl:grid-cols-[minmax(0,1fr)_340px]')}>
      <div className="grid min-w-0 content-start gap-4">
        <div className="flex min-w-0 items-start gap-4">
          <div className="grid min-w-0 flex-1 gap-1">
            <h2 id="env-opened" className="text-caption text-faint">
              {openedEyebrow(env)}
            </h2>
            <p className="text-section-title text-fg">{words.lead}</p>
            {words.detail ? <p className="max-w-[68ch] text-secondary text-muted">{words.detail}</p> : null}
          </div>
          {onFold ? (
            <Button variant="ghost" size="sm" aria-expanded aria-controls={controls} onClick={onFold} icon={<ChevronDown className="size-4 rotate-180" />}>
              Fold
            </Button>
          ) : null}
        </div>
        {bringup ? <Steps env={env} bringup={bringup} live={starting} /> : null}
      </div>
      {aside ? (
        <aside className="grid min-w-0 content-start gap-5 border-line @max-3xl:border-t @max-3xl:pt-5 @3xl:border-l @3xl:pl-8">
          {bringup && without ? <Against total={bringup.total_s ?? 0} without={without} /> : null}
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
        </aside>
      ) : null}
    </div>
  );
}

function AppLog({ org, app }: { org: string; app: EnvApp }) {
  const tail = app.log_tail ?? '';
  return (
    <div className="grid gap-2 border-t border-line px-5 py-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-caption text-faint">The end of its output</h3>
        {app.job_id ? (
          <Link to="/$org/runs/$id" params={{ org, id: app.job_id }} className="text-secondary text-fg underline decoration-line-strong underline-offset-4 hover:decoration-fg">
            Its whole output
          </Link>
        ) : null}
      </div>
      <Terminal lines={tail.split('\n')} label={`The end of ${app.command}'s output`} command={app.command} heightClass="max-h-64" highlight={lastLineMark(tail)} />
    </div>
  );
}

/** Each step on one scale: a row per step, a bar from when it began for as long as it took. */
function Steps({ env, bringup, live }: { env: EnvDetail; bringup: EnvBringUp; live: boolean }) {
  const views = stepViews(env);
  const ran = views.filter((step) => !step.skipped && !step.pending);
  const end = scaleEnd(Math.max(1, bringup.total_s ?? 0, ...ran.map((step) => step.at_s + step.seconds)));
  const ticks = [0, end / 2, end];
  const saved = skipped(bringup);
  return (
    <figure className="grid gap-2" aria-label={`The steps of the bring-up, on a scale of ${seconds(end)}`}>
      <div aria-hidden="true" className="grid grid-cols-[136px_minmax(0,1fr)_88px] items-end gap-3 max-sm:grid-cols-[104px_minmax(0,1fr)_72px]">
        <span />
        <span className="relative h-4">
          {ticks.map((tick, index) => (
            <span key={tick} className={cn('absolute bottom-0 font-mono text-code-caption text-faint tabular', index === 0 ? 'left-0' : index === 1 ? 'left-1/2 -translate-x-1/2 max-sm:hidden' : 'right-0')}>
              {tick === 0 ? '0' : seconds(tick)}
            </span>
          ))}
        </span>
        <span />
      </div>
      <ol className="grid gap-1">
        {views.map((step) => (
          <StepRow key={step.step} step={step} end={end} running={live && Boolean(step.running)} />
        ))}
      </ol>
      {saved.steps.length ? (
        <figcaption className="flex items-center gap-2 pt-1 text-caption text-faint">
          <span aria-hidden="true" className="env-hatch inline-block h-2 w-6 rounded-sm border border-line" />
          {saved.seconds > 0 ? `Skipped: ${seconds(saved.seconds)} of work it did not need this time.` : 'Skipped: work it did not need this time.'}
        </figcaption>
      ) : null}
    </figure>
  );
}

function stepSeconds(step: StepView, running: boolean) {
  if (step.pending) return <span className="font-sans text-caption">to come</span>;
  if (step.skipped) return <span className="font-sans text-caption">{step.skipped === 'prepared machine' ? 'prepared' : step.skipped.replace(' machine', '')}</span>;
  return running ? `${seconds(step.seconds)} so far` : seconds(step.seconds);
}

function StepRow({ step, end, running }: { step: StepView; end: number; running: boolean }) {
  const failed = step.ok === false;
  const last = step.saved_s ?? (step.seconds || null);
  return (
    <li className="group grid min-h-6 grid-cols-[136px_minmax(0,1fr)_88px] items-center gap-3 max-sm:grid-cols-[104px_minmax(0,1fr)_72px]">
      <span className={cn('truncate text-secondary', step.skipped || step.pending ? 'text-faint' : 'text-fg')} title={step.step}>
        {stepName(step.step)}
      </span>
      <span className="relative h-2 rounded-sm bg-chart-grid/40">
        {step.pending ? null : step.skipped ? (
          <span className="env-hatch absolute inset-y-0 left-0 right-0 rounded-sm border border-line" title={`Skipped thanks to the ${step.skipped}.${last ? ` It took ${seconds(last)} the last time it ran.` : ''}`} />
        ) : (
          <span
            className={cn('absolute inset-y-0 rounded-sm', failed ? 'border border-warn-line bg-warn-fill' : 'bg-chart-bar-strong', running && 'env-bar-live')}
            style={{ left: pct(step.at_s, end), width: `max(3px, ${pct(step.seconds, end)})` }}
          />
        )}
      </span>
      <span className={cn('text-right font-mono text-code-caption tabular', step.skipped || step.pending ? 'text-faint' : failed ? 'text-warn' : 'text-muted')}>{stepSeconds(step, running)}</span>
    </li>
  );
}

/** This open against the same one with the skipped steps, on one scale. */
function Against({ total, without }: { total: number; without: number }) {
  const times = Math.round(without / Math.max(0.1, total));
  return (
    <figure className="grid gap-3" aria-label={`This open took ${seconds(total)}, about ${seconds(without)} with the steps it skipped`}>
      <figcaption className="text-caption text-faint">Against the same open with every step</figcaption>
      {[
        { label: 'This open', value: total, strong: true },
        { label: 'Every step', value: without, strong: false },
      ].map((row) => (
        <div key={row.label} className="grid grid-cols-[80px_minmax(0,1fr)_72px] items-center gap-3">
          <span className="text-secondary text-muted">{row.label}</span>
          <span className="relative h-2 rounded-sm bg-chart-grid/40">
            <span className={cn('absolute inset-y-0 left-0 rounded-sm', row.strong ? 'bg-chart-bar-strong' : 'bg-chart-bar')} style={{ width: `max(3px, ${pct(row.value, without)})` }} />
          </span>
          <span className="text-right font-mono text-code-caption text-fg tabular">{row.strong ? seconds(row.value) : `~${seconds(row.value)}`}</span>
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
                  <span className="truncate font-mono text-code-caption text-faint">.openajar/environment.toml</span>
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

/** Why nothing was started, when that is worth saying: the repository has no environment file. */
function noAppWords(env: EnvDetail): boolean {
  return Boolean(env.prepared?.used === false && env.prepared.why?.includes('has no'));
}

function NoApp({ env }: { env: EnvDetail }) {
  if (!noAppWords(env)) return null;
  return (
    <section aria-label="The app" className="grid gap-1">
      <span className="text-caption text-faint">The app</span>
      <p className="text-secondary text-muted">
        Nothing was started for it. With a <code className="font-mono text-code-caption text-fg">.openajar/environment.toml</code> in the repository, the next open runs its setup and
        starts the app, and the ones after start from a prepared machine.
      </p>
    </section>
  );
}
