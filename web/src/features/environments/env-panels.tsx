/**
 * The three panels beside the live screen: the time left and the cost so far, what the agent is doing (its last
 * steps in plain words), and its code (the branch, what was pushed, a pull request). On the night band they are
 * dark glass; on a phone, where the band holds only the screen, they are plain cards under it.
 */
import { ArrowUpRight, GitBranch } from 'lucide-react';
import type { ReactNode } from 'react';
import type { EnvDetail } from '@/api/types';
import { Card } from '@/components/ui/card';
import { StatusDot } from '@/components/ui/status';
import { NoValue } from '@/components/ui/table';
import { cn } from '@/lib/cn';
import { clock, dateTime, plural } from '@/lib/format';
import { toolPlainTitle } from '@/lib/plain-node';
import { unsavedWords } from './close-flow';
import { SHORT_MS, ageOf, callWords, euro, hourly, isOpen, leftOf, pullRequestLink } from './words';

function Panel({ glass, className, children, label }: { glass: boolean; className?: string; children: ReactNode; label?: string }) {
  return glass ? (
    <section aria-label={label} className={cn('glass-night rounded-xl', className)}>
      {children}
    </section>
  ) : (
    <Card>
      <section aria-label={label} className={className}>
        {children}
      </section>
    </Card>
  );
}

/** A span as a big number and a small unit: "1 h" and "45 min". */
export function spanParts(ms: number): { value: string; unit?: string } {
  const minutes = Math.floor(Math.max(0, ms) / 60_000);
  if (minutes < 1) return { value: 'Under 1', unit: 'min' };
  if (minutes < 60) return { value: String(minutes), unit: 'min' };
  const hours = Math.floor(minutes / 60);
  if (hours >= 48) return { value: String(Math.round(hours / 24)), unit: 'days' };
  return { value: `${hours} h`, unit: minutes % 60 ? `${minutes % 60} min` : undefined };
}

function Stat({ label, value, unit, hint, tone }: { label: string; value: ReactNode; unit?: string; hint?: string; tone?: 'warn' }) {
  return (
    <div className="grid min-w-0 content-start gap-1" title={hint}>
      <span className="text-secondary text-faint">{label}</span>
      <span className={cn('text-stat whitespace-nowrap tabular', tone === 'warn' ? 'text-warn' : 'text-fg')}>
        {value}
        {unit ? <span className="ml-1 text-secondary font-medium text-faint">{unit}</span> : null}
      </span>
    </div>
  );
}

export function TimePanel({ env, now, glass }: { env: EnvDetail; now: number; glass: boolean }) {
  const open = isOpen(env);
  const left = leftOf(env, now);
  const parts = left !== null && left > 0 ? spanParts(left) : null;
  const ran = spanParts(ageOf(env, now));
  return (
    <Panel glass={glass} label={open ? 'Time and cost' : 'How long it ran and its cost'} className="grid grid-cols-2 gap-4 px-5 py-4">
      {open ? (
        <Stat
          label="Time left"
          value={env.state === 'closing' ? 'Closing' : left === null ? <NoValue /> : parts ? parts.value : 'Ends now'}
          unit={parts?.unit}
          tone={left !== null && left < SHORT_MS ? 'warn' : undefined}
          hint={`The node closes it at ${clock(env.expires_at)} at the latest`}
        />
      ) : (
        <Stat label="Ran for" value={ran.value} unit={ran.unit} hint={env.closed_at ? `Closed ${dateTime(env.closed_at)}` : undefined} />
      )}
      <Stat label={open ? 'Cost so far' : 'Cost'} value={euro(env.cost_eur)} hint={hourly(env.price_hourly_eur)} />
    </Panel>
  );
}

export function DoingPanel({ env, glass, steps = 4 }: { env: EnvDetail; glass: boolean; steps?: number }) {
  const open = isOpen(env);
  const shown = env.calls.slice(0, steps);
  return (
    <Panel glass={glass} label={open ? 'What it is doing' : 'What it did last'} className="px-5 pt-4 pb-2">
      <h2 className="text-card-title text-fg">{open ? 'What it is doing' : 'What it did last'}</h2>
      {shown.length ? (
        <ol className="mt-2 divide-y divide-line border-t border-line">
          {shown.map((call, index) => {
            const words = callWords(call.tool, call.summary, toolPlainTitle);
            return (
              <li key={`${call.at}:${index}`} className="grid grid-cols-[48px_minmax(0,1fr)] gap-2 py-2">
                <time dateTime={call.at} title={dateTime(call.at)} className="text-secondary text-faint tabular">
                  {clock(call.at)}
                </time>
                <span className="grid min-w-0">
                  <span className="text-secondary text-fg">
                    {call.ok ? null : <StatusDot tone="bad" className="mr-2 translate-y-px" />}
                    {words.title}
                    {call.ok ? null : <span className="sr-only">, it failed</span>}
                  </span>
                  <span className={cn('truncate text-caption', call.ok ? 'text-faint' : 'text-bad')} title={words.detail}>
                    {words.detail}
                  </span>
                </span>
              </li>
            );
          })}
        </ol>
      ) : (
        <p className="mt-1 pb-2 text-secondary text-muted">{open ? 'Nothing yet. Its steps show here as it works.' : 'It made no call.'}</p>
      )}
    </Panel>
  );
}

export function CodePanel({ env, glass }: { env: EnvDetail; glass: boolean }) {
  const git = env.git;
  const branch = git?.branch ?? env.branch;
  // a machine that never started cloned nothing: there is no code to speak of
  if (!env.repo || !branch || env.state === 'failed') return null;
  const pushes = git?.pushed.length ?? 0;
  const link = pushes ? pullRequestLink(env.repo, branch) : null;
  const unsaved = env.unsaved && (env.unsaved.dirty_files.length || env.unsaved.unpushed.length) ? env.unsaved : null;
  return (
    <Panel glass={glass} label="Code" className="grid gap-3 px-5 py-4">
      <p className="flex min-w-0 items-center gap-2">
        <GitBranch aria-hidden="true" className="size-4 shrink-0 text-faint" />
        <span className="truncate font-mono text-code text-fg" title={branch}>
          {branch}
        </span>
      </p>
      <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 text-secondary">
        <span className="text-faint">{pushes ? `Pushed ${pushes === 1 ? 'once' : plural(pushes, 'time')}` : 'Nothing pushed yet'}</span>
        {link ? (
          <a href={link} target="_blank" rel="noreferrer" className="link inline-flex items-center gap-1 font-medium text-fg">
            Open a pull request
            <ArrowUpRight aria-hidden="true" className="size-4" />
          </a>
        ) : null}
      </div>
      {unsaved && isOpen(env) ? (
        <p className="flex items-start gap-2 text-secondary text-warn">
          <StatusDot tone="warn" className="translate-y-1" />
          Not pushed yet: {unsavedWords(unsaved)}.
        </p>
      ) : null}
    </Panel>
  );
}
