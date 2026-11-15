import { ApiError } from '@/api/http';
import type { EnvSummary } from '@/api/types';
import { seedEnvironments } from '@/mocks/environments';
import { unsavedFrom, unsavedWords } from './close-flow';
import { agentWords, closedReason, commandStatus, euro, hourly, isJob, leftOf, machineWhere, openSummary, pullRequestLink, repoName, span, splitEnvironments } from './words';

const NOW = Date.parse('2026-11-13T12:00:00Z');
const summaries = (): EnvSummary[] => seedEnvironments(NOW).map((item) => item.detail);

describe('the words of Environments', () => {
  it('writes money so a few minutes never reads as nothing', () => {
    expect(euro(0.21)).toBe('€0.21');
    expect(euro(1.5)).toBe('€1.50');
    expect(euro(0)).toBe('€0.00');
    expect(euro(0.003)).toBe('€0.003');
    expect(euro(0.00036)).toBe('€0.0004');
    expect(euro(0.00001)).toBe('under €0.0001');
    expect(hourly(0.0121)).toBe('€0.0121 an hour');
    expect(hourly(0.25)).toBe('€0.25 an hour');
    // a Docker machine on the node costs nothing
    expect(hourly(0)).toBe('Free');
  });

  it('says where the machine is, and a Docker one as a container on the node', () => {
    expect(machineWhere({ provider: 'hetzner', server_type: 'cpx22', cpu: 2, memory_gb: 4, location: 'nbg1' })).toBe('cpx22 in nbg1, Hetzner');
    expect(machineWhere({ provider: 'docker', server_type: 'docker', cpu: 10, memory_gb: 7.7, location: 'local' })).toBe('A Docker container on the node');
  });

  it('writes a span a person reads', () => {
    expect(span(20_000)).toBe('under a minute');
    expect(span(14 * 60_000)).toBe('14 min');
    expect(span(106 * 60_000)).toBe('1 h 46 min');
    expect(span(4 * 3_600_000)).toBe('4 h');
    expect(span(3 * 86_400_000)).toBe('3 days');
  });

  it('names the repository, the agent and why one closed', () => {
    expect(repoName('github.com/harbor-supply/shop')).toBe('harbor-supply/shop');
    expect(repoName('https://gitlab.example/team/app.git')).toBe('gitlab.example/team/app');
    expect(agentWords({ name: 'Claude Code', client: 'T3 Code' })).toBe('Claude Code in T3 Code');
    expect(agentWords({ name: 'Claude Code', client: 'Claude Code' })).toBe('Claude Code');
    expect(agentWords({ name: 'Codex', client: null })).toBe('Codex');
    expect(closedReason('idle', 'closed')).toBe('It was closed after an hour with no call from the agent.');
    expect(closedReason('the owner asked for it', 'closed')).toBe('The owner asked for it.');
    expect(closedReason(null, 'failed')).toMatch(/did not start/);
  });

  it('puts open ones first, newest first, and closed ones after, newest closing first', () => {
    const { open, closed } = splitEnvironments(summaries());
    expect(open.map((env) => env.id)).toEqual(['q3mn', 'e7k2', 'w8tp']);
    expect(closed.map((env) => env.id)).toEqual(['p2lc', 'v6up', 'f9sr', 'o4em', 'd1dm', 'a8fx']);
    expect(openSummary(open)).toMatch(/^1 working, 1 starting, 1 idle · €/);
    expect(openSummary([])).toBe('None open right now.');
    expect(leftOf(closed[0]!, NOW)).toBeNull();
    expect(leftOf(open[1]!, NOW)).toBe(106 * 60_000);
  });

  it('links a command to its run only when it is a job, and gives each state a word', () => {
    const base = { cmd: 'pnpm dev', started_at: new Date(NOW).toISOString() };
    expect(isJob({ ...base, id: 'job-8c1f0e2a3b', state: 'running' })).toBe(true);
    expect(isJob({ ...base, id: 'exec-31f0a9', state: 'passed' })).toBe(false);
    expect(commandStatus({ ...base, id: 'x', state: 'failed', exit_code: 2 }).label).toBe('Exit 2');
    expect(commandStatus({ ...base, id: 'x', state: 'passed', exit_code: 0 }).label).toBe('Done');
    expect(commandStatus({ ...base, id: 'x', state: 'waiting_input' }).label).toBe('Waiting input');
  });

  it('reads the unsaved work from the refusal, wherever the node put it', () => {
    const fallback = { dirty_files: ['a.ts'], unpushed: [] };
    const beside = new ApiError(409, 'unsaved_work', 'Unsaved.', { dirty_files: ['x.ts', 'y.ts'], unpushed: ['1a2b3c4 One'] });
    expect(unsavedFrom(beside, fallback)).toEqual({ dirty_files: ['x.ts', 'y.ts'], unpushed: ['1a2b3c4 One'], stashes: [] });
    const nested = new ApiError(409, 'unsaved_work', 'Unsaved.', { unsaved: { dirty_files: [], unpushed: ['1a2b3c4 One'] } });
    expect(unsavedFrom(nested, fallback)).toEqual({ dirty_files: [], unpushed: ['1a2b3c4 One'], stashes: [] });
    const stashed = new ApiError(409, 'unsaved_work', 'Unsaved.', { unsaved: { dirty_files: [], unpushed: [], stashes: ['stash@{0}: WIP on main'] } });
    expect(unsavedWords(unsavedFrom(stashed, fallback))).toBe('1 stash');
    expect(unsavedFrom(new ApiError(409, 'unsaved_work', 'Unsaved.'), fallback)).toEqual(fallback);
    expect(unsavedWords({ dirty_files: ['x', 'y', 'z'], unpushed: ['a', 'b'] })).toBe('3 changed files and 2 commits not pushed');
  });
});

describe('the pull request link of a pushed branch', () => {
  it('opens GitHub\'s compare page for a branch, with each part of its name kept as it is', () => {
    expect(pullRequestLink('github.com/harbor-supply/shop', 'feat/black-friday-offer')).toBe('https://github.com/harbor-supply/shop/compare/feat/black-friday-offer?expand=1');
    expect(pullRequestLink('https://github.com/me/shop.git', 'fix/a b#1')).toBe('https://github.com/me/shop/compare/fix/a%20b%231?expand=1');
  });
  it('has none for the default branch, another host or no repository', () => {
    expect(pullRequestLink('github.com/me/shop', 'main')).toBeNull();
    expect(pullRequestLink('github.com/me/shop', 'trunk', 'trunk')).toBeNull();
    expect(pullRequestLink('gitlab.com/me/shop', 'feat/x')).toBeNull();
    expect(pullRequestLink(null, 'feat/x')).toBeNull();
  });
});
