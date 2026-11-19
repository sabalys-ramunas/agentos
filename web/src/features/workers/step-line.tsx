/**
 * The step line, the one picture workers have: one thing done, a tick for each step. The rows are who
 * thought, in the same order as the jobs, so most of it stays on the top row and the few steps that
 * needed help fall out of it, as far down as they had to go. The rail is as long as the limit on one
 * thing, so all of them share a scale and a step past the limit cannot be drawn. Pink is the step
 * being taken now, and nothing else.
 */
import { useId, useState } from 'react';
import type { KeyboardEvent, PointerEvent } from 'react';
import type { WorkerStep, WorkerStepKind, WorkerThing } from '@/api/types';
import { StateGlyph } from '@/components/ui/state-badge';
import { cn } from '@/lib/cn';
import { useTechnical } from '@/lib/technical';
import { WORKER_ROWS } from '@/lib/vocabulary';
import { STEP_WHO, STOP_WORDS } from './words';
import './workers.css';

type Row = keyof typeof WORKER_ROWS;
const ROWS: Row[] = ['decide', 'write', 'rescue', 'you'];
const ROW_OF: Record<WorkerStepKind, Row> = { decide: 'decide', write: 'write', rescue: 'rescue', you: 'you', yes: 'you' };
const ROW_HEIGHT = 28;

const at = (index: number, limit: number) => `${((index + 0.5) / limit) * 100}%`;
const rowIndex = (step: WorkerStep) => ROWS.indexOf(ROW_OF[step.kind]);

function Face({ kind, thing, left, on, now }: { kind: WorkerStepKind; thing: WorkerThing; left: string; on: boolean; now: boolean }) {
  const drop = ROWS.indexOf(ROW_OF[kind]) * ROW_HEIGHT;
  const style = { left, '--drop': `${drop}px` } as React.CSSProperties;
  return (
    <>
      <span className="steps-stem" style={style} />
      {kind === 'you' ? (
        <span className="steps-face" data-on={on || undefined} style={style}>
          <StateGlyph state={thing.state === 'asked' ? 'waiting' : 'sleeping'} size={16} />
        </span>
      ) : (
        <span className="steps-face" data-kind={kind} data-on={on || undefined} data-now={now || undefined} style={style} />
      )}
    </>
  );
}

function Reading({ thing, index, showing }: { thing: WorkerThing; index: number; showing: boolean }) {
  const [technical] = useTechnical();
  const step = thing.steps[index];
  if (!step) return <p className="min-h-10 text-secondary text-muted">No steps yet.</p>;
  const said = !showing && thing.state !== 'working' && thing.stop ? STOP_WORDS[thing.stop] : STEP_WHO[step.kind];
  const note = !showing && thing.state === 'working' ? `${said}. It stops by itself at step ${thing.limit}.` : said;
  return (
    <div className="flex min-h-10 min-w-0 items-start gap-3 sm:gap-4">
      <span className="tabular w-[52px] shrink-0 text-secondary text-faint sm:w-[72px]">Step {index + 1}</span>
      <div className="min-w-0 flex-1">
        <p className="text-body text-pretty">{step.what}</p>
        <p className="text-secondary text-pretty text-muted">{note}</p>
        {technical && step.model ? (
          <p className="mono mt-1 text-caption text-faint">
            {step.model} · {step.ms.toLocaleString('en-GB')} ms · ${step.usd.toFixed(4)}
          </p>
        ) : null}
      </div>
    </div>
  );
}

/** The whole picture. Pointing moves one fixed line underneath, so reading it never moves the page. */
export function StepLine({ thing, example = false, className }: { thing: WorkerThing; example?: boolean; className?: string }) {
  const [pointed, setPointed] = useState<number | null>(null);
  const label = useId();
  const count = thing.steps.length;
  const last = Math.max(0, count - 1);
  const index = pointed ?? last;
  const step = thing.steps[index];
  const counts = (row: Row) => thing.steps.filter((item) => ROW_OF[item.kind] === row).length;
  const live = thing.state === 'working';
  const tip = thing.steps[last];
  const tipRow = tip ? rowIndex(tip) : 0;

  const point = (event: PointerEvent<HTMLDivElement>) => {
    const box = event.currentTarget.getBoundingClientRect();
    const place = Math.floor(((event.clientX - box.left) / box.width) * thing.limit);
    setPointed(Math.max(0, Math.min(last, place)));
  };
  const keys = (event: KeyboardEvent<HTMLDivElement>) => {
    const move: Record<string, number> = { ArrowRight: index + 1, ArrowUp: index + 1, ArrowLeft: index - 1, ArrowDown: index - 1, Home: 0, End: last };
    const next = move[event.key];
    if (next === undefined) return;
    event.preventDefault();
    setPointed(Math.max(0, Math.min(last, next)));
  };

  return (
    <div className={cn('grid gap-3', className)}>
      <div className="steps" data-reading={pointed !== null || undefined} data-example={example || undefined}>
        <div aria-hidden="true" className="grid auto-rows-[28px]">
          {ROWS.map((row) => (
            <span key={row} className="flex items-center truncate text-caption text-muted">{WORKER_ROWS[row].word}</span>
          ))}
        </div>
        <div
          role="slider"
          tabIndex={0}
          aria-label="Steps, one at a time"
          aria-labelledby={label}
          aria-valuemin={1}
          aria-valuemax={Math.max(1, count)}
          aria-valuenow={index + 1}
          aria-valuetext={step ? `Step ${index + 1}: ${step.what}` : 'No steps yet'}
          className="steps-rails"
          onPointerMove={point}
          onPointerDown={point}
          onPointerLeave={(event) => event.pointerType === 'mouse' && setPointed(null)}
          onKeyDown={keys}
          onBlur={() => setPointed(null)}
        >
          {ROWS.map((row, rowAt) => (
            <span key={row} className="steps-rail" style={{ top: rowAt * ROW_HEIGHT + ROW_HEIGHT / 2 }} />
          ))}
          <span className="steps-cap" />
          {thing.steps.map((item, position) => {
            const left = at(position, thing.limit);
            const on = pointed === position;
            if (rowIndex(item) > 0) return <Face key={position} kind={item.kind} thing={thing} left={left} on={on} now={live && position === last} />;
            return <span key={position} className="steps-tick" data-on={on || undefined} data-now={(live && position === last) || undefined} style={{ left }} />;
          })}
          {live ? <span className="steps-now" style={{ left: at(last, thing.limit), top: tipRow * ROW_HEIGHT + ROW_HEIGHT / 2 }} /> : null}
          {thing.state === 'done' ? (
            <span className="absolute top-[14px] -translate-x-1/2 -translate-y-1/2" style={{ left: at(count + 1, thing.limit) }}>
              <StateGlyph state="good" size={14} />
            </span>
          ) : null}
        </div>
        <div aria-hidden="true" className="hidden auto-rows-[28px] sm:grid">
          {ROWS.map((row) => (
            <span key={row} className="tabular flex items-center justify-end text-secondary text-muted">{counts(row) || <span className="text-faint">–</span>}</span>
          ))}
        </div>
      </div>
      <p className="tabular text-caption text-muted sm:hidden">
        {ROWS.filter((row) => counts(row)).map((row) => `${WORKER_ROWS[row].word} ${counts(row)}`).join(' · ')}
      </p>
      <p id={label} className="sr-only">Each step of this thing, and who thought at each one.</p>
      {example ? null : <Reading thing={thing} index={index} showing={pointed !== null} />}
    </div>
  );
}

/** The stream alone, for a row. A step that needed anyone else reaches further than the rest. */
export function StepLineMini({ thing, className }: { thing: WorkerThing; className?: string }) {
  const last = thing.steps.length - 1;
  return (
    <div aria-hidden="true" className={cn('steps-mini', className)}>
      <span className="steps-rail" />
      <span className="steps-cap" style={{ top: 4, height: 12 }} />
      {thing.steps.map((item, position) => (
        <span
          key={position}
          className="steps-tick"
          data-reach={item.kind === 'decide' ? undefined : ''}
          data-now={(thing.state === 'working' && position === last) || undefined}
          style={{ left: at(position, thing.limit) }}
        />
      ))}
    </div>
  );
}
