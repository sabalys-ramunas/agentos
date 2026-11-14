import type { ConfirmationSetting, ConfirmDay } from '@/api/types';
import { businessRungs, FLOORS, needsProof, reaches, rungIndex, rungsOf, RUNG_WORDS, RUNGS, shapeOf, stopRung, withBusinessRung, withRung, yesSentence, yesShort } from './confirm';

const SYSTEM: ConfirmationSetting = {
  commit: { when: 'auto', level: 'chat' },
  pay: { when: 'ask', level: 'present' },
  irreversible: { when: 'ask', level: 'present' },
  after_untrusted: { level: 'signed_in' },
  chat: true,
};

const DAY: ConfirmDay = {
  label: 'Yesterday',
  date: '2026-11-04',
  ticks: [
    { minute: 60, business: 'aster', kind: 'look' },
    { minute: 70, business: 'aster', kind: 'commit' },
    { minute: 80, business: 'harbor', kind: 'commit' },
  ],
  stops: [
    { id: 'a', minute: 90, row: 'marked', business: 'aster', site: 'instagram', title: 'Post', who: 'Tomas Reyes', said: 'yes', method: 'chat', where: 'Claude Code' },
    { id: 'b', minute: 100, row: 'pay', business: 'harbor', site: 'quickbooks', title: 'Pay', who: 'Demo Owner', said: 'yes', method: 'mac', where: null },
  ],
};

describe('how strong a yes is', () => {
  it('starts as the owner decided: most on their own, money at a proof', () => {
    expect(rungsOf(SYSTEM)).toEqual({ commit: 'auto', marked: 'chat', after_untrusted: 'signed_in', pay: 'present', irreversible: 'present' });
  });

  it('never reads a level name to a person', () => {
    for (const rung of RUNGS) expect(RUNG_WORDS[rung].word).not.toMatch(/signed_in|device_key|present/);
  });

  it('keeps chat out of money, what cannot be undone and anything after outside words', () => {
    for (const row of ['pay', 'irreversible', 'after_untrusted'] as const) expect(rungIndex(FLOORS[row])).toBeGreaterThan(rungIndex('chat'));
  });

  it('counts a stronger yes for a weaker ask, and never the other way', () => {
    expect(reaches('device_key', 'present')).toBe(true);
    expect(reaches('signed_in', 'present')).toBe(false);
    expect(needsProof({ requires: 'present' })).toBe(true);
    expect(needsProof({ requires: 'signed_in' })).toBe(false);
    expect(needsProof({ requires: undefined })).toBe(false);
  });

  it('moves posts off on-its-own by making them ask, and back by leaving the level alone', () => {
    const asks = withRung(SYSTEM, 'commit', 'signed_in');
    expect(asks.commit).toEqual({ when: 'ask', level: 'signed_in' });
    expect(withRung(asks, 'commit', 'auto').commit).toEqual({ when: 'auto', level: 'signed_in' });
  });
});

describe('a business', () => {
  it('can ask for more', () => {
    expect(businessRungs(SYSTEM, { commit: { when: 'auto', level: 'signed_in' } }).marked).toBe('signed_in');
  });

  it('can never come out lower than the org, whatever it holds', () => {
    const rungs = businessRungs(SYSTEM, { pay: { when: 'ask', level: 'chat' }, after_untrusted: { level: 'chat' } });
    expect(rungs.pay).toBe('present');
    expect(rungs.after_untrusted).toBe('signed_in');
  });

  it('keeps only what it asks for beyond the org', () => {
    expect(withBusinessRung(SYSTEM, undefined, 'marked', 'signed_in')).toEqual({ commit: { when: 'auto', level: 'signed_in' } });
    expect(withBusinessRung(SYSTEM, { commit: { when: 'auto', level: 'signed_in' } }, 'marked', 'chat')).toEqual({});
  });
});

describe('the shape of a day', () => {
  it('counts what flowed and what stopped', () => {
    const shape = shapeOf(DAY, rungsOf(SYSTEM));
    expect(shape).toMatchObject({ total: 5, own: 3, stopped: 2 });
    expect(shape.lanes).toEqual({ auto: 3, chat: 1, signed_in: 0, present: 1 });
  });

  it('moves everything that goes out to a person once posts ask', () => {
    const shape = shapeOf(DAY, rungsOf(withRung(SYSTEM, 'commit', 'chat')));
    expect(shape).toMatchObject({ own: 1, stopped: 4, heldRung: 'chat' });
    expect(shape.lanes.chat).toBe(3);
  });

  it('reads one business alone', () => {
    expect(shapeOf(DAY, rungsOf(SYSTEM), 'harbor')).toMatchObject({ total: 2, own: 1, stopped: 1 });
  });

  it('never lets an outside-words stop sit under posts when posts ask for more', () => {
    expect(stopRung('after_untrusted', { ...rungsOf(SYSTEM), commit: 'present' })).toBe('present');
  });
});

describe('a yes on the record', () => {
  it('says who, where and with what', () => {
    expect(yesSentence('Ines Okafor', 'chat', 'Claude Code')).toBe('Ines Okafor said yes in a chat in Claude Code');
    expect(yesSentence('Demo Owner', 'mac', "Demo's MacBook Pro")).toBe("Demo Owner said yes on Demo's MacBook Pro");
    expect(yesShort('Mara Lindqvist', 'passkey')).toBe('Mara Lindqvist said yes with a passkey');
  });
});
