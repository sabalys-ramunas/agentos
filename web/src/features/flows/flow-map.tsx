/**
 * The routine as a picture: the trigger, then its steps, joined by wires. Left to right on a desk,
 * top to bottom on a phone. A decision forks, a repeat wraps what it repeats, and each step says
 * what it came to last time, which is the data a person used to n8n looks for.
 */
import { BellRing, Clock, Globe, Hand, Lock, Mail, MousePointerClick, Play, RefreshCw, Split, Timer, Webhook } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import type { ReactNode } from 'react';
import type { RoutineCatalog, RoutineStep, RoutineStepRun, RoutineTrigger } from '@/api/types';
import { SiteMark } from '@/components/brand/site-mark';
import { StateGlyph } from '@/components/ui/state-badge';
import { Status } from '@/components/ui/status';
import { cn } from '@/lib/cn';
import { STEP_WORDS, triggerPhrase } from '@/lib/routines';
import { stepSite } from './routine-words';

const KIND_ICON: Partial<Record<RoutineStep['kind'], LucideIcon>> = { action: Play, wait: Timer, ask: Hand, notify: BellRing, branch: Split, loop: RefreshCw };
export const TRIGGER_ICON: Record<RoutineTrigger['kind'], LucideIcon> = { schedule: Clock, webhook: Webhook, email: Mail, page_change: Globe, site_change: RefreshCw, manual: MousePointerClick };

/** The square at the front of a step: its site, or what kind of step it is. */
export function StepMark({ step, catalog, size = 32 }: { step: RoutineStep; catalog: RoutineCatalog | undefined; size?: 24 | 32 | 40 }) {
  const site = stepSite(step, catalog);
  if (site) return <SiteMark site={site} size={size} tile label={false} className="bg-card" />;
  const Icon = KIND_ICON[step.kind] ?? Clock;
  return (
    <span className="grid shrink-0 place-items-center rounded-lg border border-line bg-card text-muted" style={{ width: size, height: size }}>
      <Icon aria-hidden="true" className="size-4" />
    </span>
  );
}

export function TriggerMark({ trigger, size = 32 }: { trigger: RoutineTrigger; size?: 24 | 32 | 40 }) {
  const Icon = TRIGGER_ICON[trigger.kind];
  return (
    <span className="grid shrink-0 place-items-center rounded-lg bg-fg text-card" style={{ width: size, height: size }}>
      <Icon aria-hidden="true" className="size-4" />
    </span>
  );
}

const capital = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);

interface Lit {
  /** The step a person's pointer is on, here or in the sentence. */
  lit?: string | null;
  onLight?: (id: string | null) => void;
}

function Node({ mark, silk, title, note, locked, status, id, lit, onLight }: { mark: ReactNode; silk: string; title: string; note?: string | null; locked?: boolean; status?: RoutineStepRun['status']; id: string } & Lit) {
  const dim = status === 'skipped';
  return (
    <div
      data-node={id}
      onPointerEnter={() => onLight?.(id)}
      onPointerLeave={() => onLight?.(null)}
      className={cn(
        'relative w-full shrink-0 rounded-xl border bg-card p-3 transition-[border-color,opacity] duration-(--duration-fast) ease-standard md:w-48',
        lit === id ? 'border-fg' : status === 'failed' ? 'border-(--stuck-line)' : status === 'waiting_approval' ? 'border-(--wait-line)' : 'border-line',
        dim && 'opacity-55',
      )}
    >
      <span className="flex items-center justify-between gap-2">
        {mark}
        <span className="silk truncate">{silk}</span>
      </span>
      <span className="mt-2 block text-secondary font-medium text-pretty">{title}</span>
      <span className="mt-1 flex min-h-4 items-center gap-1 text-caption text-faint">
        {status === 'failed' ? <StateGlyph state="stuck" size={14} /> : status === 'waiting_approval' ? <StateGlyph state="waiting" size={14} /> : locked ? <Lock aria-hidden="true" className="size-3 shrink-0" /> : null}
        <span className="truncate">{note ?? (locked ? 'Always asks' : '')}</span>
      </span>
    </div>
  );
}

/** A wire between two steps, with the way it runs. */
function Wire({ short }: { short?: boolean }) {
  return (
    <span aria-hidden="true" className={cn('relative shrink-0 self-center bg-(--board-wire-strong) max-md:h-5 max-md:w-px md:h-px', short ? 'md:w-4' : 'md:w-8')}>
      <span className="absolute size-2 border-t border-r border-(--board-wire-strong) max-md:-bottom-px max-md:-left-[3.5px] max-md:rotate-[135deg] md:-top-[3.5px] md:right-0 md:rotate-45" />
    </span>
  );
}

interface MapProps extends Lit {
  steps: RoutineStep[];
  catalog: RoutineCatalog | undefined;
  /** The newest run, so each step can say what it came to. */
  run?: RoutineStepRun[] | null;
  locked: ReadonlySet<string>;
}

function Steps({ steps, catalog, run, locked, lit, onLight }: MapProps) {
  return (
    <>
      {steps.map((step, index) => {
        const ran = run?.find((item) => item.step === step.id);
        return (
          <div key={step.id} className="flex items-stretch max-md:flex-col md:items-center">
            {index > 0 ? <Wire /> : null}
            {step.kind === 'branch' ? (
              <Fork step={step} catalog={catalog} run={run} locked={locked} lit={lit} onLight={onLight} />
            ) : step.kind === 'loop' ? (
              <div className="rounded-xl border border-dashed border-(--board-line-strong) p-2 md:p-3" data-node={step.id}>
                <p className="silk mb-2 flex items-center gap-2">
                  <RefreshCw aria-hidden="true" className="size-3" />
                  {step.label}
                  {ran?.short ? <span className="normal-case tracking-normal">· {ran.short}</span> : null}
                </p>
                <div className="flex items-stretch max-md:flex-col md:items-center">
                  <Steps steps={step.body} catalog={catalog} run={run} locked={locked} lit={lit} onLight={onLight} />
                </div>
              </div>
            ) : (
              <Node id={step.id} mark={<StepMark step={step} catalog={catalog} />} silk={STEP_WORDS[step.kind].silk} title={capital(step.label)} note={ran?.short} locked={locked.has(step.id)} status={ran?.status} lit={lit} onLight={onLight} />
            )}
          </div>
        );
      })}
    </>
  );
}

function Fork({ step, catalog, run, locked, lit, onLight }: Omit<MapProps, 'steps'> & { step: Extract<RoutineStep, { kind: 'branch' }> }) {
  const arm = (label: string, steps: RoutineStep[]) => (
    <div className="grid gap-1">
      <span className="silk">{label}</span>
      {steps.length ? (
        <div className="flex items-stretch max-md:flex-col md:items-center">
          <Steps steps={steps} catalog={catalog} run={run} locked={locked} lit={lit} onLight={onLight} />
        </div>
      ) : (
        <span className="text-caption text-faint">Carries on</span>
      )}
    </div>
  );
  return (
    <div className="flex items-stretch max-md:flex-col md:items-center">
      <Node id={step.id} mark={<StepMark step={step} catalog={catalog} />} silk="Decide" title={capital(step.label)} note={run?.find((item) => item.step === step.id)?.short} lit={lit} onLight={onLight} />
      <Wire short />
      <div className="grid gap-2 border-l border-(--board-wire-strong) py-1 pl-3 max-md:mt-2 max-md:ml-3">
        {arm('If yes', step.then)}
        {arm('Otherwise', step.otherwise)}
      </div>
    </div>
  );
}

export function FlowMap({ trigger, steps, catalog, run, next, locked, lit, onLight }: MapProps & { trigger: RoutineTrigger; next?: string | null }) {
  return (
    <div className="picture-stage scrollbar-none overflow-x-auto rounded-(--radius-card) border border-line p-4 md:p-6">
      <div role="img" aria-label="The steps of this routine, in order" className="mx-auto flex w-full min-w-0 items-stretch max-md:flex-col md:w-max md:items-center">
        <Node id="trigger" mark={<TriggerMark trigger={trigger} />} silk="Starts" title={capital(triggerPhrase(trigger))} note={next} lit={lit} onLight={onLight} />
        <Wire />
        <Steps steps={steps} catalog={catalog} run={run} locked={locked} lit={lit} onLight={onLight} />
      </div>
    </div>
  );
}

/** The eyebrow over a step in the flow: what kind of step it is, as a verb ("Does", "Decides"). */
const FLOW_WORD: Record<RoutineStep['kind'] | 'trigger', string> = {
  trigger: 'Starts',
  action: 'Does',
  branch: 'Decides',
  wait: 'Waits',
  loop: 'Repeats',
  ask: 'Asks',
  notify: 'Tells',
  stop_if_replied: 'Stops',
};

/** What a step came to on the newest run, as a chip in its outcome's tone, or quiet words. */
function Outcome({ ran, locked, note }: { ran?: RoutineStepRun; locked?: boolean; note?: string | null }) {
  const status = ran?.status;
  const words = ran?.short ?? note ?? null;
  if (status === 'failed') return <Status chip status={{ label: words ?? 'Did not work', tone: 'bad' }} />;
  if (status === 'waiting_approval' || status === 'waiting') return <Status chip status={{ label: words ?? 'Waits for you', tone: 'warn' }} />;
  if (status === 'running') return <Status chip status={{ label: words ?? 'Doing it now', tone: 'live' }} />;
  if (status === 'skipped') return <p className="text-secondary text-faint">Skipped last time</p>;
  if (status && words && (status === 'succeeded_verified' || status === 'succeeded_unverified' || status === 'partial')) {
    return <Status chip status={{ label: words, tone: status === 'partial' ? 'warn' : 'ok' }} />;
  }
  if (words) return <p className="text-secondary text-muted">{words}</p>;
  if (locked) {
    return (
      <p className="flex items-center gap-1 text-secondary text-muted">
        <Lock aria-hidden="true" className="size-3.5 shrink-0" />
        Always asks
      </p>
    );
  }
  return null;
}

function FlowRow({ id, mark, eyebrow, title, last, failed, children, lit, onLight }: { id: string; mark: ReactNode; eyebrow: string; title: string; last: boolean; failed?: boolean; children?: ReactNode } & Lit) {
  return (
    <li
      data-node={id}
      onPointerEnter={() => onLight?.(id)}
      onPointerLeave={() => onLight?.(null)}
      className="grid grid-cols-[40px_minmax(0,1fr)] gap-x-4"
    >
      <span className="flex flex-col items-center">
        <span className={cn('rounded-lg', failed && 'outline-1 outline-offset-2 outline-bad-line')}>{mark}</span>
        {last ? null : <span aria-hidden="true" className="my-2 w-px flex-1 bg-line" />}
      </span>
      <div
        className={cn(
          'grid min-w-0 content-start gap-1 rounded-lg pb-6 transition-colors duration-(--duration-fast) ease-standard',
          lit === id && 'bg-pressed',
          last && 'pb-0',
        )}
      >
        <span className="text-eyebrow text-muted">{eyebrow}</span>
        <span className="text-card-title text-pretty">{title}</span>
        {children}
      </div>
    </li>
  );
}

function FlowSteps({ steps, catalog, run, locked, lit, onLight, lastOuter }: MapProps & { lastOuter: boolean }) {
  return (
    <>
      {steps.map((step, index) => {
        const ran = run?.find((item) => item.step === step.id);
        const last = lastOuter && index === steps.length - 1;
        const nested = step.kind === 'branch' ? [{ label: 'If yes', steps: step.then }, { label: 'Otherwise', steps: step.otherwise }] : step.kind === 'loop' ? [{ label: 'For each one', steps: step.body }] : [];
        return (
          <FlowRow
            key={step.id}
            id={step.id}
            mark={<StepMark step={step} catalog={catalog} size={40} />}
            eyebrow={FLOW_WORD[step.kind]}
            title={capital(step.label)}
            last={last}
            failed={ran?.status === 'failed'}
            lit={lit}
            onLight={onLight}
          >
            <span className="mt-1 flex">
              <Outcome ran={ran} locked={locked.has(step.id)} />
            </span>
            {nested.map((arm) => (
              <div key={arm.label} className="mt-3 grid gap-2">
                <span className="text-secondary text-faint">{arm.label}</span>
                {arm.steps.length ? (
                  <ol className="grid">
                    <FlowSteps steps={arm.steps} catalog={catalog} run={run} locked={locked} lit={lit} onLight={onLight} lastOuter />
                  </ol>
                ) : (
                  <span className="text-secondary text-muted">Carries on</span>
                )}
              </div>
            ))}
          </FlowRow>
        );
      })}
    </>
  );
}

/**
 * The routine as drawn on its page: a column of steps from what starts it, each with its tile, its
 * verb, its words and what it came to last time. A decision and a repeat hold their own steps under
 * them. The failed step's tile wears a red edge.
 */
export function StepFlow({ trigger, steps, catalog, run, next, locked, lit, onLight }: MapProps & { trigger: RoutineTrigger; next?: string | null }) {
  return (
    <ol aria-label="The steps of this routine, in order" className="grid">
      <FlowRow id="trigger" mark={<TriggerMark trigger={trigger} size={40} />} eyebrow={FLOW_WORD.trigger} title={capital(triggerPhrase(trigger))} last={!steps.length} lit={lit} onLight={onLight}>
        {next ? <p className="text-secondary text-muted">{next}</p> : null}
      </FlowRow>
      <FlowSteps steps={steps} catalog={catalog} run={run} locked={locked} lit={lit} onLight={onLight} lastOuter />
    </ol>
  );
}
