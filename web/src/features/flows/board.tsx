/**
 * The steps, top to bottom, on the board. Between any two steps is a gap: it adds a step, and it is
 * where a dragged step can land. The same board works at 375 px, where a decision's two ways stack.
 */
import { Bell, GripVertical, Hand, Lock, MoreHorizontal, Play, Plus, RefreshCw, Split, Timer, Trash2 } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import type { KeyboardEvent, PointerEvent, ReactNode } from 'react';
import type { RoutineCatalog, RoutineStep, RoutineStepKind, RoutineStepRun, RoutineTrigger } from '@/api/types';
import { IconButton } from '@/components/ui/button';
import { Menu, MenuContent, MenuItem, MenuTrigger, Popover, PopoverContent, PopoverTrigger } from '@/components/ui/menu';
import { cn } from '@/lib/cn';
import { siteName } from '@/lib/sites';
import { isSendStep, operationFor, STEP_WORDS, TOP, TRIGGER_TARGET, TRIGGER_WORDS, triggerPhrase, triggerUnfinished, unfinished } from '@/lib/routines';
import type { Arm, Slot } from '@/lib/routines';
import { StepMark, TriggerMark } from './flow-map';
import { slotKey } from './use-reorder';

const PICKER: { kind: RoutineStepKind; icon: LucideIcon }[] = [
  { kind: 'action', icon: Play },
  { kind: 'branch', icon: Split },
  { kind: 'loop', icon: RefreshCw },
  { kind: 'wait', icon: Timer },
  { kind: 'ask', icon: Hand },
  { kind: 'notify', icon: Bell },
];

export interface BoardEvents {
  selected: string | null;
  lit: string | null;
  dragging: string | null;
  over: string | null;
  adding: string | null;
  grip: (id: string) => { onPointerDown: (event: PointerEvent<HTMLElement>) => void };
  onSelect: (id: string) => void;
  onLight: (id: string | null) => void;
  onAdding: (slot: string | null) => void;
  onAdd: (slot: Slot, kind: RoutineStepKind) => void;
  onRemove: (id: string) => void;
  onNudge: (id: string, by: -1 | 1) => void;
  onKey: (event: KeyboardEvent<HTMLElement>, id: string) => void;
}

function Gap({ slot, events, last }: { slot: Slot; events: BoardEvents; last?: boolean }) {
  const key = slotKey(slot);
  const lit = events.over === key;
  return (
    <div data-slot={key} className="group/gap relative flex h-10 w-full items-center justify-center">
      <span aria-hidden="true" className={cn('absolute inset-y-0 left-1/2 w-px', last ? 'bottom-1/2' : '', 'bg-(--board-wire-strong)')} />
      {lit ? <span aria-hidden="true" className="absolute inset-x-6 top-1/2 h-0.5 -translate-y-1/2 rounded-full bg-fg" /> : null}
      <Popover open={events.adding === key} onOpenChange={(open) => events.onAdding(open ? key : null)}>
        <PopoverTrigger
          aria-label="Add a step here"
          className={cn(
            'relative grid size-6 place-items-center rounded-full border border-line-strong bg-card text-muted transition-[opacity,transform,color] duration-(--duration-fast) ease-standard hover:scale-110 hover:text-fg',
            events.dragging ? 'opacity-0' : last || events.adding === key ? 'opacity-100' : 'opacity-0 group-hover/gap:opacity-100 focus-visible:opacity-100 pointer-coarse:opacity-100',
          )}
        >
          <Plus aria-hidden="true" className="size-3" />
        </PopoverTrigger>
        <PopoverContent align="center" className="p-1">
          <p className="px-2 py-1 text-caption text-faint">Add a step</p>
          {PICKER.map(({ kind, icon: Icon }) => (
            <button key={kind} type="button" onClick={() => events.onAdd(slot, kind)} className="flex w-full items-start gap-3 rounded-md px-2 py-2 text-left hover:bg-pressed">
              <Icon aria-hidden="true" className="translate-y-0.5 size-4 shrink-0 text-muted" />
              <span className="min-w-0">
                <span className="block text-body">{STEP_WORDS[kind].word}</span>
                <span className="block text-caption text-faint">{STEP_WORDS[kind].hint}</span>
              </span>
            </button>
          ))}
        </PopoverContent>
      </Popover>
    </div>
  );
}

const capital = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);

interface CardProps {
  id: string;
  mark: ReactNode;
  silk: string;
  title: string;
  detail: ReactNode;
  blank: boolean;
  events: BoardEvents;
  foot?: ReactNode;
  movable?: boolean;
}

function Card({ id, mark, silk, title, detail, blank, events, foot, movable = true }: CardProps) {
  const selected = events.selected === id;
  return (
    <div
      data-step-card={id}
      onPointerEnter={() => events.onLight(id)}
      onPointerLeave={() => events.onLight(null)}
      className={cn(
        'group/card relative w-full max-w-80 rounded-xl border bg-card shadow-(--shadow-card) transition-[border-color,box-shadow] duration-(--duration-fast) ease-standard',
        selected ? 'border-fg' : events.lit === id ? 'border-line-strong' : blank ? 'border-dashed border-line-strong' : 'border-line',
        events.dragging === id && 'shadow-(--shadow-hover)',
      )}
    >
      <button type="button" data-card-button={id} onClick={() => events.onSelect(id)} onKeyDown={(event) => events.onKey(event, id)} className="flex w-full items-center gap-3 rounded-xl p-3 pr-9 text-left">
        {mark}
        <span className="min-w-0 flex-1">
          <span className="silk block">{silk}</span>
          <span className="block truncate text-label">{title}</span>
          <span className="block truncate text-caption text-faint">{detail}</span>
        </span>
      </button>
      {foot ? <div className="border-t border-line px-3 py-2 text-caption text-muted">{foot}</div> : null}
      {movable ? (
        <>
          <span
            {...events.grip(id)}
            aria-hidden="true"
            className="absolute top-3 -left-6 grid h-8 w-6 cursor-grab touch-none place-items-center text-faint opacity-0 transition-opacity duration-(--duration-fast) ease-standard group-hover/card:opacity-100 active:cursor-grabbing pointer-coarse:left-auto pointer-coarse:-right-6 pointer-coarse:opacity-100"
          >
            <GripVertical className="size-4" />
          </span>
          <Menu>
            <MenuTrigger asChild>
              <IconButton label={`More for ${title}`} size="sm" className="absolute top-2 right-1">
                <MoreHorizontal className="size-4" />
              </IconButton>
            </MenuTrigger>
            <MenuContent align="end">
              <MenuItem onSelect={() => events.onNudge(id, -1)}>Move up</MenuItem>
              <MenuItem onSelect={() => events.onNudge(id, 1)}>Move down</MenuItem>
              <MenuItem onSelect={() => events.onRemove(id)} className="text-bad">
                <Trash2 className="size-4" /> Remove
              </MenuItem>
            </MenuContent>
          </Menu>
        </>
      ) : null}
    </div>
  );
}

interface ListProps {
  steps: RoutineStep[];
  parent: string | null;
  arm: Arm | null;
  catalog: RoutineCatalog | undefined;
  run: RoutineStepRun[] | undefined;
  events: BoardEvents;
  locked: ReadonlySet<string>;
  limits: Map<string, { daily_limit: number }>;
}

function detailOf(step: RoutineStep, catalog: RoutineCatalog | undefined, ran: RoutineStepRun | undefined): ReactNode {
  if (unfinished(step, catalog)) return 'Needs a choice';
  if (ran?.short) return ran.short;
  if (step.kind === 'action') {
    const lane = operationFor(catalog, step)?.lane;
    return lane ? `On ${siteName(lane.split('/')[1] ?? lane)}` : 'Ready';
  }
  if (step.kind === 'wait') return `${step.amount} ${step.unit}`;
  if (step.kind === 'notify') return 'Only to you';
  if (step.kind === 'ask') return 'Stops until someone says yes';
  return STEP_WORDS[step.kind].hint;
}

function StepBlock({ step, ...rest }: Omit<ListProps, 'steps' | 'parent' | 'arm'> & { step: RoutineStep }) {
  const { catalog, run, events, locked, limits } = rest;
  const ran = run?.find((item) => item.step === step.id);
  const sends = step.kind === 'action' && isSendStep(step, catalog);
  const foot = locked.has(step.id) ? (
    <span className="flex items-center gap-2">
      <Lock aria-hidden="true" className="size-3 shrink-0 text-faint" />
      Always asks you first
    </span>
  ) : sends ? (
    <span className="flex items-center gap-2">
      <Lock aria-hidden="true" className="size-3 shrink-0 text-faint" />
      <span className="truncate">Up to {limits.get(step.id)?.daily_limit ?? 200} a day · skips unsubscribed</span>
    </span>
  ) : null;
  const card = <Card id={step.id} mark={<StepMark step={step} catalog={catalog} />} silk={STEP_WORDS[step.kind].silk} title={capital(step.label)} detail={detailOf(step, catalog, ran)} blank={unfinished(step)} events={events} foot={foot} />;
  if (step.kind === 'branch') {
    return (
      <div data-step-block={step.id} className="relative flex w-full flex-col items-center">
        {card}
        <div className="mt-0 grid w-full gap-4 pt-6 md:grid-cols-2 relative">
          <span aria-hidden="true" className="absolute top-0 left-1/2 h-3 w-px bg-(--board-wire-strong)" />
          <span aria-hidden="true" className="absolute top-3 right-1/4 left-1/4 hidden h-px bg-(--board-wire-strong) md:block" />
          {(['then', 'otherwise'] as const).map((arm) => (
            <div key={arm} className="flex min-w-0 flex-col items-center">
              <span className="silk mb-1 rounded-sm bg-inset px-1">{arm === 'then' ? 'If yes' : 'Otherwise'}</span>
              <List {...rest} steps={step[arm]} parent={step.id} arm={arm} />
            </div>
          ))}
        </div>
      </div>
    );
  }
  if (step.kind === 'loop') {
    return (
      <div data-step-block={step.id} className="relative flex w-full flex-col items-center">
        {card}
        <div className="mt-2 flex w-full max-w-96 flex-col items-center rounded-xl border border-dashed border-(--board-line-strong) px-2 py-3">
          <span className="silk">Each {step.item}</span>
          <List {...rest} steps={step.body} parent={step.id} arm="body" />
        </div>
      </div>
    );
  }
  return (
    <div data-step-block={step.id} className="relative flex w-full flex-col items-center">
      {card}
    </div>
  );
}

function List({ steps, parent, arm, ...rest }: ListProps) {
  return (
    <>
      <Gap slot={{ parent, arm, index: 0 }} events={rest.events} last={steps.length === 0} />
      {steps.map((step, index) => (
        <div key={step.id} className="flex w-full flex-col items-center">
          <StepBlock step={step} {...rest} />
          <Gap slot={{ parent, arm, index: index + 1 }} events={rest.events} last={index === steps.length - 1} />
        </div>
      ))}
    </>
  );
}

export function Board({ trigger, steps, catalog, run, events, locked, limits }: Omit<ListProps, 'parent' | 'arm'> & { trigger: RoutineTrigger }) {
  return (
    <div role="group" aria-label="The steps" className="mx-auto flex w-full max-w-3xl flex-col items-center">
      <Card
        id={TRIGGER_TARGET}
        movable={false}
        mark={<TriggerMark trigger={trigger} />}
        silk="Starts"
        title={capital(triggerPhrase(trigger))}
        detail={triggerUnfinished(trigger) ? 'Needs a choice' : trigger.kind === 'webhook' ? 'Signed requests only' : TRIGGER_WORDS[trigger.kind].word}
        blank={triggerUnfinished(trigger)}
        events={events}
      />
      <List steps={steps} parent={TOP.parent} arm={TOP.arm} catalog={catalog} run={run} events={events} locked={locked} limits={limits} />
    </div>
  );
}

