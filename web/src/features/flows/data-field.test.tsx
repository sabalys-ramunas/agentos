import { fireEvent, render, screen } from '@testing-library/react';
import type { RoutineActionStep, RoutineCatalog, RoutineStep } from '@/api/types';
import type { DataSource } from '@/lib/routines';
import { DataField, withoutDeadRefs } from './data-field';
import { changeAction } from './inspector';

const SOURCES: DataSource[] = [
  { step: 'trigger', label: 'When a new order arrives', fields: [{ key: 'name', label: 'Customer name', type: 'text' }] },
  { step: 'stock', label: 'Check the stock', fields: [{ key: 'left', label: 'Stock left', type: 'number' }] },
];

describe('Data in a field (B8)', () => {
  it('takes out a reference that no longer points at anything a step can read', () => {
    expect(withoutDeadRefs('Hi {{trigger.name}}, {{gone.field}} left', SOURCES)).toBe('Hi {{trigger.name}},  left');
  });

  it('removes a data chip with its own button, and says so in the value', () => {
    const changes: string[] = [];
    render(<DataField label="Message" value="Hi {{trigger.name}}, thanks" sources={SOURCES} onChange={(value) => changes.push(value)} />);
    fireEvent.click(screen.getByRole('button', { name: 'Remove Customer name' }));
    expect(changes).toEqual(['Hi , thanks']);
  });
});

describe('Changing what a step does (B8)', () => {
  const catalog = {
    operations: [
      { id: 'mail.send', name: 'Send mail', lane: 'biz-a/mail', workspace: 'biz-a', effect: 'commit', params: { subject: { type: 'string' }, body: { type: 'string' } }, produces: [], sends: true, tags: [] },
      { id: 'page.read', name: 'Read a page', lane: 'biz-a/web', workspace: 'biz-a', effect: 'read', params: { url: { type: 'string' } }, produces: [], sends: false, tags: [] },
    ],
  } as unknown as RoutineCatalog;
  const step = {
    id: 'act', kind: 'action', label: 'send it', operation: 'mail.send', inputs: { subject: 'Hi {{trigger.name}}', body: 'Left over {{gone.field}}' }, send: { list: 'list_1' },
  } as unknown as RoutineStep;

  it('keeps only the values the new action takes, drops what no longer applies, and drops the send list', () => {
    const next = changeAction(step, 'page.read', catalog, SOURCES) as RoutineActionStep;
    expect(next.operation).toBe('page.read');
    expect(next.inputs).toEqual({});
    expect(next.send).toBeUndefined();
  });

  it('keeps a value the new action still takes, without a reference that points at nothing', () => {
    const next = changeAction(step, 'mail.send', catalog, SOURCES) as RoutineActionStep;
    expect(next.inputs).toEqual({ subject: 'Hi {{trigger.name}}', body: 'Left over ' });
    expect(next.send).toEqual({ list: 'list_1' });
  });
});
