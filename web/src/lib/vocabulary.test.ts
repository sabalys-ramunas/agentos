import type { Effect } from '@/api/types';
import { actionVerbs, EFFECT_WORDS, GLOSSARY, PLACES, THINGS } from './vocabulary';

describe('vocabulary', () => {
  it('holds the names the owner decided, with the technical term behind each', () => {
    expect(PLACES.accounts).toMatchObject({ word: 'Accounts', technical: 'Lanes' });
    expect(PLACES.routines).toMatchObject({ word: 'Routines', technical: 'Flows' });
    expect(PLACES.history).toMatchObject({ word: 'History', technical: 'Activity' });
    expect(PLACES.machines).toMatchObject({ word: 'Machines', technical: 'Nodes' });
    // The old key reads the new word, so a screen not yet moved never says Computers.
    expect(PLACES.computers.word).toBe('Machines');
    expect(PLACES.runs.word).toBe('Runs');
    expect(PLACES.checks.word).toBe('Checks');
    expect(PLACES.brand.word).toBe('Brand');
    expect(PLACES.needsYou.word).toBe('Needs you');
    expect(THINGS.inspect.word).toBe('Technical details');
  });

  it('gives every effect an action verb, never Approve or Deny', () => {
    for (const effect of Object.keys(EFFECT_WORDS) as Effect[]) {
      const verbs = actionVerbs(effect);
      expect(verbs.yes).not.toMatch(/approve/i);
      expect(verbs.no).not.toMatch(/deny/i);
    }
    expect(actionVerbs('commit')).toMatchObject({ yes: 'Post it', no: "Don't post" });
    expect(actionVerbs('pay', { amount: '$40' }).yes).toBe('Pay $40');
    expect(actionVerbs('irreversible', { count: 3, noun: 'pages' })).toMatchObject({ yes: 'Delete 3 pages', no: 'Keep them' });
  });

  it('keeps the whole glossary, with no em dash in anything a person reads', () => {
    expect(GLOSSARY.length).toBeGreaterThan(50);
    const dash = String.fromCharCode(0x2014);
    expect(JSON.stringify(GLOSSARY).includes(dash)).toBe(false);
  });
});
