/**
 * A routine as a tree of steps, and everything the screens need to say about it. One place, so
 * the sentence on the list, the sentence over the board and the file a developer reads agree.
 * Pure functions on plain data: every edit returns a new definition, which is what makes undo work.
 */
import type {
  RoutineActionStep,
  RoutineCatalog,
  RoutineCondition,
  RoutineDefinition,
  RoutineField,
  RoutineOperation,
  RoutineStep,
  RoutineStepKind,
  RoutineTemplate,
  RoutineTrigger,
} from '@/api/types';
import { describeCron } from './cron';

/** The lists of steps inside a step. */
export type Arm = 'then' | 'otherwise' | 'body';

/** A place between steps. `parent` null is the top level. */
export interface Slot {
  parent: string | null;
  arm: Arm | null;
  index: number;
}

export const TOP: Omit<Slot, 'index'> = { parent: null, arm: null };

function armsOf(step: RoutineStep): Arm[] {
  if (step.kind === 'branch') return ['then', 'otherwise'];
  return step.kind === 'loop' ? ['body'] : [];
}

function armOf(step: RoutineStep, arm: Arm): RoutineStep[] {
  if (step.kind === 'branch' && arm !== 'body') return step[arm];
  if (step.kind === 'loop' && arm === 'body') return step.body;
  return [];
}

function withArm(step: RoutineStep, arm: Arm, steps: RoutineStep[]): RoutineStep {
  if (step.kind === 'branch' && arm !== 'body') return { ...step, [arm]: steps };
  if (step.kind === 'loop' && arm === 'body') return { ...step, body: steps };
  return step;
}

/** Every step, in reading order. */
export function flatSteps(steps: RoutineStep[]): RoutineStep[] {
  return steps.flatMap((step) => [step, ...armsOf(step).flatMap((arm) => flatSteps(armOf(step, arm)))]);
}

export const findStep = (steps: RoutineStep[], id: string) => flatSteps(steps).find((step) => step.id === id) ?? null;

/** Applies `change` to the list a slot's parent holds, wherever in the tree that is. */
function mapList(steps: RoutineStep[], parent: string | null, arm: Arm | null, change: (list: RoutineStep[]) => RoutineStep[]): RoutineStep[] {
  if (parent === null || arm === null) return change(steps);
  return steps.map((step) => {
    if (step.id === parent) return withArm(step, arm, change(armOf(step, arm)));
    return armsOf(step).reduce((next, name) => withArm(next, name, mapList(armOf(next, name), parent, arm, change)), step);
  });
}

export function updateStep(steps: RoutineStep[], id: string, patch: (step: RoutineStep) => RoutineStep): RoutineStep[] {
  return steps.map((step) => {
    if (step.id === id) return patch(step);
    return armsOf(step).reduce((next, name) => withArm(next, name, updateStep(armOf(next, name), id, patch)), step);
  });
}

export function slotOf(steps: RoutineStep[], id: string, parent: string | null = null, arm: Arm | null = null): Slot | null {
  for (const [index, step] of steps.entries()) {
    if (step.id === id) return { parent, arm, index };
    for (const name of armsOf(step)) {
      const found = slotOf(armOf(step, name), id, step.id, name);
      if (found) return found;
    }
  }
  return null;
}

export const insertStep = (steps: RoutineStep[], slot: Slot, step: RoutineStep) =>
  mapList(steps, slot.parent, slot.arm, (list) => [...list.slice(0, slot.index), step, ...list.slice(slot.index)]);

export function removeStep(steps: RoutineStep[], id: string): RoutineStep[] {
  const slot = slotOf(steps, id);
  if (!slot) return steps;
  return mapList(steps, slot.parent, slot.arm, (list) => list.filter((step) => step.id !== id));
}

/** True when `id` is `inside` or anywhere under it: a step cannot move into itself. */
export function isWithin(steps: RoutineStep[], inside: string, id: string): boolean {
  const step = findStep(steps, inside);
  return step ? flatSteps([step]).some((item) => item.id === id) : false;
}

/** `slot` is read against the list as it stands, with the step still in it. */
export function moveStep(steps: RoutineStep[], id: string, slot: Slot): RoutineStep[] {
  const step = findStep(steps, id);
  const from = slotOf(steps, id);
  if (!step || !from || (slot.parent && isWithin(steps, id, slot.parent))) return steps;
  const same = from.parent === slot.parent && from.arm === slot.arm;
  const index = same && from.index < slot.index ? slot.index - 1 : slot.index;
  if (same && index === from.index) return steps;
  return insertStep(removeStep(steps, id), { ...slot, index }, step);
}

/** One place up or down the list a step is in. Null at either end. */
export function nudgeStep(steps: RoutineStep[], id: string, by: -1 | 1): RoutineStep[] | null {
  const from = slotOf(steps, id);
  if (!from) return null;
  const size = from.parent ? armOf(findStep(steps, from.parent) as RoutineStep, from.arm as Arm).length : steps.length;
  const index = from.index + by;
  if (index < 0 || index >= size) return null;
  return moveStep(steps, id, { ...from, index: by > 0 ? index + 1 : index });
}

let counter = 0;

/** Readable in a file, unique within a routine. `taken` is what the definition already uses. */
export function newStepId(kind: RoutineStepKind, taken: ReadonlySet<string>): string {
  for (;;) {
    counter += 1;
    const id = `${kind}_${counter}`;
    if (!taken.has(id)) return id;
  }
}

export const STEP_WORDS: Record<RoutineStepKind, { silk: string; word: string; hint: string; label: string }> = {
  action: { silk: 'Do', word: 'Do something', hint: 'Run a routine or use an account.', label: 'do something' },
  branch: { silk: 'Decide', word: 'Decide', hint: 'Go one way or the other, based on what came before.', label: 'decide' },
  wait: { silk: 'Wait', word: 'Wait', hint: 'Pause for a while, then carry on.', label: 'wait a day' },
  loop: { silk: 'Repeat', word: 'Repeat for each', hint: 'Do the same steps for every item in a list.', label: 'for each one' },
  ask: { silk: 'Ask', word: 'Ask a person', hint: 'Stop here until someone says yes.', label: 'ask you' },
  notify: { silk: 'Tell', word: 'Tell me', hint: 'Send you a note. Nothing goes to anyone else.', label: 'tell you' },
  stop_if_replied: { silk: 'Stop', word: 'Stop if they reply', hint: 'Leave out anyone who already answered an earlier email.', label: 'leave out whoever replied' },
};

export function blankStep(kind: RoutineStepKind, id: string): RoutineStep {
  const label = STEP_WORDS[kind].label;
  switch (kind) {
    case 'action':
      return { id, kind, label, operation: null, inputs: {} };
    case 'branch':
      return { id, kind, label, when: { left: null, op: 'is', right: '' }, then: [], otherwise: [] };
    case 'wait':
      return { id, kind, label, amount: 1, unit: 'days' };
    case 'loop':
      return { id, kind, label, over: null, item: 'item', body: [] };
    case 'ask':
      return { id, kind, label, question: '' };
    case 'notify':
      return { id, kind, label, message: '' };
    case 'stop_if_replied':
      return { id, kind, label, after: '' };
  }
}

export const TRIGGER_WORDS: Record<RoutineTrigger['kind'], { word: string; hint: string }> = {
  schedule: { word: 'On a schedule', hint: 'Every day, every Monday, the first of the month.' },
  webhook: { word: 'When an app sends a webhook', hint: 'Your shop, your form or your own code calls a private address.' },
  email: { word: 'When an email arrives', hint: 'Watches one mail account. Your login stays on your computer.' },
  page_change: { word: 'When a page changes', hint: 'Looks at a page and notices what is new.' },
  site_change: { word: "When a site's answer changes", hint: 'Checks a site on a timer and starts when a row is new, gone or changed.' },
  manual: { word: 'Only when I start it', hint: 'Nothing happens until someone presses Run now.' },
};

/** The triggers a person builds here. A site's rows start from a Site API, which has its own screen. */
export const BUILT_TRIGGERS = ['schedule', 'webhook', 'email', 'page_change', 'manual'] as const satisfies readonly RoutineTrigger['kind'][];

export function triggerPhrase(trigger: RoutineTrigger): string {
  if (trigger.label) return trigger.label;
  switch (trigger.kind) {
    case 'schedule': {
      if (!trigger.cron) return 'on a schedule';
      const words = describeCron(trigger.cron);
      return words === trigger.cron ? 'on a schedule' : words;
    }
    case 'webhook':
      return 'when an app sends a webhook';
    case 'email':
      return 'when an email arrives';
    case 'page_change':
      return 'when a page changes';
    case 'site_change':
      return "when a site's answer changes";
    case 'manual':
      return 'when you start it';
  }
}

const COMPARE: Record<RoutineCondition['op'], string> = {
  is: 'is',
  is_not: 'is not',
  contains: 'contains',
  more_than: 'is more than',
  less_than: 'is less than',
  is_empty: 'is empty',
  not_empty: 'is not empty',
};

export const COMPARE_WORDS = COMPARE;

const REF = /\{\{\s*([\w-]+)\.([\w-]+)\s*\}\}/g;

export const refOf = (step: string, field: string) => `{{${step}.${field}}}`;

export function parseRef(text: string | null | undefined): { step: string; field: string } | null {
  const match = /^\{\{\s*([\w-]+)\.([\w-]+)\s*\}\}$/.exec(text?.trim() ?? '');
  return match ? { step: match[1] ?? '', field: match[2] ?? '' } : null;
}

export type Piece = { text: string } | { ref: string; step: string; field: string };

/** Text with data in it, as what a field shows: plain runs and the references between them. */
export function piecesOf(text: string): Piece[] {
  const pieces: Piece[] = [];
  let at = 0;
  for (const match of text.matchAll(REF)) {
    if (match.index > at) pieces.push({ text: text.slice(at, match.index) });
    pieces.push({ ref: match[0], step: match[1] ?? '', field: match[2] ?? '' });
    at = match.index + match[0].length;
  }
  if (at < text.length) pieces.push({ text: text.slice(at) });
  return pieces;
}

export interface SentencePart {
  text: string;
  /** The step this phrase is about, or `trigger`. */
  target?: string;
  /** Something a person still has to choose. */
  blank?: boolean;
}

export const TRIGGER_TARGET = 'trigger';

const capitalise = (text: string) => text.charAt(0).toUpperCase() + text.slice(1);

function joined(lists: SentencePart[][]): SentencePart[] {
  return lists.flatMap((part, index) => {
    if (index === 0) return part;
    return [{ text: index === lists.length - 1 ? ', then ' : ', ' }, ...part];
  });
}

function stepPhrase(step: RoutineStep, name: (ref: string | null) => string | null, needsChoice: (step: RoutineStep) => boolean): SentencePart[] {
  const own = { text: step.label, target: step.id, blank: needsChoice(step) };
  if (step.kind === 'branch') {
    const left = name(step.when.left);
    const rest = step.when.op === 'is_empty' || step.when.op === 'not_empty' ? COMPARE[step.when.op] : `${COMPARE[step.when.op]} ${step.when.right}`;
    const said = step.label !== STEP_WORDS.branch.label;
    const test: SentencePart = said ? { text: step.label, target: step.id, blank: !step.when.left } : left ? { text: `if ${left} ${rest}`, target: step.id } : { text: 'if something is true', target: step.id, blank: true };
    const yes = joined(step.then.map((item) => stepPhrase(item, name, needsChoice)));
    const no = joined(step.otherwise.map((item) => stepPhrase(item, name, needsChoice)));
    return [test, ...(yes.length ? [{ text: ', ' }, ...yes] : []), ...(no.length ? [{ text: ', otherwise ' }, ...no] : [])];
  }
  if (step.kind === 'loop') {
    const inner = joined(step.body.map((item) => stepPhrase(item, name, needsChoice)));
    return [{ text: `for each ${step.item}`, target: step.id, blank: !step.over }, ...(inner.length ? [{ text: ', ' }, ...inner] : [])];
  }
  return [own];
}

/** A value nobody typed: missing, empty or only spaces. */
const blankValue = (value: unknown) => value === undefined || value === null || (typeof value === 'string' && !value.trim()) || (Array.isArray(value) && !value.length);

/** The words a list reads as: `a`, `a and b`, `a, b and c`. */
const listWords = (items: string[]) => (items.length <= 1 ? (items[0] ?? '') : `${items.slice(0, -1).join(', ')} and ${items.at(-1)}`);

/** The params the step's operation needs and the step leaves empty, in the words a person reads ("caption"). */
export function missingParams(step: RoutineActionStep, catalog: RoutineCatalog | undefined): string[] {
  const operation = operationOf(catalog, step);
  if (!operation) return [];
  return Object.entries(operation.params)
    .filter(([key, param]) => param.required && blankValue(step.inputs[key]))
    .map(([key, param]) => param.label ?? key.replaceAll('_', ' '));
}

/** Whether a step still needs a person to choose something before it can run. */
export function unfinished(step: RoutineStep, catalog?: RoutineCatalog): boolean {
  switch (step.kind) {
    case 'action':
      return !step.operation || missingParams(step, catalog).length > 0;
    case 'branch':
      return !step.when.left;
    case 'loop':
      return !step.over;
    case 'ask':
      return !step.question.trim();
    case 'notify':
      return !step.message.trim();
    case 'wait':
      return !(step.amount > 0);
    case 'stop_if_replied':
      return !step.after;
  }
}

export function triggerUnfinished(trigger: RoutineTrigger): boolean {
  switch (trigger.kind) {
    case 'schedule':
      return !trigger.cron;
    case 'email':
      return !trigger.lane;
    case 'page_change':
      return !trigger.url;
    case 'site_change':
      return !trigger.operation;
    default:
      return false;
  }
}

/**
 * "When a new order arrives, check the stock, then email the customer." Each phrase keeps the step
 * it came from, so the sentence and the board can point at each other.
 */
export function sentenceOf(definition: Pick<RoutineDefinition, 'trigger' | 'steps'>, name: (ref: string | null) => string | null = () => null, catalog?: RoutineCatalog): SentencePart[] {
  const opening: SentencePart = { text: capitalise(triggerPhrase(definition.trigger)), target: TRIGGER_TARGET, blank: triggerUnfinished(definition.trigger) };
  const steps = definition.steps.map((step) => stepPhrase(step, name, (item) => unfinished(item, catalog)));
  if (!steps.length) return [opening, { text: ', do nothing yet.' }];
  return [opening, { text: ', ' }, ...joined(steps), { text: '.' }];
}

export const sentenceText = (parts: SentencePart[]) => parts.map((part) => part.text).join('');

export interface DataSource {
  /** The step's id, or `trigger`. */
  step: string;
  label: string;
  fields: RoutineField[];
}

const operationOf = (catalog: RoutineCatalog | undefined, step: RoutineStep): RoutineOperation | null =>
  step.kind === 'action' && step.operation ? (catalog?.operations.find((item) => item.id === step.operation) ?? null) : null;

export const fieldsOfStep = (catalog: RoutineCatalog | undefined, step: RoutineStep): RoutineField[] => {
  if (step.kind === 'ask') return [{ key: 'answer', label: 'Their answer', type: 'yes_no' }];
  return operationOf(catalog, step)?.produces ?? [];
};

/** What this step can read: the trigger, the steps before it, and the item of a loop around it. */
export function dataBefore(definition: Pick<RoutineDefinition, 'trigger' | 'steps'>, id: string | null, catalog: RoutineCatalog | undefined): DataSource[] {
  const sources: DataSource[] = [{ step: TRIGGER_TARGET, label: capitalise(triggerPhrase(definition.trigger)), fields: catalog?.trigger_fields[definition.trigger.kind] ?? [] }];
  const walk = (steps: RoutineStep[], outer: DataSource[]): DataSource[] | null => {
    const seen = [...outer];
    for (const step of steps) {
      if (step.id === id) return seen;
      for (const arm of armsOf(step)) {
        const loopItem: DataSource[] = step.kind === 'loop' ? [{ step: step.id, label: `This ${step.item}`, fields: loopFields(definition, step.over, catalog) }] : [];
        const found = walk(armOf(step, arm), [...seen, ...loopItem]);
        if (found) return found;
      }
      const fields = fieldsOfStep(catalog, step);
      if (fields.length) seen.push({ step: step.id, label: capitalise(step.label), fields });
    }
    return id === null ? seen : null;
  };
  return (walk(definition.steps, sources) ?? sources).filter((source) => source.fields.length);
}

/** One item of a list has the fields the operation describes under the list, or a single value. */
function loopFields(definition: Pick<RoutineDefinition, 'trigger' | 'steps'>, over: string | null, catalog: RoutineCatalog | undefined): RoutineField[] {
  const ref = parseRef(over);
  if (!ref) return [];
  const step = findStep(definition.steps, ref.step);
  const produced = step ? fieldsOfStep(catalog, step) : (catalog?.trigger_fields[definition.trigger.kind] ?? []);
  const list = produced.find((field) => field.key === ref.field);
  const operation = step ? operationOf(catalog, step) : null;
  const named = operation?.produces.filter((field) => field.key.startsWith(`${ref.field}_`)) ?? [];
  if (named.length) return named.map((field) => ({ ...field, key: field.key.slice(ref.field.length + 1) }));
  return list ? [{ ...list, key: 'value', label: 'This one', type: 'text' }] : [];
}

/** A reference in everyday words: "Stock left". Null for something that is not a reference. */
export function refName(sources: DataSource[], ref: string | null | undefined): string | null {
  const parsed = parseRef(ref);
  if (!parsed) return null;
  const field = sources.find((item) => item.step === parsed.step)?.fields.find((item) => item.key === parsed.field);
  return field?.label ?? parsed.field.replaceAll('_', ' ');
}

/** Every reference in a definition, to tell a person when a change left one pointing at nothing. */
export function brokenRefs(definition: Pick<RoutineDefinition, 'trigger' | 'steps'>, catalog: RoutineCatalog | undefined): { step: string; ref: string }[] {
  const out: { step: string; ref: string }[] = [];
  for (const step of flatSteps(definition.steps)) {
    const sources = dataBefore(definition, step.id, catalog);
    for (const text of textsOf(step)) {
      for (const piece of piecesOf(text)) {
        if (!('ref' in piece)) continue;
        const known = sources.find((item) => item.step === piece.step)?.fields.some((item) => item.key === piece.field);
        if (!known) out.push({ step: step.id, ref: piece.ref });
      }
    }
  }
  return out;
}

function textsOf(step: RoutineStep): string[] {
  switch (step.kind) {
    case 'action':
      return Object.values(step.inputs);
    case 'branch':
      return [step.when.left ?? ''];
    case 'loop':
      return [step.over ?? ''];
    case 'ask':
      return [step.question];
    case 'notify':
      return [step.message];
    case 'wait':
    case 'stop_if_replied':
      return [];
  }
}

/** What stops a routine from being turned on, in the order a person would fix it. */
export interface Unfinished {
  target: string;
  words: string;
}

/**
 * A step that posts or commits, placed before the step that asks a person. It would run before anyone has said
 * yes, so it stays unfinished until it is moved after the question. The node's check says the same.
 */
export function actsBeforeAsking(definition: Pick<RoutineDefinition, 'steps'>, catalog: RoutineCatalog | undefined): Unfinished[] {
  const order = flatSteps(definition.steps);
  const ask = order.find((step) => step.kind === 'ask');
  if (!ask) return [];
  const out: Unfinished[] = [];
  for (const step of order) {
    if (step.id === ask.id) break;
    const effect = step.kind === 'action' ? operationOf(catalog, step)?.effect : undefined;
    if (effect && effect !== 'read' && effect !== 'draft') out.push({ target: step.id, words: `"${step.label}" would run before anyone is asked. Move it after "${ask.label}".` });
  }
  return out;
}

export function whatIsMissing(definition: RoutineDefinition, catalog: RoutineCatalog | undefined): Unfinished[] {
  const out: Unfinished[] = [];
  if (!definition.workspace) out.push({ target: TRIGGER_TARGET, words: 'Choose a business.' });
  if (triggerUnfinished(definition.trigger)) out.push({ target: TRIGGER_TARGET, words: 'Finish how it starts.' });
  if (!definition.steps.length) out.push({ target: TRIGGER_TARGET, words: 'Add a first step.' });
  for (const step of flatSteps(definition.steps)) {
    const params = step.kind === 'action' ? missingParams(step, catalog) : [];
    if (params.length) out.push({ target: step.id, words: `Fill in ${listWords(params)} for "${step.label}".` });
    else if (unfinished(step, catalog)) out.push({ target: step.id, words: `Finish "${step.label}".` });
    else if (step.kind === 'action' && isSendStep(step, catalog) && !step.send?.list) out.push({ target: step.id, words: `Choose who "${step.label}" sends to.` });
  }
  out.push(...actsBeforeAsking(definition, catalog));
  for (const broken of brokenRefs(definition, catalog)) out.push({ target: broken.step, words: `A value in "${findStep(definition.steps, broken.step)?.label ?? ''}" comes from a step that moved or is gone.` });
  return out;
}

export const isSendStep = (step: RoutineActionStep, catalog: RoutineCatalog | undefined) => Boolean(operationOf(catalog, step)?.sends);

export const operationFor = operationOf;

/** The definition alone, ready to read, keep in a repository and send back. */
export function fileOf(definition: RoutineDefinition): string {
  const { name, workspace, trigger, steps } = definition;
  return `${JSON.stringify({ name, workspace, trigger, steps }, null, 2)}\n`;
}

/** A template for a business: each step takes what that business has, or waits for a person to choose. */
export function fillTemplate(template: Pick<RoutineTemplate, 'name' | 'trigger' | 'steps'>, catalog: RoutineCatalog | undefined, workspace: string | null): RoutineDefinition {
  const fill = (steps: RoutineStep[]): RoutineStep[] =>
    steps.map((step) => {
      if (step.kind === 'action') {
        const tag = step.operation?.startsWith('tag:') ? step.operation.slice(4) : null;
        if (!tag) return step;
        const found = catalog?.operations.find((item) => item.workspace === workspace && item.tags.includes(tag));
        return { ...step, operation: found?.id ?? null };
      }
      if (step.kind === 'branch') return { ...step, then: fill(step.then), otherwise: fill(step.otherwise) };
      return step.kind === 'loop' ? { ...step, body: fill(step.body) } : step;
    });
  return { name: template.name, workspace, trigger: template.trigger, steps: fill(template.steps) };
}
