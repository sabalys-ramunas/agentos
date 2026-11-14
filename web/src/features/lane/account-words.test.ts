import type { ControlAction, LanePace, LaneQueue, TaintFamily } from '@/api/types';
import {
  checkStatus,
  holderName,
  isPlainLimit,
  laneTimeWords,
  limitStatus,
  nextPostWords,
  ownActionSentence,
  paceSentence,
  paceStatus,
  queueSentence,
  quietHoursWords,
  siteWords,
  taintSentence,
  validSite,
  windowText,
  wholeOrNull,
  windowWords,
} from './account-words';

const pace = (patch: Partial<LanePace> = {}): LanePace => ({
  lane: 'aster/instagram',
  pace: { commits_per_day: 3, min_gap_minutes: 45, active_hours: ['08:00-22:00'] },
  paced: true,
  zone: { timezone: 'Europe/Vilnius', source: 'node', source_words: "the computer's zone", exit_timezone: null, unreadable: false, note: null },
  local_now: '2026-11-07T10:14+02:00',
  workspace_timezone: null,
  preset: { commits_per_day: 3, min_gap_minutes: 45, active_hours: ['08:00-22:00'] },
  posts_today: 1,
  last_post_at: null,
  may_post_now: true,
  blocked_by: null,
  next_allowed_at: null,
  next_allowed_local: null,
  ...patch,
});

describe('posting pace', () => {
  it('says how the account may post, in one sentence', () => {
    expect(paceSentence({ commits_per_day: 3, min_gap_minutes: 45, active_hours: ['08:00-22:00'] })).toBe(
      'Posts go out between 08:00 and 22:00, at least 45 minutes apart, 3 posts a day.',
    );
    expect(paceSentence({ commits_per_day: 1 })).toBe('Posts go out 1 post a day.');
    expect(paceSentence(null)).toBeNull();
  });

  it('keeps a day list the node wrote, and reads several windows in order', () => {
    expect(windowWords('sat,sun 10:00-23:00')).toBe('sat,sun 10:00 and 23:00');
    expect(windowWords('mon-fri 09:00-18:00')).toBe('mon-fri 09:00 and 18:00');
  });

  it('says whether a post may go now, and why it may not', () => {
    expect(paceStatus(pace())).toEqual({ word: 'May post now', tone: 'ok' });
    expect(paceStatus(pace({ may_post_now: false, blocked_by: 'active_hours' }))).toEqual({ word: 'Sleeping for now', tone: 'idle' });
    expect(paceStatus(pace({ may_post_now: false, blocked_by: 'commits_per_day' }))).toEqual({ word: 'Done for today', tone: 'idle' });
    expect(paceStatus(pace({ paced: false, pace: null }))).toEqual({ word: 'Not paced', tone: 'neutral' });
  });

  it('gives the lane time on the clock the windows are read on', () => {
    expect(laneTimeWords(pace())).toBe('Lane time 10:14, on Europe/Vilnius.');
  });

  it('names the next post by day, and only when something blocks it', () => {
    expect(nextPostWords(pace())).toBeNull();
    const blocked = pace({ may_post_now: false, blocked_by: 'active_hours', next_allowed_local: '2026-11-07T08:00+02:00' });
    expect(nextPostWords(blocked)).toBe('Next post from 08:00 today.');
    const tomorrow = pace({ may_post_now: false, blocked_by: 'active_hours', next_allowed_local: '2026-11-08T08:00+02:00' });
    expect(nextPostWords(tomorrow)).toBe('Next post from 08:00 tomorrow.');
  });
});

describe('quiet hours', () => {
  it('is the stretch outside one plain waking window, on the same clock', () => {
    expect(quietHoursWords(['08:00-22:00'])).toBe('Quiet from 22:00 to 08:00.');
    expect(quietHoursWords(['08:00-22:00', 'sat,sun 10:00-23:00'])).toBeNull();
    expect(quietHoursWords(['sat,sun 10:00-23:00'])).toBeNull();
    expect(quietHoursWords(null)).toBeNull();
  });
});

describe('limits', () => {
  it('reads a meter in words, at its three states', () => {
    expect(limitStatus('ok')).toEqual({ word: 'Within its limit', tone: 'ok' });
    expect(limitStatus('warning')).toEqual({ word: 'Near its limit', tone: 'warn' });
    expect(limitStatus('cut_off')).toEqual({ word: 'Paused until it resets', tone: 'bad' });
  });

  it('keeps money and model use out of the plain limits', () => {
    expect(isPlainLimit('commits_per_day')).toBe(true);
    expect(isPlainLimit('commits_per_hour')).toBe(true);
    expect(isPlainLimit('usd_per_day')).toBe(false);
    expect(isPlainLimit('tokens_per_day')).toBe(false);
  });

  it('reads a field as a whole number, or nothing', () => {
    expect(wholeOrNull(' 6 ')).toBe(6);
    expect(wholeOrNull('')).toBeNull();
    expect(wholeOrNull('2.5')).toBeNull();
    expect(wholeOrNull('-1')).toBeNull();
  });
});

describe('who holds and who waits', () => {
  it('names an agent as people do, and a routine by its name, never by its run id', () => {
    expect(holderName('agent claude-code', 'agent')).toBe('Claude Code');
    expect(holderName('flow run run_9 (weekly-posts)', 'flow')).toBe('the weekly posts routine');
  });

  it('says who holds the account and who waits behind it', () => {
    const queue: LaneQueue = {
      lane: 'aster/instagram',
      claim: { holder: 'agent claude-code', kind: 'agent', ref: 'a', since: '', purpose: 'Post the morning photo', held_s: 40, implicit: false, yours: false },
      queue_length: 1,
      queue: [{ position: 1, mode: 'call', label: 'flow run run_9 (weekly-posts)', kind: 'flow', ref: 'w', waited_s: 12, estimate_s: 20, kept_out_by_claim: true }],
      inside: [],
      call_estimate_s: 4,
    };
    expect(queueSentence(queue)).toEqual({
      lead: 'Claude Code holds the account, for post the morning photo.',
      waiting: 'One thing waits: the weekly posts routine.',
    });
    expect(queueSentence({ ...queue, claim: { ...queue.claim!, yours: true }, queue: [] })).toEqual({
      lead: 'You hold the account, for post the morning photo.',
      waiting: null,
    });
    expect(queueSentence({ ...queue, claim: null, queue: [] }).lead).toBe('Nobody holds the account now.');
  });
});

describe('taint', () => {
  const family: TaintFamily = {
    context: 'family',
    labels: [],
    untrusted: true,
    since: null,
    follows: [{ kind: 'web_page', host: 'coffee-reviews.example', last: '' }],
    after_untrusted: 'a web page at coffee-reviews.example',
    waits_for_a_person: [],
    clean_context: '',
  };

  it('says an agent read a page, so its next post asks first', () => {
    expect(taintSentence(family, true)).toBe('An agent read a page at coffee-reviews.example, so its next post asks you first.');
    expect(taintSentence(family, false)).toBe('An agent read a page at coffee-reviews.example, so its next post asks a person first.');
    expect(taintSentence({ ...family, after_untrusted: null }, true)).toBe('An agent read text from outside, so its next post asks you first.');
  });
});

describe('the health line', () => {
  it('says safe, safe with a warning, or not safe, from the brief', () => {
    expect(checkStatus({ level: 'ok', safe: true })).toEqual({ word: 'Safe to use', tone: 'ok' });
    expect(checkStatus({ level: 'warn', safe: true })).toEqual({ word: 'Safe to use, with a warning', tone: 'warn' });
    expect(checkStatus({ level: 'fail', safe: false })).toEqual({ word: 'Not safe to use right now', tone: 'bad' });
    expect(checkStatus({ level: 'unknown', safe: null })).toEqual({ word: 'Not checked yet', tone: 'neutral' });
  });
});

describe('sites', () => {
  it('shows a frame as a site a page may show, not one the account visits', () => {
    expect(siteWords('instagram.com')).toEqual({ host: 'instagram.com', note: null });
    expect(siteWords('frame:www.google.com')).toEqual({ host: 'www.google.com', note: 'Only inside pages' });
  });

  it('takes a host, a host pattern, or a frame, and nothing else', () => {
    expect(validSite('instagram.com')).toBe(true);
    expect(validSite('*.instagram.com')).toBe(true);
    expect(validSite('frame:www.google.com')).toBe(true);
    expect(validSite('not a site')).toBe(false);
    expect(validSite('https://instagram.com/x')).toBe(false);
  });
});

describe('the windows a person types', () => {
  it('writes a window the node reads, and refuses an empty or backwards one', () => {
    expect(windowText('08:00', '22:00')).toBe('08:00-22:00');
    expect(windowText('08:00', '08:00')).toBeNull();
    expect(windowText('', '22:00')).toBeNull();
  });
});

describe("the owner's own changes to an account", () => {
  const owner = { kind: 'human', id: 'usr_owner', thread: null };
  it('names what the person did, in their words', () => {
    expect(ownActionSentence({ tool: 'panel.freeze', principal: owner, status: 'ok' })).toBe('You froze it');
    expect(ownActionSentence({ tool: 'panel.unfreeze', principal: owner, status: 'ok' })).toBe('You lifted the freeze');
    expect(ownActionSentence({ tool: 'panel.lane.pace', principal: owner, status: 'ok' })).toBe('You changed its pace');
    expect(ownActionSentence({ tool: 'panel.lane.limits', principal: owner, status: 'ok' })).toBe('You changed its limits');
    expect(ownActionSentence({ tool: 'panel.lane.control', principal: owner, status: 'ok' })).toBe('You changed who has control');
  });

  it('says a take over and a hand back apart, from the action the trace carries', () => {
    const control = (action: ControlAction | null) => ownActionSentence({ tool: 'panel.lane.control', principal: owner, status: 'ok', control: action });
    expect(control('take')).toBe('You took over');
    expect(control('hand_back')).toBe('You handed it back');
    expect(control('stop_agent')).toBe('You stopped the agent');
    expect(control('deny_request')).toBe('You kept control');
    // An older row, with no action on it, keeps the plain sentence.
    expect(control(null)).toBe('You changed who has control');
  });

  it('leaves out reads, refusals and what an agent did', () => {
    expect(ownActionSentence({ tool: 'panel.lane.check', principal: owner, status: 'ok' })).toBeNull();
    expect(ownActionSentence({ tool: 'panel.lane.pace', principal: owner, status: 'denied' })).toBeNull();
    expect(ownActionSentence({ tool: 'panel.lane.pace', principal: { kind: 'agent', id: 'codex', thread: null }, status: 'ok' })).toBeNull();
    expect(ownActionSentence({ tool: 'browser_act', principal: owner, status: 'ok' })).toBeNull();
  });
});
