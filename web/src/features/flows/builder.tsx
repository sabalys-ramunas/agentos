/**
 * Changing a routine: the sentence on top, the steps on the board, the settings of the chosen step
 * beside them. Reads like a sentence for someone who has never seen a workflow, and gives someone
 * used to n8n a board, data between steps and what each step did last time.
 */
import { Link, useNavigate, useParams, useSearch } from '@tanstack/react-router';
import { ArrowLeft, Redo2, Undo2 } from 'lucide-react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { toast } from 'sonner';
import { useAccess, useEnableRoutine, useFlows, useRoutineCatalog, useRoutineRuns, useSaveRoutine, useWorkspaces } from '@/api/hooks';
import type { Flow, RoutineCatalog, RoutineDefinition, RoutineStep, RoutineStepKind, RoutineTrigger } from '@/api/types';
import { Button, IconButton } from '@/components/ui/button';
import { Card as Box } from '@/components/ui/card';
import { Dialog } from '@/components/ui/dialog';
import { Tooltip } from '@/components/ui/menu';
import { Select } from '@/components/ui/select';
import { EmptyState, NoAccess, Skeleton } from '@/components/ui/states';
import { useNarrow } from '@/features/needs-you/use-narrow';
import { routineAsk } from '@/lib/ask-first';
import { cn } from '@/lib/cn';
import { describeError } from '@/lib/errors';
import { isTyping } from '@/lib/keyboard';
import { blankStep, dataBefore, fillTemplate, findStep, flatSteps, insertStep, moveStep, newStepId, nudgeStep, operationFor, refName, removeStep, sentenceOf, slotOf, TRIGGER_TARGET, updateStep, whatIsMissing } from '@/lib/routines';
import type { Slot } from '@/lib/routines';
import { Board } from './board';
import type { BoardEvents } from './board';
import { Inspector } from './inspector';
import { useDraft } from './use-draft';
import { NEEDS_REVIEW_WORDS, routineTitle } from './routine-words';
import { slotKey, useReorder, useSettle } from './use-reorder';

function Sentence({ draft, catalog, lit, selected, onLight, onPick }: { draft: RoutineDefinition; catalog: RoutineCatalog | undefined; lit: string | null; selected: string | null; onLight: (id: string | null) => void; onPick: (id: string) => void }) {
  const parts = sentenceOf(draft, (ref) => refName(dataBefore(draft, null, catalog), ref)?.toLowerCase() ?? null, catalog);
  return (
    <p className="max-w-4xl text-20 font-semibold text-balance md:text-24">
      {parts.map((part, index) => {
        // A button is its own box, so a comma after one could fall to the next line. It goes inside.
        const before = index ? parts[index - 1] : null;
        const after = parts[index + 1];
        const mark = part.target && after && !after.target ? /^[,.]/.exec(after.text)?.[0] : null;
        const text = !part.target && before?.target ? part.text.replace(/^[,.]/, '') : part.text;
        if (!part.target) {
          return text ? (
            <span key={index} className="text-faint">
              {text}
            </span>
          ) : null;
        }
        return (
          <button
            key={index}
            type="button"
            aria-label={part.text}
            onClick={() => onPick(part.target ?? '')}
            onPointerEnter={() => onLight(part.target ?? null)}
            onPointerLeave={() => onLight(null)}
            className={cn(
              'rounded-sm text-left font-semibold transition-colors duration-(--duration-fast) ease-standard hover:bg-fill-2',
              part.blank ? 'underline decoration-amber decoration-dashed decoration-2 underline-offset-4' : 'underline decoration-transparent',
              (lit === part.target || selected === part.target) && 'bg-fill-2',
            )}
          >
            {part.text}
            {mark ? <span className="text-faint">{mark}</span> : null}
          </button>
        );
      })}
    </p>
  );
}

interface EditorProps {
  org: string;
  start: RoutineDefinition;
  flow: Flow | null;
  catalog: RoutineCatalog | undefined;
}

function Editor({ org, start, flow, catalog }: EditorProps) {
  const navigate = useNavigate();
  const narrow = useNarrow();
  const save = useSaveRoutine(org);
  const enable = useEnableRoutine(org);
  const canDecide = useAccess(org).can('approvals.decide');
  const workspaces = useWorkspaces(org).data ?? [];
  const runs = useRoutineRuns(org, flow?.id ?? '').data;
  const run = runs?.find((item) => item.steps.length)?.steps;

  const { draft, change, undo, redo, canUndo, canRedo, dirty, markSaved } = useDraft(start);
  const [selected, setSelected] = useState<string | null>(null);
  const [lit, setLit] = useState<string | null>(null);
  const [adding, setAdding] = useState<string | null>(null);
  const [said, setSaid] = useState('');
  const [problem, setProblem] = useState<string | null>(null);
  const board = useRef<HTMLDivElement>(null);
  const focusNext = useRef<string | null>(null);

  const steps = draft.steps;
  const missing = useMemo(() => whatIsMissing(draft, catalog), [draft, catalog]);
  const step = selected && selected !== TRIGGER_TARGET ? findStep(steps, selected) : null;
  const locked = useMemo(() => {
    const out = new Set<string>();
    for (const item of flatSteps(steps)) {
      const effect = operationFor(catalog, item)?.effect;
      if (effect === 'pay' || effect === 'irreversible') out.add(item.id);
    }
    return out;
  }, [steps, catalog]);
  const limits = useMemo(() => new Map((flow?.send_limits ?? []).map((item) => [item.step, item])), [flow?.send_limits]);

  const setSteps = useCallback((next: (steps: RoutineStep[]) => RoutineStep[], key?: string) => change((current) => {
    const made = next(current.steps);
    return made === current.steps ? current : { ...current, steps: made };
  }, key), [change]);

  const onDrop = useCallback((id: string, slot: Slot) => {
    setSteps((current) => moveStep(current, id, slot));
    setSaid('Moved.');
  }, [setSteps]);
  const { grip, dragging, over } = useReorder(board, onDrop);
  useSettle(board, draft.steps);

  useEffect(() => {
    if (!focusNext.current) return;
    board.current?.querySelector<HTMLElement>(`[data-card-button="${focusNext.current}"]`)?.focus();
    focusNext.current = null;
  });

  useEffect(() => {
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (!(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== 'z' || isTyping(event)) return;
      event.preventDefault();
      if (event.shiftKey) redo();
      else undo();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [undo, redo]);

  const remove = (id: string) => {
    const target = findStep(steps, id);
    const order = flatSteps(steps).map((item) => item.id);
    const at = order.indexOf(id);
    setSteps((current) => removeStep(current, id));
    setSelected((current) => (current === id ? null : current));
    focusNext.current = order[at - 1] ?? TRIGGER_TARGET;
    toast(`Removed "${target?.label ?? 'step'}"`, { action: { label: 'Undo', onClick: undo } });
  };

  const nudge = (id: string, by: -1 | 1) => {
    const next = nudgeStep(steps, id, by);
    if (!next) return;
    setSteps(() => next);
    focusNext.current = id;
    setSaid(by < 0 ? 'Moved up.' : 'Moved down.');
  };

  const add = (slot: Slot, kind: RoutineStepKind) => {
    const taken = new Set(flatSteps(steps).map((item) => item.id));
    const id = newStepId(kind, taken);
    setSteps((current) => insertStep(current, slot, blankStep(kind, id)));
    setAdding(null);
    setSelected(id);
    focusNext.current = id;
  };

  const onKey = (event: KeyboardEvent<HTMLElement>, id: string) => {
    const cards = [...(board.current?.querySelectorAll<HTMLElement>('[data-card-button]') ?? [])];
    const at = cards.findIndex((card) => card.dataset.cardButton === id);
    if (event.altKey && (event.key === 'ArrowUp' || event.key === 'ArrowDown')) {
      event.preventDefault();
      if (id !== TRIGGER_TARGET) nudge(id, event.key === 'ArrowUp' ? -1 : 1);
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      cards[at + (event.key === 'ArrowDown' ? 1 : -1)]?.focus();
    } else if ((event.key === 'Delete' || event.key === 'Backspace') && id !== TRIGGER_TARGET) {
      event.preventDefault();
      remove(id);
    } else if (event.key.toLowerCase() === 'a') {
      event.preventDefault();
      const slot = id === TRIGGER_TARGET ? { parent: null, arm: null, index: 0 } : slotOf(steps, id);
      if (slot) setAdding(slotKey(id === TRIGGER_TARGET ? slot : { ...slot, index: slot.index + 1 }));
    }
  };

  const events: BoardEvents = { selected, lit, dragging, over, adding, grip, onSelect: setSelected, onLight: setLit, onAdding: setAdding, onAdd: add, onRemove: remove, onNudge: nudge, onKey };

  /**
   * Saves the routine. A new one is saved off, and Turn on then asks the computer to switch it on: the computer
   * decides whether this person may, and the words say which of the two happened. Nothing is claimed that the
   * computer did not answer.
   */
  const commit = async (turnOn: boolean) => {
    setProblem(null);
    try {
      const saved = await save.mutateAsync({ id: flow?.id ?? null, body: draft });
      markSaved();
      if (saved.needs_review) {
        // The routine is on, and this version is saved over it: nothing is switched, so there is nothing to turn on.
        toast(`Saved. ${NEEDS_REVIEW_WORDS}`);
        return;
      }
      if (flow && !turnOn) {
        toast(saved.enabled === false ? 'Saved. It is still off.' : 'Saved on the computer');
        return;
      }
      if (flow && saved.enabled !== false) {
        toast('Saved on the computer');
        return;
      }
      if (turnOn) {
        try {
          await enable.mutateAsync(saved.id);
          toast(`${draft.name} is on`);
        } catch (error) {
          toast(`${draft.name} is saved and off. ${describeError(error)}`);
        }
      } else {
        toast(`${draft.name} is saved as a draft and off`);
      }
      if (!flow) void navigate({ to: '/$org/flows/$flow', params: { org, flow: saved.id } });
    } catch (error) {
      setProblem(describeError(error));
    }
  };

  const firstMissing = missing[0];
  const ask = flow ? routineAsk(flow, canDecide) : null;
  const inspector = selected ? (
    <Inspector
      target={selected}
      step={step}
      definition={draft}
      flow={flow}
      catalog={catalog}
      ran={run?.find((item) => item.step === selected)}
      onStep={(patch, key) => selected && setSteps((current) => updateStep(current, selected, patch), key)}
      onTrigger={(trigger: RoutineTrigger) => change((current) => ({ ...current, trigger }))}
    />
  ) : null;

  return (
    <div data-bleed className="flex min-h-[calc(100dvh-56px)] flex-col max-lg:pb-16">
      <header className="sticky top-0 z-20 border-b border-line bg-bg px-4 py-3 md:px-6">
        <div className="flex items-center gap-2">
          <Tooltip label="Back to the routine">
            <Link to={flow ? '/$org/flows/$flow' : '/$org/flows/new'} params={{ org, flow: flow?.id ?? '' }} aria-label="Back" className="grid size-8 shrink-0 place-items-center rounded-md text-muted hover:bg-fill-1 hover:text-fg">
              <ArrowLeft className="size-4" />
            </Link>
          </Tooltip>
          <input
            aria-label="Name of this routine"
            value={draft.name}
            onChange={(event) => change((current) => ({ ...current, name: event.target.value }), 'name')}
            className="min-w-0 flex-1 rounded-md border border-transparent bg-transparent px-2 py-1 text-14 font-medium transition-colors duration-(--duration-fast) ease-standard hover:border-line md:max-w-72"
          />
          {!flow && workspaces.length > 1 ? (
            <Select aria-label="For which business" size="sm" value={draft.workspace ?? ''} onValueChange={(workspace) => change((current) => ({ ...fillTemplate({ name: current.name, trigger: current.trigger, steps: current.steps }, catalog, workspace), workspace }))} options={workspaces.map((item) => ({ value: item.id, label: item.name }))} className="hidden w-44 md:inline-flex" />
          ) : null}
          <span className="flex-1 max-md:hidden" />
          <p aria-live="polite" className="hidden w-44 truncate text-right text-13 text-faint lg:block">
            {problem ?? (save.isPending ? 'Telling the computer.' : dirty ? 'Not saved yet' : flow ? 'Saved' : '')}
          </p>
          <IconButton label="Undo" disabled={!canUndo} onClick={undo}>
            <Undo2 className="size-4" />
          </IconButton>
          <IconButton label="Redo" disabled={!canRedo} onClick={redo} className="max-sm:hidden">
            <Redo2 className="size-4" />
          </IconButton>
          {flow ? (
            <Button variant={flow.enabled === false ? 'secondary' : 'primary'} loading={save.isPending && !enable.isPending} onClick={() => void commit(false)}>
              Save
            </Button>
          ) : (
            <Button loading={save.isPending && !enable.isPending} onClick={() => void commit(false)}>
              Save as draft
            </Button>
          )}
          {!flow || flow.enabled === false ? (
            <Button
              variant="primary"
              loading={enable.isPending || (save.isPending && !flow)}
              onClick={() => {
                if (firstMissing) {
                  // Turn on never fails silently: it names the first thing to finish, and opens it.
                  setSelected(firstMissing.target);
                  toast(`It is not on yet. ${firstMissing.words}`);
                  return;
                }
                void commit(true);
              }}
            >
              Turn on
            </Button>
          ) : null}
        </div>
      </header>

      <div className="border-b border-line px-4 py-4 md:px-6 md:py-6">
        <Sentence draft={draft} catalog={catalog} lit={lit} selected={selected} onLight={setLit} onPick={setSelected} />
        <p className="mt-2 flex min-h-5 flex-wrap items-center gap-x-3 text-13 text-muted">
          {firstMissing ? (
            <button type="button" onClick={() => setSelected(firstMissing.target)} className="link">
              {missing.length === 1 ? firstMissing.words : `${missing.length} things to finish. ${firstMissing.words}`}
            </button>
          ) : (
            <span>{ask ? ask.word : 'Runs on its own'}. {locked.size ? `${locked.size === 1 ? 'One step always asks' : `${locked.size} steps always ask`}.` : 'Money and things that can\'t be undone always ask.'}</span>
          )}
        </p>
      </div>

      <div className="grid min-h-0 flex-1 lg:grid-cols-[minmax(0,1fr)_24rem]">
        <div ref={board} className="picture-stage min-w-0 overflow-x-hidden px-8 py-8 md:px-10">
          <Board trigger={draft.trigger} steps={steps} catalog={catalog} run={run} events={events} locked={locked} limits={limits} />
          <p className="mx-auto mt-6 hidden max-w-3xl text-center text-12 text-faint lg:block">Arrow keys move between steps. Alt and an arrow moves a step. A adds one, Delete removes it, and Cmd Z undoes.</p>
        </div>
        {narrow ? (
          <Dialog open={selected !== null} onOpenChange={(open) => !open && setSelected(null)} title="This step" className="max-h-[85dvh]">
            {inspector}
          </Dialog>
        ) : (
          <aside aria-label="Settings of this step" className="border-l border-line bg-bg p-6">
            {inspector ?? (
              <div className="grid gap-4">
                <h2 className="text-16 font-semibold">{firstMissing ? 'Almost ready' : 'Ready to go'}</h2>
                {missing.length ? (
                  <ul className="grid gap-1">
                    {missing.map((item) => (
                      <li key={`${item.target}-${item.words}`}>
                        <button type="button" onClick={() => setSelected(item.target)} className="w-full rounded-md px-2 py-2 text-left text-14 text-pretty hover:bg-fill-1">
                          {item.words}
                        </button>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="text-14 text-pretty text-muted">Nothing is missing. Choose a step to see its settings, and what it did last time.</p>
                )}
              </div>
            )}
          </aside>
        )}
      </div>
      <p aria-live="assertive" className="sr-only">{said}</p>
    </div>
  );
}

function Loading() {
  return (
    <div role="status" aria-label="Loading" className="grid gap-6">
      <Skeleton className="h-12 w-3/4" />
      <Skeleton className="h-96 rounded-(--radius-card)" />
    </div>
  );
}

export function NewRoutineBuilder() {
  const { org, template } = useParams({ from: '/$org/flows/new/$template' });
  const search = useSearch({ from: '/$org/flows/new/$template' });
  const catalog = useRoutineCatalog(org);
  const workspaces = useWorkspaces(org);
  const can = useAccess(org).can('lanes.operate');
  if (!can) return <Box><NoAccess /></Box>;
  if (!catalog.data || !workspaces.data) return <Loading />;
  const chosen = catalog.data.templates.find((item) => item.id === template);
  const workspace = search.workspace || workspaces.data[0]?.id || null;
  const start = chosen ? fillTemplate(chosen, catalog.data, workspace) : { name: 'New routine', workspace, trigger: { kind: 'manual' as const }, steps: [] };
  return <Editor key={template} org={org} start={start} flow={null} catalog={catalog.data} />;
}

export function EditRoutine() {
  const { org, flow: id } = useParams({ from: '/$org/flows/$flow/edit' });
  const flows = useFlows(org);
  const catalog = useRoutineCatalog(org);
  const can = useAccess(org).can('lanes.operate');
  if (!can) return <Box><NoAccess /></Box>;
  if (!flows.data || !catalog.data) return <Loading />;
  const flow = flows.data.find((item) => item.id === id);
  if (!flow?.trigger || !flow.steps) {
    return (
      <Box>
        <EmptyState title="This routine can't be changed here yet" description="Its computer keeps it as a recording, so its steps can't be shown here. Updating that computer lets you see and change them." />
      </Box>
    );
  }
  const start: RoutineDefinition = { name: routineTitle(flow), workspace: flow.lane.split('/')[0] ?? null, trigger: flow.trigger, steps: flow.steps };
  return <Editor key={id} org={org} start={start} flow={flow} catalog={catalog.data} />;
}
