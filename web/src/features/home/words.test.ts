import type { Lane, Workspace } from '@/api/types';
import { accountCondition, businessSentence, didSentence, heroSentence, nextStep, problemSentence, stuckSentences } from './words';

const lane = (over: Partial<Lane> = {}): Lane =>
  ({
    id: 'aster/instagram',
    workspace: 'aster',
    name: 'instagram',
    kind: 'browser',
    state: 'warm',
    signal: 'ok',
    control: { state: 'agent', by: null, since: null, takeback_requested: false, takeback_reason: null, takeback_by: null, last_human_input: null },
    account: null,
    hold: null,
    hold_until: null,
    site: { key: 'instagram', label: 'Instagram', domain: 'instagram.com' },
    health: { state: 'ok', blocks: false, reason: null, face: 'all_good' },
    ...over,
  }) as Lane;

const business = (lanes: Lane[], over: Partial<Workspace> = {}): Workspace => ({ id: 'aster', name: 'Aster Coffee', color: '#a0763f', health: 'ok', summary: '', lanes, ...over });

describe('Home words', () => {
  it('says what an agent did as a sentence, from the real summaries', () => {
    expect(didSentence({ summary: 'Check linkedin.com as @personal. Readback confirmed it.', status: 'succeeded_verified' })).toBe('Checked LinkedIn');
    expect(didSentence({ summary: 'Post 1 photo with caption to instagram.com as @astercoffee', status: 'succeeded_unverified' })).toBe('Posted a photo on Instagram');
    expect(didSentence({ summary: 'Publish 1 article to medium.com as @solsticestudio', status: 'partial' })).toBe('Partly done: published an article on Medium');
    expect(didSentence({ summary: 'Check blog.aster.example as @astercoffee', status: 'failed' })).toBe('Could not check the blog');
    expect(didSentence({ summary: 'Failed: Posted 1 photo to Instagram as aster', status: 'failed' })).toBe('Could not post a photo on Instagram');
    expect(didSentence({ summary: 'Replied to 3 comments on Reddit', status: 'succeeded_verified' })).toBe('Replied to 3 comments on Reddit');
    expect(didSentence({ summary: 'Publishing the weekly post on aster/instagram', status: 'running' })).toBe('Publishing the weekly post on Instagram');
  });

  it('takes the account state from the cloud, so a card and the counts agree', () => {
    expect(accountCondition(lane({ signal: 'rate_limited', state: 'cold', health: { state: 'rate_limited', blocks: false, reason: 'The site asked the account to slow down.', face: 'stuck' } }))).toEqual({
      state: 'stuck',
      sentence: 'The site asked the account to slow down',
    });
    expect(accountCondition(lane({ state: 'quarantined', health: { state: 'quarantined', blocks: true, reason: 'x', face: 'stuck' } })).sentence).toBe('Switched off for safety');
  });

  it('reads a frozen account as Frozen, even when the cloud face says all good', () => {
    expect(accountCondition(lane({ frozen: true }))).toEqual({ state: 'stuck', label: 'Frozen', sentence: 'Nothing runs on it until the freeze is lifted' });
    // A business's line says it in a sentence of its own, not the lane sentence.
    expect(problemSentence(lane({ frozen: true }))).toBe('Instagram is frozen');
  });

  it('names the one thing an account needs', () => {
    expect(nextStep(lane({ signal: 'logged_out', health: { state: 'logged_out', blocks: true, reason: null, face: 'waiting' } }))?.label).toBe('Sign in');
    expect(nextStep(lane())).toBeNull();
    expect(nextStep(lane({ state: 'active', health: { state: 'working', blocks: false, reason: null, face: 'working' } }))?.label).toBe('Watch');
  });

  it('gives each business one plain status sentence', () => {
    expect(businessSentence(business([lane(), lane({ id: 'aster/facebook', name: 'facebook', site: { key: 'facebook', label: 'Facebook', domain: null } })]))).toEqual({ state: 'good', text: 'All 2 accounts fine' });
    expect(businessSentence(business([lane({ state: 'quarantined', health: { state: 'quarantined', blocks: true, reason: null, face: 'stuck' } })], { state: 'stuck' }))).toEqual({
      state: 'stuck',
      text: 'Instagram is switched off for safety',
    });
    expect(businessSentence(business([lane({ state: 'cold', health: { state: 'asleep', blocks: false, reason: null, face: 'sleeping' } })]), { next: 'Instagram post at 19:00' }).text).toBe('Sleeping. Next: Instagram post at 19:00');
    expect(businessSentence(business([lane()]), { asks: 2 })).toEqual({ state: 'waiting', text: '2 things wait for your yes' });
  });

  it('names a frozen account, and a failing check, on the card and in the fallback sentence', () => {
    // The card's badge is Frozen for one frozen account, as its page says; the two-problem line names each state.
    expect(businessSentence(business([lane({ frozen: true })]))).toEqual({ state: 'stuck', text: 'Instagram is frozen', label: 'Frozen' });
    const shop = lane({ id: 'aster/shop', name: 'shop', site: { key: 'web', label: 'Shop', domain: null }, check: { level: 'fail', safe: false } });
    expect(businessSentence(business([shop, lane({ frozen: true })]))).toEqual({ state: 'stuck', text: 'Shop: not safe to use right now, and Instagram is frozen' });
    const ws = [business([shop, lane({ frozen: true })])];
    expect(stuckSentences(ws)).toEqual(["Aster Coffee's Shop: not safe to use right now.", "Aster Coffee's Instagram is frozen."]);
    // The fallback never says "everything else is fine" beside a stuck account.
    expect(heroSentence({ needs: 1, offline: [], accounts: 2, stuck: stuckSentences(ws) })).toMatchObject({
      lead: 'One thing needs you.',
      rest: "Aster Coffee's Shop: not safe to use right now.",
    });
    expect(heroSentence({ needs: 0, offline: [], accounts: 2, stuck: ["Aster Coffee's Instagram is frozen."] })).toMatchObject({
      lead: "Aster Coffee's Instagram is frozen.",
      rest: 'Nothing needs you.',
    });
  });

  it('answers the question first', () => {
    expect(heroSentence({ needs: 4, offline: [], accounts: 29 })).toMatchObject({ lead: '4 things need you.', rest: 'Everything else is fine.' });
    expect(heroSentence({ needs: 0, offline: [], accounts: 29 })).toMatchObject({ lead: 'All good.', rest: 'Nothing needs you.' });
    expect(heroSentence({ needs: 3, offline: ['Mac mini (home)'], accounts: 29 }).lead).toBe('Mac mini (home) is offline.');
    expect(heroSentence({ needs: 0, offline: [], accounts: 0 }).lead).toBe('Welcome.');
  });
});

describe("an account's own failing check", () => {
  it('makes the account stuck in the same words everywhere, with no cloud health to say otherwise', () => {
    expect(accountCondition(lane({ check: { level: 'fail', safe: false } }))).toEqual({ state: 'stuck', sentence: 'Not safe to use right now' });
  });
});
