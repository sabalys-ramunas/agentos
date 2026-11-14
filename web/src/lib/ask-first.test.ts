import type { Flow, LaneAskFirst } from '@/api/types';
import { can } from './permissions';
import { accountAsk, ASKS, flowAccountSet, flowSet, routineAsk, RUNS, WAITS, whoSetIt, whoSetItShort, whyWords } from './ask-first';

const flow = (over: Partial<Flow> = {}): Flow => ({
  id: 'aster.gmail.newsletter',
  lane: 'aster/gmail',
  version: 1,
  params: {},
  effect: 'commit',
  last_run: null,
  ask_first: false,
  account_asks_first: false,
  always_asks: false,
  asks_first: false,
  asks_because: null,
  ask_first_supported: true,
  ask_first_online: true,
  ...over,
});

const account = (over: Partial<LaneAskFirst> = {}): LaneAskFirst => ({
  lane: 'aster/gmail',
  ask_first: false,
  asks_first: false,
  ask_first_set_by: null,
  ask_first_set_at: null,
  ask_first_supported: true,
  ask_first_online: true,
  ...over,
});

const name = () => 'Claude Code';

describe('routineAsk', () => {
  it('says what will really happen, and only offers a switch that would change it', () => {
    expect(routineAsk(flow())).toMatchObject({ word: RUNS, asks: false, switchable: true, checked: false });
    expect(routineAsk(flow({ ask_first: true, asks_first: true, asks_because: 'routine' }))).toMatchObject({ word: ASKS, switchable: true, checked: true });
  });

  it('never offers a switch on money or what cannot be undone, whatever the switch says', () => {
    for (const effect of ['pay', 'irreversible'] as const) {
      const view = routineAsk(flow({ effect, always_asks: true, asks_first: true, asks_because: 'always' }));
      expect(view).toMatchObject({ word: ASKS, switchable: false, fixed: true });
    }
    const off = routineAsk(flow({ effect: 'pay', always_asks: true, ask_first: false, asks_first: true, asks_because: 'always' }));
    expect(off?.word).toBe(ASKS);
  });

  it('offers no switch on a routine with nothing to ask about', () => {
    expect(routineAsk(flow({ effect: 'read' }))).toMatchObject({ word: RUNS, switchable: false, short: 'Only looks' });
    expect(routineAsk(flow({ effect: 'draft' }))).toMatchObject({ word: RUNS, switchable: false, short: 'Only drafts' });
  });

  it('points at the account when the account is what asks', () => {
    expect(routineAsk(flow({ account_asks_first: true, asks_first: true, asks_because: 'account' }))).toMatchObject({ word: ASKS, switchable: false, account: 'aster/gmail' });
  });

  it('tells the truth about a computer without the switch: it asks, and nothing here changes that', () => {
    const view = routineAsk(flow({ ask_first_supported: false, asks_first: true, asks_because: 'computer' }));
    expect(view).toMatchObject({ word: ASKS, switchable: false });
    expect(view?.sentence).toContain('older version');
  });

  it('draws no switch that cannot reach an offline computer, and keeps the last known state', () => {
    expect(routineAsk(flow({ ask_first_online: false }))).toMatchObject({ word: RUNS, switchable: false, short: 'Computer offline' });
    expect(routineAsk(flow({ ask_first_online: false, ask_first: true, asks_first: true, asks_because: 'routine' }))).toMatchObject({ word: ASKS, switchable: false });
  });

  it('says a publishing routine waits in Needs you for a yes when the person may not decide, and never that it runs on its own', () => {
    const member = routineAsk(flow({ effect: 'commit' }), false);
    expect(member).toMatchObject({ word: WAITS, asks: true, switchable: true, checked: false });
    expect(member?.sentence).toBe('It waits in Needs you for a yes each time before it publishes.');
    expect(routineAsk(flow({ effect: 'commit' }), true)).toMatchObject({ word: RUNS, asks: false });
    expect(routineAsk(flow({ effect: 'read' }), false)).toMatchObject({ word: RUNS, switchable: false });
    // Once it asks first, a member still gets the switch, and the words say who answers
    expect(routineAsk(flow({ effect: 'commit', ask_first: true, asks_first: true, asks_because: 'routine' }), false)).toMatchObject({ word: ASKS, checked: true });
    expect(routineAsk(flow({ effect: 'commit', ask_first: true, asks_first: true, asks_because: 'routine' }), false)?.sentence).toBe(
      'It waits in Needs you for a yes each time before it acts.',
    );
  });

  it('claims nothing for an older cloud that says nothing', () => {
    expect(routineAsk(flow({ ask_first: undefined }))).toBeNull();
    expect(accountAsk(undefined)).toBeNull();
  });
});

describe('accountAsk', () => {
  it('has the same switch, and the same truth about a computer without it', () => {
    expect(accountAsk(account())).toMatchObject({ word: RUNS, switchable: true, checked: false });
    expect(accountAsk(account({ ask_first: true, asks_first: true }))).toMatchObject({ word: ASKS, switchable: true, checked: true });
    expect(accountAsk(account({ ask_first_supported: false, asks_first: true }))).toMatchObject({ word: ASKS, switchable: false });
  });
});

describe('the moment a switch moves', () => {
  it('keeps a routine asking when its account still does', () => {
    const next = flowSet(flow({ ask_first: true, account_asks_first: true, asks_first: true, asks_because: 'routine' }), false);
    expect(next).toMatchObject({ ask_first: false, asks_first: true, asks_because: 'account' });
  });

  it('moves a routine with its account, but its own switch stays the reason', () => {
    expect(flowAccountSet(flow(), true)).toMatchObject({ asks_first: true, asks_because: 'account' });
    expect(flowAccountSet(flow({ ask_first: true, asks_first: true, asks_because: 'routine' }), true)).toMatchObject({ asks_because: 'routine' });
    expect(flowAccountSet(flow({ asks_first: true, asks_because: 'account', account_asks_first: true }), false)).toMatchObject({ asks_first: false, asks_because: null });
  });

  it('never makes a payment or a looking routine change with the account', () => {
    expect(flowAccountSet(flow({ effect: 'pay', always_asks: true, asks_first: true, asks_because: 'always' }), false)).toMatchObject({ asks_first: true, asks_because: 'always' });
    expect(flowAccountSet(flow({ effect: 'read' }), true)).toMatchObject({ asks_first: false, asks_because: null });
  });
});

describe('who set it', () => {
  it('names an agent, and says only a person can undo it', () => {
    expect(whoSetIt({ by: { kind: 'agent', id: 'claude-code' }, on: true }, name, '3h ago')).toBe('Claude Code turned this on, 3h ago. Only a person can turn it off.');
    expect(whoSetIt({ by: { kind: 'agent', id: 'claude-code' }, on: true }, name, '3h ago', true)).toBe('Claude Code turned this on, 3h ago.');
  });

  it('gives the computer\'s reason in the same line, before who can undo it', () => {
    const why = 'A person set another routine on this account to ask first.';
    expect(whoSetIt({ by: { kind: 'agent', id: 'claude-code' }, on: true, why }, name, '3h ago')).toBe(`Claude Code turned this on, 3h ago. ${why} Only a person can turn it off.`);
    expect(whoSetIt({ by: null, on: true, why }, name, null)).toBe(why);
  });

  it('says a person and the computer plainly, and nothing when nobody has', () => {
    expect(whoSetIt({ by: { kind: 'user', id: 'usr_1' }, on: false }, () => 'You', '2d ago')).toBe('You turned this off, 2d ago.');
    expect(whoSetIt({ by: { kind: 'computer', id: 'owner:cli' }, on: true }, name, '1h ago')).toBe('Set on the computer itself, 1h ago.');
    expect(whoSetIt({ by: null, on: false }, name, null)).toBeNull();
    expect(whoSetItShort({ by: { kind: 'agent', id: 'claude-code' }, on: true }, name)).toBe('Set by Claude Code');
    expect(whoSetItShort({ by: { kind: 'user', id: 'usr_1' }, on: false }, name)).toBeNull();
  });

  it('says a person set it from the command line, never "owner:cli"', () => {
    const you = () => 'you';
    expect(whoSetItShort({ by: { kind: 'user', id: 'owner', via: 'cli' }, on: true }, you)).toBe('Set by you, from the command line');
    expect(whoSetIt({ by: { kind: 'user', id: 'owner', via: 'cli' }, on: true }, () => 'You', '1h ago')).toBe('You turned this on, from the command line, 1h ago.');
  });
});

describe('whyWords', () => {
  it('turns the node\'s ids into the routine\'s name', () => {
    const said = whyWords('a person set routine aster.gmail.newsletter on aster/gmail to ask first, so a new routine an agent saves there starts the same way');
    expect(said).toBe('A person set Gmail newsletter to ask first, so this new routine on the same account started that way.');
    expect(said).not.toMatch(/aster/);
  });

  it('says anything else as a sentence, and nothing for nothing', () => {
    expect(whyWords('the owner asked for it')).toBe('The owner asked for it.');
    expect(whyWords('Already a sentence.')).toBe('Already a sentence.');
    expect(whyWords(null)).toBeNull();
    expect(whyWords('  ')).toBeNull();
  });
});

describe('who may change a switch', () => {
  it('matches the cloud: an admin turns it off, never a member, approver or viewer', () => {
    expect(can('owner', 'ask_first.off')).toBe(true);
    expect(can('admin', 'ask_first.off')).toBe(true);
    for (const role of ['member', 'approver', 'viewer'] as const) expect(can(role, 'ask_first.off')).toBe(false);
    expect(can('member', 'ask_first.on')).toBe(true);
    for (const role of ['approver', 'viewer'] as const) expect(can(role, 'ask_first.on')).toBe(false);
  });
});
