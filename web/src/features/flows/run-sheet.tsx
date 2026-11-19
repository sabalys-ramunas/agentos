/**
 * One run, step by step: what went in, what came out, how long it took and an honest word for how
 * it ended. A failed step says why and can be tried again. Raw values wait behind Technical details.
 */
import { ChevronRight, RotateCcw } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';
import { useAccess, useRetryRoutineRun } from '@/api/hooks';
import type { Flow, RoutineCatalog, RoutineRun, RoutineStepRun } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Drawer } from '@/components/ui/dialog';
import { Status } from '@/components/ui/status';
import { StateGlyph } from '@/components/ui/state-badge';
import { cn } from '@/lib/cn';
import { dateTime, elapsed } from '@/lib/format';
import { runStatus } from '@/lib/labels';
import type { StatusLabel } from '@/lib/labels';
import { useTechnical } from '@/lib/technical';
import { OutputCard } from './output-card';

const STEP_STATUS: Record<RoutineStepRun['status'], StatusLabel> = {
  ...runStatus,
  succeeded_verified: { label: 'Done', tone: 'ok' },
  succeeded_unverified: { label: 'Done', tone: 'neutral' },
  skipped: { label: 'Skipped', tone: 'idle' },
  waiting: { label: 'Waiting', tone: 'neutral' },
  stopped: { label: 'Stopped early', tone: 'idle' },
};

function Values({ title, values, technical }: { title: string; values: Record<string, unknown> | null; technical: boolean }) {
  const entries = Object.entries(values ?? {}).filter(([, value]) => value !== null && value !== undefined);
  if (!entries.length) return null;
  return (
    <div className="min-w-0">
      <p className="silk mb-1">{title}</p>
      {technical ? (
        <pre className="mono overflow-x-auto rounded-md bg-inset p-2 text-caption">{JSON.stringify(values, null, 2)}</pre>
      ) : (
        <dl className="grid gap-1">
          {entries.map(([key, value]) => (
            <div key={key} className="grid grid-cols-[auto_minmax(0,1fr)] gap-3 text-secondary">
              <dt className="text-faint">{key.replaceAll('_', ' ')}</dt>
              <dd className="text-right break-words">{Array.isArray(value) ? value.join(', ') : String(value)}</dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  );
}

function StepRow({ step, longest, onRetry, retrying, canRetry }: { step: RoutineStepRun; longest: number; onRetry: () => void; retrying: boolean; canRetry: boolean }) {
  const failed = step.status === 'failed';
  const [open, setOpen] = useState(failed);
  const [technical] = useTechnical();
  const took = step.started_at && step.finished_at ? Date.parse(step.finished_at) - Date.parse(step.started_at) : null;
  const detail = step.input || step.output || step.note || step.items;
  return (
    <li className={cn('border-b border-line last:border-b-0', step.status === 'skipped' && 'opacity-60')}>
      <button type="button" disabled={!detail} aria-expanded={open} onClick={() => setOpen(!open)} className="flex w-full items-center gap-2 py-3 text-left">
        <ChevronRight aria-hidden="true" className={cn('size-4 shrink-0 text-faint transition-transform duration-(--duration-fast) ease-standard', open && 'rotate-90', !detail && 'invisible')} />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-body">{step.label.charAt(0).toUpperCase() + step.label.slice(1)}</span>
          {step.short ? <span className="block truncate text-caption text-faint">{step.short}</span> : null}
        </span>
        {took !== null ? (
          <span className="flex w-20 shrink-0 flex-col items-end gap-1">
            <span className="mono tabular text-caption text-faint">{elapsed(took)}</span>
            <span aria-hidden="true" className="block h-0.5 rounded-full bg-fg/40" style={{ width: `${Math.max(6, (took / longest) * 100)}%` }} />
          </span>
        ) : null}
        <Status status={STEP_STATUS[step.status]} className="w-24 shrink-0 justify-end" />
      </button>
      {open && detail ? (
        <div className="grid gap-3 pb-4 pl-6">
          {step.note ? (
            <p className="flex items-start gap-2 text-secondary text-pretty">
              {failed ? <StateGlyph state="stuck" size={14} className="translate-y-0.5" /> : null}
              {step.note}
            </p>
          ) : null}
          {step.items?.length ? (
            <ul className="grid gap-1 text-secondary text-muted">
              {step.items.map((item) => (
                <li key={item.label}>{item.label}</li>
              ))}
            </ul>
          ) : null}
          <div className="grid gap-3 sm:grid-cols-2">
            <Values title="Went in" values={step.input} technical={technical} />
            <Values title="Came out" values={step.output} technical={technical} />
          </div>
          {failed && canRetry ? (
            <div>
              <Button size="sm" icon={<RotateCcw className="size-3" />} loading={retrying} onClick={onRetry}>
                Try this step again
              </Button>
              <p className="mt-1 text-caption text-faint">It starts from here. Steps that already worked are kept, and nothing is sent twice.</p>
            </div>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}

export function RunSheet({ org, flow, run, catalog, site, onClose }: { org: string; flow: Flow; run: RoutineRun | null; catalog?: RoutineCatalog; site: string; onClose: () => void }) {
  const retry = useRetryRoutineRun(org, flow.id);
  const canRetry = useAccess(org).can('lanes.operate');
  const [technical] = useTechnical();
  void catalog;
  if (!run) return <Drawer open={false} onOpenChange={onClose} title="Run"><span /></Drawer>;
  const failedStep = run.steps.find((step) => step.status === 'failed');
  const start = Date.parse(run.started_at);
  const end = run.finished_at ? Date.parse(run.finished_at) : null;
  const longest = Math.max(...run.steps.map((step) => (step.started_at && step.finished_at ? Date.parse(step.finished_at) - Date.parse(step.started_at) : 0)), 1);
  return (
    <Drawer open onOpenChange={(next) => !next && onClose()} title={failedStep ? `Stopped at "${failedStep.label}"` : (run.result ?? 'This run')} description={`${dateTime(run.started_at)} · ${run.cause}${end ? ` · took ${elapsed(end - start)}` : ''}`}>
      <div className="grid gap-4">
        <Status status={runStatus[run.status]} />
        {run.output ? (
          <div className="pointer-events-none">
            <OutputCard run={run} site={site} onOpen={() => undefined} />
          </div>
        ) : null}
        <ol aria-label="Steps of this run">
          {run.steps.map((step) => (
            <StepRow
              key={step.step}
              step={step}
              longest={longest}
              canRetry={canRetry}
              retrying={retry.isPending}
              onRetry={() => retry.mutate(run.id, { onSuccess: () => { toast('It worked this time'); onClose(); } })}
            />
          ))}
        </ol>
        {technical ? <p className="mono text-caption break-all text-faint">{run.id} · v{run.version} · ${run.cost.usd.toFixed(2)}</p> : null}
      </div>
    </Drawer>
  );
}
