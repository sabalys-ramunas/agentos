import { routineRunOf } from './post-preview';

describe("a routine's run waiting for a yes", () => {
  it('says the routine and its inputs in words, never the JSON the node sends', () => {
    const text = JSON.stringify({
      workflow: 'biz-a.routine.verify-one-publish',
      name: 'Verify one publish',
      values: { caption: 'Hello', post_date: '2026-11-07', pin: true, tags: ['coffee', 'autumn'], note: '' },
    });
    expect(routineRunOf(text)).toEqual({
      name: 'Verify one publish',
      inputs: ['Caption: Hello', 'Post date: 2026-11-07', 'Pin: Yes', 'Tags: coffee, autumn', 'Note: Not set'],
    });
  });

  it('says a value the node held back as a secret is not shown, and still reads the rest', () => {
    // The node writes a held-back value as a bare [redacted], which is not JSON, so it is read here
    const text = '{\n  "workflow": "biz-a.routine.verify-one-publish",\n  "name": "Verify one publish",\n  "values": {\n    "caption": "Hello",\n    "pin": [redacted]\n  }\n}';
    expect(routineRunOf(text)).toEqual({ name: 'Verify one publish', inputs: ['Caption: Hello', 'Pin: Not shown'] });
  });

  it('names a routine with no inputs by its name alone', () => {
    expect(routineRunOf(JSON.stringify({ workflow: 'biz-a.routine.verify-one-publish', name: 'Verify one publish', values: {} })))
      .toEqual({ name: 'Verify one publish', inputs: [] });
  });

  it('leaves every other preview to its own card', () => {
    expect(routineRunOf(null)).toBeNull();
    expect(routineRunOf('A plain post, not JSON')).toBeNull();
    expect(routineRunOf('{"text": "no workflow here"}')).toBeNull();
    expect(routineRunOf('[1, 2]')).toBeNull();
  });
});
