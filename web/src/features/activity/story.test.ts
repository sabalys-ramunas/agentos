import type { HistoryDay, HistoryStory } from '@/api/types';
import { entriesOf, quietLine } from './feed';
import { stepWords, storyOf } from './story';
import { artifactOf, fromHistory, kindOf, titleOf } from './story-view';

const NAMES: Record<string, string> = { aster: 'Aster Coffee', harbor: 'Harbor Supply', meridian: 'Meridian Travel', personal: 'Personal' };
const tell = (lane: string, effect: string, status: string, summary: string) =>
  storyOf({ lane, effect: effect as never, status: status as never, summary }, (id) => NAMES[id]);

describe('a run in words', () => {
  it.each([
    ['meridian/instagram', 'commit', 'succeeded_verified', 'Post 1 photo with caption to instagram.com as @meridiantravel. Readback confirmed it.', "Posted a photo on Meridian Travel's Instagram", 'Live, checked'],
    ['meridian/linkedin', 'commit', 'succeeded_verified', 'Post 1 update to linkedin.com as meridian. Readback confirmed it.', "Posted an update on Meridian Travel's LinkedIn", 'Live, checked'],
    ['aster/medium', 'read', 'succeeded_verified', 'Check medium.com as @astercoffee. Readback confirmed it.', "Checked Aster Coffee's Medium", 'Checked'],
    ['aster/blog', 'read', 'succeeded_verified', 'Check blog.aster.example as aster. Readback confirmed it.', "Checked Aster Coffee's blog", 'Checked'],
    ['harbor/medium', 'commit', 'cancelled', 'Not run: no decision before it expired', "Did not post on Harbor Supply's Medium", 'Stopped'],
    ['aster/facebook', 'read', 'failed', 'Check facebook.com as @astercoffee. Failed: the lane signal is captcha.', "Could not check Aster Coffee's Facebook", 'Did not work'],
    ['personal/linkedin', 'commit', 'succeeded_unverified', 'Post 1 update to linkedin.com as @personal. Submitted, but the readback found nothing yet.', 'Posted an update on your personal LinkedIn', 'Not checked'],
    // Older data says it in the past already, with the workspace slug on the end.
    ['aster/instagram', 'commit', 'succeeded_verified', 'Posted 1 photo to Instagram as aster', "Posted a photo on Aster Coffee's Instagram", 'Live, checked'],
    ['harbor/facebook', 'commit', 'failed', 'Failed: Published 1 post to Facebook as harbor', "Could not publish a post on Harbor Supply's Facebook", 'Did not work'],
    ['harbor/blog', 'commit', 'succeeded_verified', 'Replied to 3 comments on Blog', "Replied to 3 comments on Harbor Supply's blog", 'Live, checked'],
  ])('%s: %s', (lane, effect, status, summary, sentence, label) => {
    const story = tell(lane, effect, status, summary);
    expect(story.sentence).toBe(sentence);
    expect(story.label).toBe(label);
    expect(story.sentence).not.toMatch(/@|\bas (aster|harbor|meridian)\b|\.com/);
  });

  it('says what did not happen and why, in plain words', () => {
    expect(tell('aster/facebook', 'read', 'failed', 'Check facebook.com as aster. Failed: the lane signal is captcha.').note).toBe('Facebook wanted a robot check, so the agent stopped.');
    const quarantined = tell('aster/instagram', 'commit', 'failed', 'Post 1 photo with caption to instagram.com as aster. Failed: lane aster/instagram is quarantined: self-check failed');
    expect(quarantined.note).toBe('Nothing was posted. The account is switched off for safety.');
    expect(quarantined.accountProblem).toBe(true);
    expect(tell('harbor/medium', 'commit', 'cancelled', 'Not run: no decision before it expired').note).toBe('Nobody answered in time, so nothing was posted.');
  });

  it('folds only reads that went fine', () => {
    expect(tell('aster/medium', 'read', 'succeeded_verified', 'Check medium.com as aster. Readback confirmed it.').routineRead).toBe(true);
    expect(tell('aster/medium', 'read', 'failed', 'Check medium.com as aster. Failed: the page changed.').routineRead).toBe(false);
  });

  it('ticks the steps off in words', () => {
    const story = tell('harbor/instagram', 'commit', 'succeeded_verified', 'Post 1 photo with caption to instagram.com as harbor. Readback confirmed it.');
    const words = ['open_lane', 'compose', 'publish', 'readback'].map((tool, index) => stepWords({ tool, summary: tool, status: 'succeeded_verified' }, story, index === 3));
    expect(words).toEqual(['Opened Instagram as Harbor Supply', 'Wrote the post', 'Shared it', 'Looked it up from outside and found it']);
  });
});

const story = (overrides: Partial<HistoryStory>): HistoryStory => ({
  id: 'run_1',
  source: 'run',
  at: '2026-11-04T12:07:33Z',
  started_at: '2026-11-04T12:07:27Z',
  lane: 'aster/linkedin',
  workspace: 'aster',
  workspace_name: 'Aster Coffee',
  place: "Aster Coffee's LinkedIn",
  site: { key: 'linkedin', label: 'LinkedIn' },
  action: 'read',
  sentence: "Could not check Aster Coffee's LinkedIn",
  verdict: 'failed',
  verdict_text: 'Nothing was read. The account was signed out.',
  reason: 'The account was signed out.',
  detail: 'the lane signal is logged_out.',
  status: 'failed',
  proof: null,
  who: { kind: 'flow', id: 'aster.linkedin.check', name: 'LinkedIn check', brand: null },
  why: { trigger: 'schedule', text: 'On its schedule', approval: null },
  steps: [],
  technical: { run_id: 'run_1', flow: 'aster.linkedin.check', effect: 'read', cost: { tokens: 0, usd: 0 } },
  ...overrides,
});

describe('a story from the cloud', () => {
  it('reads as a title with who did it, and knows when the account needs a person', () => {
    const view = fromHistory(story({}));
    expect(titleOf(view)).toBe("The LinkedIn check routine could not check Aster Coffee's LinkedIn");
    expect(view.label).toBe('Did not work');
    expect(view.accountProblem).toBe(true);
  });

  it('says a checked post is live, and a checked look is done', () => {
    expect(fromHistory(story({ verdict: 'checked', action: 'post', status: 'succeeded_verified' })).label).toBe('Live, checked');
    expect(fromHistory(story({ verdict: 'checked', action: 'read', status: 'succeeded_verified' })).verdict).toBe('Done, and we checked.');
  });

  it('makes one row of a check that kept failing the same way', () => {
    const day: HistoryDay = {
      date: '2026-11-04',
      label: 'Today',
      summary: '',
      done: 0,
      checked: 0,
      problems: 3,
      payments: 0,
      stories: [story({ id: 'a' }), story({ id: 'b' }), story({ id: 'c', action: 'post', sentence: "Could not post on Aster Coffee's LinkedIn" })],
      reads: null,
    };
    const entries = entriesOf(day);
    expect(entries.map((entry) => [entry.view.id, entry.times])).toEqual([
      ['a', 2],
      ['c', 1],
    ]);
  });

  it('says a quiet stretch once', () => {
    const label = (date: string) => (date === '2026-11-03' ? 'Yesterday' : date);
    expect(quietLine('2026-11-04', '2026-11-03', label)).toBeNull();
    expect(quietLine('2026-11-04', '2026-11-02', label)).toBe('Yesterday: nothing happened. The agents rested.');
    expect(quietLine('2026-11-04', '2026-10-31', label)).toBe('Nothing happened for 3 days. The agents rested.');
  });
});

describe('the kind of work', () => {
  it('comes from the cloud when it says, else from the proof, else from the action', () => {
    expect(kindOf(story({ kind: 'forms' }))).toBe('forms');
    expect(kindOf(story({ artifact: { type: 'files', files: [] } }))).toBe('money');
    expect(kindOf(story({ action: 'publish' }))).toBe('sent');
    // A routine look is no kind of work of its own: it folds into the day's one line.
    expect(kindOf(story({ action: 'read' }))).toBeNull();
  });

  it('says a checked email was sent, not that it is live', () => {
    const view = fromHistory(story({ verdict: 'checked', status: 'succeeded_verified', artifact: { type: 'email', from: 'a', to: ['b'], subject: 's', body: 'x' } }));
    expect(view.label).toBe('Sent, checked');
    expect(view.verdict).toBe('It was sent, and we checked.');
  });
});

describe('a proof sent the way approvals carry it', () => {
  it('reads a payment preview as the payment', () => {
    const artifact = artifactOf({ preview: { kind: 'payment', data: { amount: 40, currency: 'EUR', payee: 'Meta', for: 'Promote a post' } } });
    expect(artifact).toEqual({ type: 'payment', amount: '€40.00', payee: 'Meta', for: 'Promote a post', from_account: undefined, reference: undefined });
  });

  it('marks the fields a form preview says changed', () => {
    const artifact = artifactOf({ preview: { kind: 'form', data: { form: 'Listing', fields: [{ label: 'Price', value: '€118', before: '€104' }, { label: 'Stay', value: '3' }], changed: ['Price'] } } });
    expect(artifact?.type === 'form' && artifact.fields.map((field) => field.changed)).toEqual([true, false]);
  });
});
