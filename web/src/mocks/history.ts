/**
 * The mock's `GET /history`: the seeded log told as stories, in the shape the cloud sends
 * (api-for-rebuild.md, section 9). The words come from the panel's own story helpers, so the
 * mock and the real cloud say the same kind of sentence.
 */
import type { HistoryAction, HistoryDay, HistoryPage, HistoryReads, HistoryShow, HistoryStory, HistoryVerdict, Run, RunStatus, TraceEvent } from '@/api/types';
import { stepWords, storyOf } from '@/features/activity/story';
import { knownAgent, principalName, routineName } from '@/lib/principal';
import { siteOf } from '@/lib/sites';
import { exampleStories } from './history-examples';
import { storyWorker } from './workers';
import type { OrgData } from './seed';

const VERDICT: Record<RunStatus, HistoryVerdict> = {
  succeeded_verified: 'checked',
  succeeded_unverified: 'not_checked',
  partial: 'partial',
  failed: 'failed',
  cancelled: 'not_run',
  running: 'running',
  queued: 'running',
  waiting_approval: 'running',
};

function actionOf(row: TraceEvent, sentence: string): HistoryAction {
  if (row.effect === 'read') return 'read';
  if (row.effect === 'pay') return 'pay';
  if (row.effect === 'draft' || /draft/i.test(sentence)) return 'draft';
  if (/repl/i.test(sentence)) return 'reply';
  if (/publish/i.test(sentence)) return 'publish';
  if (row.effect === 'commit') return 'post';
  return 'change';
}

const BRANDS: Record<string, string> = { 'claude-code': 'claude', claude: 'claude', codex: 'codex', chatgpt: 'chatgpt' };

function storyFrom(row: TraceEvent, run: Run | undefined, names: Map<string, string>): HistoryStory {
  const told = storyOf(row, (id) => names.get(id));
  const principal = typeof row.principal === 'string' ? { kind: 'agent' as const, id: row.principal, thread: null } : row.principal;
  const agent = knownAgent(principal);
  const verdict = VERDICT[row.status];
  const failing = verdict === 'failed' || verdict === 'not_run' || verdict === 'partial';
  return {
    id: run?.id ?? row.id,
    source: run ? 'run' : 'call',
    at: run?.finished_at ?? row.at,
    started_at: run?.started_at ?? row.at,
    lane: row.lane,
    workspace: told.workspace || null,
    workspace_name: told.business || null,
    place: told.place,
    site: { key: told.siteKey, label: siteOf(row.lane)?.name ?? told.site },
    action: actionOf(row, told.sentence),
    sentence: told.sentence,
    verdict,
    verdict_text: told.note ?? told.verdict,
    reason: failing ? told.note : null,
    detail: told.detail,
    status: row.status,
    proof: run?.proof ? { url: run.proof.url ?? null, screenshot: run.proof.screenshot ?? null } : null,
    who: {
      kind: principal.kind,
      id: principal.id,
      name: principal.kind === 'flow' ? routineName(principal.id) : principalName(principal),
      brand: agent ? (BRANDS[agent] ?? null) : null,
    },
    why: {
      trigger: principal.kind === 'flow' ? 'schedule' : principal.thread ? 'agent' : null,
      text: principal.kind === 'flow' ? 'On its schedule' : principal.thread ? 'Asked from a chat' : null,
      approval: null,
      on_its_own: true,
    },
    steps: (run?.steps ?? []).map((step, index, all) => ({
      id: step.id,
      at: step.at,
      label: stepWords(step, told, index === all.length - 1),
      status: step.status,
    })),
    technical: { run_id: row.run_id, tool: row.tool, effect: row.effect, cost: row.cost },
  };
}

const dayOf = (iso: string) => {
  const at = new Date(iso);
  return `${at.getFullYear()}-${String(at.getMonth() + 1).padStart(2, '0')}-${String(at.getDate()).padStart(2, '0')}`;
};

function label(day: string, today: string, yesterday: string): string {
  if (day === today) return 'Today';
  if (day === yesterday) return 'Yesterday';
  const [year = 0, month = 1, date = 1] = day.split('-').map(Number);
  return new Date(year, month - 1, date).toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long' }).replace(',', '');
}

/** A look that went fine folds away; one that found something (a table) stays a story. */
const quiet = (story: HistoryStory) => story.action === 'read' && !story.artifact && (story.verdict === 'checked' || story.verdict === 'not_checked');
const POSTS = new Set<HistoryAction>(['post', 'publish', 'reply']);
const PROBLEMS = new Set<HistoryVerdict>(['failed', 'partial']);

export function historyPage(data: OrgData, options: { workspace?: string | null; show?: string | null; before?: string | null; days?: number }): HistoryPage {
  const names = new Map(data.workspaces.map((item) => [item.id, item.name]));
  const runs = new Map(data.runs.map((run) => [run.id, run]));
  const now = new Date();
  const today = dayOf(now.toISOString());
  const yesterdayDate = new Date(now);
  yesterdayDate.setDate(now.getDate() - 1);
  const yesterday = dayOf(yesterdayDate.toISOString());
  const span = Math.min(31, Math.max(1, options.days ?? 7));
  const last = options.before ? new Date(`${options.before}T00:00:00`) : new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
  const first = new Date(last);
  first.setDate(last.getDate() - span);
  const oldest = new Date(now);
  oldest.setDate(now.getDate() - data.retention.kept_days);

  const inScope = (row: TraceEvent) => !options.workspace || row.lane.startsWith(`${options.workspace}/`);
  const seen = new Set<string>();
  const all: HistoryStory[] = [];
  const working: HistoryStory[] = [];
  for (const row of data.activity) {
    if (!inScope(row) || (row.run_id && seen.has(row.run_id))) continue;
    if (row.run_id) seen.add(row.run_id);
    const at = new Date(row.at);
    const told = storyFrom(row, row.run_id ? runs.get(row.run_id) : undefined, names);
    const story = { ...told, worker: storyWorker(told, data) };
    if (story.verdict === 'running') {
      if (!options.before && row.status === 'running') working.push({ ...story, headline: `${story.who.kind === 'flow' ? `The ${story.who.name.toLowerCase()} routine` : story.who.name} is ${story.sentence.charAt(0).toLowerCase()}${story.sentence.slice(1)}` });
      continue;
    }
    if (at < first || at >= last) continue;
    all.push(story);
  }
  // The demo org does more than post; an empty org stays empty.
  for (const story of data.activity.length ? exampleStories(options.workspace) : []) {
    const at = new Date(story.at);
    if (at >= first && at < last) all.push(story);
  }
  all.sort((a, b) => b.at.localeCompare(a.at));
  const counts: Record<HistoryShow, number> = {
    everything: all.length,
    posts: all.filter((story) => POSTS.has(story.action)).length,
    problems: all.filter((story) => PROBLEMS.has(story.verdict)).length,
    spending: all.filter((story) => story.action === 'pay').length,
  };
  const shown = all.filter((story) =>
    options.show === 'posts' ? POSTS.has(story.action) : options.show === 'problems' ? PROBLEMS.has(story.verdict) : options.show === 'spending' ? story.action === 'pay' : true,
  );
  const byDay = new Map<string, HistoryStory[]>();
  for (const story of shown) {
    const key = dayOf(story.at);
    byDay.set(key, [...(byDay.get(key) ?? []), story]);
  }
  const days: HistoryDay[] = [...byDay.entries()].map(([date, items]) => {
    const reads = items.filter(quiet);
    const done = items.filter((story) => ['checked', 'not_checked', 'partial'].includes(story.verdict)).length;
    const checked = items.filter((story) => story.verdict === 'checked').length;
    const problems = items.filter((story) => PROBLEMS.has(story.verdict)).length;
    const lanes = new Map<string, HistoryReads['by_account'][number]>();
    for (const story of reads) {
      const entry = lanes.get(story.lane) ?? { lane: story.lane, place: story.place, site: story.site, times: 0, last_at: story.at };
      entry.times += 1;
      if (story.at > entry.last_at) entry.last_at = story.at;
      lanes.set(story.lane, entry);
    }
    const accounts = lanes.size;
    return {
      date,
      label: label(date, today, yesterday),
      summary: `${done} ${done === 1 ? 'thing' : 'things'} done · ${checked} checked${problems ? ` · ${problems} ${problems === 1 ? 'problem' : 'problems'}` : ''}`,
      done,
      checked,
      problems,
      payments: 0,
      stories: items.filter((story) => !quiet(story)),
      reads: accounts
        ? {
            times: reads.length,
            accounts,
            sentence: `Also checked ${accounts} ${accounts === 1 ? 'account' : 'accounts'} ${reads.length} ${reads.length === 1 ? 'time' : 'times'}.`,
            by_account: [...lanes.values()].sort((a, b) => b.times - a.times || a.lane.localeCompare(b.lane)),
          }
        : null,
    };
  });
  return {
    timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
    counts,
    working_now: working,
    days,
    next_before: first > oldest ? dayOf(first.toISOString()) : null,
    retention_days: data.retention.kept_days,
  };
}
