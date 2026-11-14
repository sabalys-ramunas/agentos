import { homeSentence, waitedFor } from './sentence';
import type { Failure, SentenceInput, Trouble } from './sentence';

const NOW = Date.parse('2026-11-13T14:00:00Z');
const minutesAgo = (minutes: number) => new Date(NOW - minutes * 60_000).toISOString();

const base: SentenceInput = { running: 0, machines: [], knowsRuns: true, failures: [], troubles: [], waiting: 0, oldestAt: null, now: NOW };
const say = (over: Partial<SentenceInput>) => homeSentence({ ...base, ...over });
const words = (over: Partial<SentenceInput>) => {
  const { lead, line } = say(over);
  return [lead, ...line.map((part) => (part.kind === 'needs' ? `${part.text}${part.rest}.` : `${part.text}.`))].join(' ');
};

const failed = (over: Partial<Failure> = {}): Failure => ({ what: 'tests', again: false, againBy: null, ...over });
const blocker: Trouble = { text: "Harbor Supply's shop has a blocker", to: 'check', id: 'run_1' };
const down: Trouble = { text: 'hel-2 is not answering', to: 'machine', id: 'hel-2' };

describe("Home's sentence", () => {
  it('says all is quiet only when nothing runs, nothing failed, nothing waits and nothing is wrong', () => {
    expect(words({})).toBe('All quiet. Nothing is running, and nothing needs you.');
  });

  it('says nothing runs, and names what is wrong, when nothing runs but a machine is down', () => {
    expect(words({ troubles: [down] })).toBe('Nothing is running right now. hel-2 is not answering. Nothing needs you.');
  });

  it('says one job and its machine by name', () => {
    expect(words({ running: 1, machines: ['hetzner-1'] })).toBe('One job is running on hetzner-1. Nothing has failed. Nothing needs you.');
  });

  it('counts the machines when there are several, and leaves them out when no job says where it runs', () => {
    expect(say({ running: 5, machines: ['a', 'b', 'c', 'd'] }).lead).toBe('5 jobs are running on 4 machines. Nothing has failed.');
    expect(say({ running: 5, machines: [] }).lead).toBe('5 jobs are running. Nothing has failed.');
  });

  it('says a failure is running again, and who started that, only when the data says so', () => {
    expect(say({ running: 2, machines: ['hetzner-1'], failures: [failed({ again: true, againBy: 'Claude Code' })] }).lead).toBe(
      '2 jobs are running on hetzner-1. A test run failed, and Claude Code is running it again.',
    );
    expect(say({ running: 1, failures: [failed({ what: 'run', again: true })] }).lead).toBe('One job is running. A run failed, and it is running again.');
  });

  it('says a failure nobody ran again plainly, without claiming anyone is or is not on it', () => {
    const lead = say({ running: 0, failures: [failed()] }).lead;
    expect(lead).toBe('Nothing is running right now. A test run failed, and it has not run again since.');
    expect(lead).not.toMatch(/fixing|nobody|no one/i);
  });

  it('counts several failures, and how many run again', () => {
    expect(say({ running: 3, failures: [failed({ again: true }), failed({ again: true })] }).lead).toMatch(/2 test runs failed, and all of them are running again\.$/);
    expect(say({ running: 3, failures: [failed({ again: true }), failed({ what: 'run' })] }).lead).toMatch(/2 runs failed; 1 is running again\.$/);
    expect(say({ running: 0, failures: [failed(), failed()] }).lead).toMatch(/2 test runs failed, and none has run again since\.$/);
  });

  it('leads with what waits when that is all there is, with the oldest one\'s age', () => {
    expect(words({ waiting: 4, oldestAt: minutesAgo(130) })).toBe('4 things need you, the oldest for 2 h. Nothing is running right now.');
    expect(words({ waiting: 1, oldestAt: minutesAgo(7) })).toBe('One thing needs you, for 7 min. Nothing is running right now.');
  });

  it('puts what waits after what runs, as a link part', () => {
    const { line } = say({ running: 2, waiting: 6, oldestAt: minutesAgo(125), troubles: [blocker] });
    expect(line).toEqual([
      { kind: 'trouble', text: blocker.text, to: 'check', id: 'run_1' },
      { kind: 'needs', text: '6 things need you', rest: ', the oldest for 2 h' },
    ]);
  });

  it('names two problems and counts the rest', () => {
    const third: Trouble = { text: "Aster Coffee's Reddit is frozen", to: 'account', id: 'aster/reddit' };
    expect(words({ troubles: [blocker, down, third] })).toBe(`Nothing is running right now. ${blocker.text}. ${down.text}. 1 more problem. Nothing needs you.`);
  });

  it('says nothing about what waits while the count is still being read, and nothing about failures it could not read', () => {
    expect(say({ running: 1, waiting: null }).line).toEqual([]);
    expect(say({ running: 1, knowsRuns: false, failures: [failed()] }).lead).toBe('One job is running.');
    expect(say({ running: 0, knowsRuns: false, waiting: 0 }).lead).toBe('Nothing is running right now.');
  });

  it('says how long in plain short words', () => {
    expect(waitedFor(minutesAgo(0), NOW)).toBe('a moment');
    expect(waitedFor(minutesAgo(59), NOW)).toBe('59 min');
    expect(waitedFor(minutesAgo(60 * 26), NOW)).toBe('1 day');
    expect(waitedFor(minutesAgo(60 * 24 * 3), NOW)).toBe('3 days');
  });
});
