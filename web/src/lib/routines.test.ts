import type { RoutineCatalog, RoutineDefinition, RoutineStep } from '@/api/types';
import {
  blankStep,
  brokenRefs,
  dataBefore,
  flatSteps,
  insertStep,
  moveStep,
  nudgeStep,
  piecesOf,
  refName,
  removeStep,
  sentenceOf,
  sentenceText,
  slotOf,
  TOP,
  unfinished,
  updateStep,
  whatIsMissing,
} from './routines';

const action = (id: string, label: string, operation: string | null = 'op.stock'): RoutineStep => ({ id, kind: 'action', label, operation, inputs: {} });

const order: RoutineDefinition = {
  name: 'New orders',
  workspace: 'harbor',
  trigger: { kind: 'webhook', label: 'when a new order arrives' },
  steps: [action('stock', 'check the stock'), action('mail', 'email the customer', 'op.mail')],
};

const catalog: RoutineCatalog = {
  operations: [
    { id: 'op.stock', name: 'Stock', lane: 'harbor/shopify', workspace: 'harbor', effect: 'read', params: {}, produces: [{ key: 'left', label: 'Stock left', type: 'number' }], sends: false, tags: [] },
    { id: 'op.mail', name: 'Mail', lane: 'harbor/gmail', workspace: 'harbor', effect: 'commit', params: {}, produces: [], sends: true, tags: [] },
  ],
  lists: [],
  templates: [],
  trigger_fields: {
    webhook: [{ key: 'order_id', label: 'Order number', type: 'text' }, { key: 'note', label: 'Customer note', type: 'text', untrusted: true }],
    schedule: [],
    site_change: [],
    email: [],
    page_change: [],
    manual: [],
  },
};

describe('sentenceOf', () => {
  it('reads like a person said it', () => {
    expect(sentenceText(sentenceOf(order))).toBe('When a new order arrives, check the stock, then email the customer.');
  });

  it('says a decision and a list the way they run', () => {
    const steps: RoutineStep[] = [
      { id: 'd', kind: 'branch', label: 'decide', when: { left: '{{stock.left}}', op: 'more_than', right: '0' }, then: [action('a', 'email the customer')], otherwise: [{ id: 't', kind: 'notify', label: 'tell you', message: 'x' }] },
      { id: 'l', kind: 'loop', label: 'each', over: '{{stock.left}}', item: 'invoice', body: [action('b', 'send a reminder')] },
    ];
    const name = (ref: string | null) => (ref ? 'stock left' : null);
    expect(sentenceText(sentenceOf({ ...order, steps }, name))).toBe('When a new order arrives, if stock left is more than 0, email the customer, otherwise tell you, then for each invoice, send a reminder.');
  });

  it('marks what is still to choose, and keeps each phrase on its step', () => {
    const parts = sentenceOf({ ...order, steps: [action('stock', 'check the stock', null)] });
    expect(parts.find((part) => part.target === 'stock')?.blank).toBe(true);
    expect(parts[0]?.target).toBe('trigger');
  });

  it('never ends on a dangling comma when there are no steps', () => {
    expect(sentenceText(sentenceOf({ ...order, steps: [] }))).toBe('When a new order arrives, do nothing yet.');
  });
});

describe('editing the tree', () => {
  const nested: RoutineStep[] = [
    action('a', 'a'),
    { id: 'd', kind: 'branch', label: 'd', when: { left: null, op: 'is', right: '' }, then: [action('x', 'x'), action('y', 'y')], otherwise: [] },
    { id: 'l', kind: 'loop', label: 'l', over: null, item: 'i', body: [action('z', 'z')] },
  ];
  const ids = (steps: RoutineStep[]) => flatSteps(steps).map((step) => step.id).join(' ');

  it('moves down the same list, counting from where the step was', () => {
    expect(ids(moveStep(nested, 'a', { ...TOP, index: 2 }))).toBe('d x y a l z');
    expect(ids(moveStep(nested, 'a', { ...TOP, index: 0 }))).toBe(ids(nested));
    expect(ids(moveStep(nested, 'a', { ...TOP, index: 1 }))).toBe(ids(nested));
  });

  it('moves between lists, in and out of a decision or a loop', () => {
    expect(ids(moveStep(nested, 'x', { parent: 'd', arm: 'otherwise', index: 0 }))).toBe('a d y x l z');
    expect(ids(moveStep(nested, 'z', { ...TOP, index: 0 }))).toBe('z a d x y l');
    expect(ids(moveStep(nested, 'a', { parent: 'l', arm: 'body', index: 1 }))).toBe('d x y l z a');
  });

  it('refuses to put a step inside itself', () => {
    expect(moveStep(nested, 'd', { parent: 'd', arm: 'then', index: 0 })).toBe(nested);
  });

  it('nudges within a list and stops at the ends', () => {
    expect(ids(nudgeStep(nested, 'y', -1) ?? [])).toBe('a d y x l z');
    expect(ids(nudgeStep(nested, 'x', 1) ?? [])).toBe('a d y x l z');
    expect(nudgeStep(nested, 'x', -1)).toBeNull();
    expect(nudgeStep(nested, 'l', 1)).toBeNull();
  });

  it('inserts, updates and removes without touching what it was given', () => {
    const before = JSON.stringify(nested);
    const added = insertStep(nested, { parent: 'l', arm: 'body', index: 0 }, blankStep('wait', 'w'));
    expect(ids(added)).toBe('a d x y l w z');
    expect(ids(removeStep(added, 'd'))).toBe('a l w z');
    expect(flatSteps(updateStep(nested, 'z', (step) => ({ ...step, label: 'new' }))).at(-1)?.label).toBe('new');
    expect(JSON.stringify(nested)).toBe(before);
    expect(slotOf(nested, 'y')).toEqual({ parent: 'd', arm: 'then', index: 1 });
  });
});

describe('data between steps', () => {
  it('offers the trigger and what came before, and nothing from later', () => {
    const before = dataBefore(order, 'mail', catalog);
    expect(before.map((source) => source.step)).toEqual(['trigger', 'stock']);
    expect(dataBefore(order, 'stock', catalog).map((source) => source.step)).toEqual(['trigger']);
  });

  it('names a reference in words and finds one that lost its source', () => {
    expect(refName(dataBefore(order, 'mail', catalog), '{{stock.left}}')).toBe('Stock left');
    const swapped: RoutineDefinition = { ...order, steps: [{ id: 'mail', kind: 'notify', label: 'tell you', message: 'Only {{stock.left}} left' }, action('stock', 'check the stock')] };
    expect(brokenRefs(swapped, catalog)).toEqual([{ step: 'mail', ref: '{{stock.left}}' }]);
  });

  it('splits text around the data in it', () => {
    expect(piecesOf('Hi {{trigger.name}}, thanks')).toEqual([{ text: 'Hi ' }, { ref: '{{trigger.name}}', step: 'trigger', field: 'name' }, { text: ', thanks' }]);
  });
});

describe('whatIsMissing', () => {
  it('says what stops it from starting, and a send step needs a list', () => {
    expect(whatIsMissing(order, catalog).map((item) => item.words)).toEqual(['Choose who "email the customer" sends to.']);
    const fixed: RoutineDefinition = { ...order, steps: [action('stock', 'check the stock'), { id: 'mail', kind: 'action', label: 'email the customer', operation: 'op.mail', inputs: {}, send: { list: 'l1' } }] };
    expect(whatIsMissing(fixed, catalog)).toEqual([]);
    expect(whatIsMissing({ ...order, steps: [], workspace: null }, catalog).map((item) => item.words)).toEqual(['Choose a business.', 'Add a first step.']);
  });
});

describe('what a routine needs before it can be turned on', () => {
  const photo: RoutineCatalog = {
    ...catalog,
    operations: [
      ...catalog.operations,
      {
        id: 'op.photo', name: 'Post a photo', lane: 'harbor/social', workspace: 'harbor', effect: 'commit', sends: false, tags: [], produces: [],
        params: { caption: { type: 'string', required: true }, image: { type: 'string', label: 'picture', required: true }, note: { type: 'string' } },
      },
    ],
  };
  const post = (inputs: Record<string, string>): RoutineStep => ({ id: 'post', kind: 'action', label: 'post it', operation: 'op.photo', inputs });
  const ask: RoutineStep = { id: 'read', kind: 'ask', label: 'ask you to read it', question: 'Is it ready?' };

  it('names each param a step needs and leaves empty, and an optional one never blocks', () => {
    expect(whatIsMissing({ ...order, steps: [post({ caption: '  ' })] }, photo)).toEqual([{ target: 'post', words: 'Fill in caption and picture for "post it".' }]);
    expect(whatIsMissing({ ...order, steps: [post({ caption: 'Hi' })] }, photo)).toEqual([{ target: 'post', words: 'Fill in picture for "post it".' }]);
    expect(unfinished(post({ caption: 'Hi', image: 'a.jpg' }), photo)).toBe(false);
    expect(unfinished(post({ caption: '', image: 'a.jpg' }), photo)).toBe(true);
    expect(whatIsMissing({ ...order, steps: [post({ caption: 'Hi', image: 'a.jpg' })] }, photo)).toEqual([]);
  });

  it('flags a step that posts when it comes before the question, and says where it goes', () => {
    const full = post({ caption: 'Hi', image: 'a.jpg' });
    expect(whatIsMissing({ ...order, steps: [full, ask] }, photo)).toEqual([
      { target: 'post', words: '"post it" would run before anyone is asked. Move it after "ask you to read it".' },
    ]);
    expect(whatIsMissing({ ...order, steps: [ask, full] }, photo)).toEqual([]);
  });

  it('does not count a step that only reads, or a routine with no question', () => {
    const reads: RoutineCatalog = { ...photo, operations: photo.operations.map((item) => (item.id === 'op.photo' ? { ...item, effect: 'read' } : item)) };
    expect(whatIsMissing({ ...order, steps: [post({ caption: 'Hi', image: 'a.jpg' }), ask] }, reads)).toEqual([]);
    expect(whatIsMissing({ ...order, steps: [post({ caption: 'Hi', image: 'a.jpg' })] }, photo)).toEqual([]);
  });
});
