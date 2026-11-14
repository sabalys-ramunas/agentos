import type { Approval } from '@/api/types';
import { batchKind, kindOf } from './kinds';
import { amountOf, blockerOf, deletionOf, factsOf, outcomeLine, questionOf, scaleOf, verbsOf, whereOf, whyAsked } from './words';

const base: Approval = {
  id: 'ap_1',
  state: 'pending',
  effect: 'commit',
  lane: 'aster/instagram',
  summary: 'Post 1 photo with caption to instagram.com as @astercoffee',
  content_hash: 'sha256:x',
  preview: { text: 'Autumn blend', image: '/preview.png' },
  requested_by: { kind: 'agent', id: 'claude-code' },
  created_at: new Date(Date.now() - 4 * 60_000).toISOString(),
  expires_at: new Date(Date.now() + 60 * 60_000).toISOString(),
  site: { key: 'instagram', label: 'Instagram', domain: 'instagram.com' },
};

describe('an approval in plain words', () => {
  it('asks the question with the business and the site', () => {
    const where = whereOf(base, 'Aster Coffee');
    expect(where).toMatchObject({ place: "Aster Coffee's Instagram", handle: '@astercoffee' });
    expect(questionOf(base, where, 'Claude Code')).toBe("Post this photo on Aster Coffee's Instagram?");
    expect(questionOf({ ...base, summary: 'Post 4 photos with caption to instagram.com' }, where, 'Claude Code')).toBe("Post these 4 photos on Aster Coffee's Instagram?");
  });

  it('prefers the business name the panel knows over an old slug from the cloud', () => {
    expect(whereOf({ ...base, workspace_name: 'Aster' }, 'Aster Coffee').place).toBe("Aster Coffee's Instagram");
    expect(whereOf({ ...base, workspace_name: 'Aster Coffee' }, 'aster').place).toBe("Aster Coffee's Instagram");
  });

  it('names the buttons for the action, and says a blocked yes waits', () => {
    const where = whereOf(base, 'Aster Coffee');
    expect(verbsOf(base, where)).toMatchObject({ yes: 'Post it', no: "Don't post" });
    expect(verbsOf(base, where, 'quarantined').yes).toBe('Post it once fixed');
    expect(verbsOf(base, where, 'logged_out').yes).toBe('Post it once signed in');
    const pay = { ...base, effect: 'pay' as const, summary: 'Boost 1 post on facebook.com for $40.00 over 5 days' };
    expect(verbsOf(pay, where)).toMatchObject({ yes: 'Pay $40', no: "Don't pay" });
    const gone = { ...base, effect: 'irreversible' as const, summary: 'Delete 3 draft pages from the harbor blog' };
    expect(verbsOf(gone, where)).toMatchObject({ yes: 'Delete 3 pages', no: 'Keep them' });
    const story = { ...base, lane: 'solstice/medium', site: { key: 'medium', label: 'Medium', domain: 'medium.com' } };
    expect(verbsOf(story, whereOf(story, 'Solstice Studio')).yes).toBe('Publish it');
  });

  it('reads amounts and deletions from the request', () => {
    expect(amountOf('Boost for $40.00 over 5 days')).toBe('$40');
    expect(amountOf('Pay $12.50')).toBe('$12.50');
    expect(deletionOf('Delete 3 draft pages from the harbor blog')).toEqual({ count: 3, noun: 'draft pages' });
  });

  it('draws how big it is', () => {
    expect([scaleOf('read'), scaleOf('commit'), scaleOf('pay'), scaleOf('irreversible')]).toEqual([0, 1, 2, 3]);
  });

  it('gives the four plain facts', () => {
    const facts = factsOf(base, whereOf(base, 'Aster Coffee'), 'Claude Code');
    expect(facts.map((fact) => fact.title)).toEqual([
      'Claude Code asked 4 minutes ago',
      'Anyone on Instagram can see it',
      'You can delete it later',
      expect.stringMatching(/^Waits for you until (today|tomorrow) at \d\d:\d\d$/),
    ]);
    expect(facts[1]?.detail).toBe('Public, on instagram.com/astercoffee');
  });

  it('says first why it asks when a switch is why', () => {
    const asked = { ...base, reason: 'Ask me first is on for routine aster.instagram.post' };
    const [why] = factsOf(asked, whereOf(asked, 'Aster Coffee'), 'Claude Code');
    expect(why).toMatchObject({ key: 'why', title: 'Someone asked to see this first', detail: 'Ask me first is on for this routine.' });
    expect(whyAsked({ reason: 'Ask me first is on for aster/gmail.', effect: 'commit' })?.detail).toBe('Ask me first is on.');
    expect(whyAsked({ reason: 'Ask me first is on', effect: 'commit' })?.kind).toBe('chosen');
    expect(whyAsked({ reason: 'it does not act on one account, so a person decides', effect: 'commit' })?.kind).toBe('always');
  });

  it('says money and what cannot be undone always ask, whatever the node wrote', () => {
    expect(whyAsked({ reason: 'always a human co-sign', effect: 'pay' })).toMatchObject({ kind: 'always', title: 'Money always asks' });
    expect(whyAsked({ reason: null, effect: 'irreversible' })).toMatchObject({ kind: 'always', title: "This can't be undone, so it always asks" });
  });

  it('puts a safety check into plain words, and leaves the row of reasons to the notice', () => {
    const reason = 'it follows untrusted text from a web page at evil.example; Ask me first is on for routine x';
    const why = whyAsked({ reason, effect: 'commit' });
    expect(why).toMatchObject({ kind: 'safety', title: 'A safety check stopped this' });
    expect(why?.detail).toBe('This would normally run on its own. It read text from a web page at evil.example, which nobody here wrote and which could be steering it.');
    const asked = { ...base, reason };
    expect(factsOf(asked, whereOf(asked, 'Aster Coffee'), 'Claude Code').some((fact) => fact.key === 'why')).toBe(false);
  });

  it('says the lethal trifecta without the jargon', () => {
    const reason = 'this agent read untrusted content and private data, and this would send to a site outside its allowed list; this call sends data to files.example';
    const why = whyAsked({ reason, effect: 'commit' });
    expect(why?.kind).toBe('safety');
    expect(why?.detail).toBe('This would normally run on its own. It read private information and text from outside. This would send data to files.example it has not been allowed to reach.');
    expect(why?.detail).not.toMatch(/trifecta|egress|allowlist|grant/);
  });

  it('says nothing it cannot say plainly: the node\'s words stay in the technical details', () => {
    expect(whyAsked({ reason: 'first use per lane per UTC day', effect: 'secret' })).toBeNull();
    expect(whyAsked({ reason: null, effect: 'commit' })).toBeNull();
  });

  it('warns before a yes when the account cannot act, with the fix', () => {
    const blocked = { ...base, lane_state: { state: 'logged_out' as const, blocks: true, reason: 'The account is signed out.', warning: 'x' } };
    expect(blockerOf(blocked, whereOf(blocked, 'Aster Coffee'))).toEqual({
      state: 'logged_out',
      sentence: "Aster Coffee's Instagram is signed out, so if you say yes, it will not post until someone signs in.",
      fix: 'Sign in',
    });
    expect(blockerOf({ ...base, lane_state: { state: 'ok', blocks: false, reason: null, warning: null } }, whereOf(base, 'Aster Coffee'))).toBeNull();
  });

  it('says a failure with its reason, never as done', () => {
    const line = outcomeLine({
      state: 'failed',
      title: "Couldn't post to Aster Coffee's Instagram",
      reason: 'The account is switched off for safety.',
      detail: null,
      source: 'run',
      at: '',
      run_id: null,
      proof: null,
      cleared_at: null,
      cleared_by: null,
    });
    expect(line).toBe("Couldn't post to Aster Coffee's Instagram: the account is switched off for safety.");
  });
});

describe('every kind of job, in its own words', () => {
  const of = (patch: Partial<Approval>) => ({ ...base, preview: { text: null, image: null }, ...patch }) as Approval;
  const words = (approval: Approval, business = 'Aster Coffee') => {
    const where = whereOf(approval, business);
    return { question: questionOf(approval, where, 'Claude'), ...verbsOf(approval, where) };
  };

  it('sends an email to whom it is addressed', () => {
    const email = of({ lane: 'aster/gmail', site: null, preview: { kind: 'email', data: { to: ['accounts@beanco.example'], subject: 'Invoice 1182' } } });
    expect(words(email)).toMatchObject({ question: 'Send this email to accounts@beanco.example?', yes: 'Send email', no: "Don't send" });
  });

  it('pays a named payee the exact amount', () => {
    const pay = of({ effect: 'pay', preview: { kind: 'payment', data: { amount: 412.5, currency: 'EUR', payee: 'Nordic Ropeworks' } } });
    expect(words(pay)).toMatchObject({ question: 'Pay €412.50 to Nordic Ropeworks?', yes: 'Pay €412.50', no: "Don't pay" });
  });

  it('submits a form, refunds an order, replies in a thread, deletes exactly what is listed and runs a command', () => {
    const form = of({ preview: { kind: 'form', data: { form: 'VAT return, October', fields: [] } } });
    expect(words(form)).toMatchObject({ yes: 'Submit', no: "Don't submit" });
    const refund = of({ effect: 'pay', preview: { kind: 'order', data: { amount: 24, currency: 'EUR', customer: 'Lina Berg', order: '#1432' } } });
    expect(words(refund)).toMatchObject({ question: 'Refund €24 to Lina Berg?', yes: 'Refund €24', no: "Don't refund" });
    const reply = of({ preview: { kind: 'message', data: { thread: [{ from: 'Jonas Petraitis', text: 'Lovely trip' }], reply: 'Thank you' } } });
    expect(words(reply)).toMatchObject({ question: 'Send this reply to Jonas Petraitis?', yes: 'Send reply' });
    const gone = of({ effect: 'irreversible', preview: { kind: 'delete', data: { computer: 'home-mini', items: [{ name: '/a.zip' }, { name: '/b.mov' }] } } });
    expect(words(gone)).toMatchObject({ question: 'Delete 2 files on home-mini?', yes: 'Delete 2 files', no: 'Keep them' });
    const command = of({ effect: 'exec', preview: { kind: 'command', data: { computer: 'home-mini', command: 'restic backup' } } });
    expect(words(command)).toMatchObject({ question: 'Run this command on home-mini?', yes: 'Run it', no: "Don't run" });
  });

  it('reads the kind from the effect and the site when the cloud does not say it', () => {
    expect(kindOf(of({ lane: 'aster/gmail', site: null }))).toBe('email');
    expect(kindOf(base)).toBe('post');
    expect(kindOf(of({ effect: 'exec' }))).toBe('command');
  });

  it('batches routine posts and replies only, never email or money', () => {
    expect(batchKind(base)).toBe(true);
    expect(batchKind(of({ preview: { kind: 'message', data: { thread: [] } } }))).toBe(true);
    expect(batchKind(of({ lane: 'aster/gmail', site: null, preview: { kind: 'email' } }))).toBe(false);
    expect(batchKind(of({ effect: 'pay' }))).toBe(false);
  });
});
