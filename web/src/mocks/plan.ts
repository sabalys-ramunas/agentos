/**
 * The day plan for Home, built from the same accounts and approvals the rest of the demo shows. It is a draft:
 * nothing here is queued or sent, and the mock never changes what it reports. Sections with nothing to say are
 * left out, as the node leaves them out.
 */
import { http, HttpResponse } from 'msw';
import type { DayPlan, PlanItem, PlanSection } from '@/api/types';
import type { OrgData } from './seed';
import type { Helpers } from './s3';

const BLOCKED_SIGNALS = new Set(['captcha', 'checkpoint', 'logged_out', 'blocked', 'rate_limited']);

function dayOf(now: Date): string {
  const year = now.getFullYear();
  const month = String(now.getMonth() + 1).padStart(2, '0');
  const day = String(now.getDate()).padStart(2, '0');
  return `${year}-${month}-${day}`;
}

/** What the day's draft says, from the workspace's accounts and the approvals waiting on them. */
function draftOf(data: OrgData, workspace: string | null, now = new Date()): DayPlan {
  const inWorkspace = (lane: string) => !workspace || lane.startsWith(`${workspace}/`);
  const items: PlanItem[] = [];
  const add = (section: PlanSection, lane: string | null, text: string, readiness?: string) => items.push({ section, lane, text, readiness: readiness ?? null });
  for (const item of data.lanes.filter((lane) => inWorkspace(lane.id))) {
    if (BLOCKED_SIGNALS.has(item.signal)) {
      add('blocked', item.id, 'Sign-in needed. Clear it in the live view, then hand it back.', 'needs_human');
    } else if (item.control.state === 'human') {
      add('blocked', item.id, 'Someone holds this account. Agents wait until it is handed back.', 'human');
    }
  }
  const approvals = data.approvals.filter((approval) => inWorkspace(approval.lane));
  for (const lane of new Set(approvals.map((approval) => approval.lane))) {
    const n = approvals.filter((approval) => approval.lane === lane).length;
    add('queue', lane, n === 1 ? '1 approval waiting.' : `${n} approvals waiting.`);
  }
  const questions = data.questions.filter((question) => question.state === 'open' && (!workspace || (question.lane ?? '').startsWith(`${workspace}/`)));
  if (questions.length) add('queue', null, questions.length === 1 ? '1 question waiting for an answer.' : `${questions.length} questions waiting for an answer.`);
  return {
    day: dayOf(now),
    status: 'draft',
    dry_run: true,
    items,
    content: [],
    unavailable: [],
  };
}

export function planHandlers({ org }: Pick<Helpers, 'org' | 'failure'>) {
  const scopeOf = (request: Request) => new URL(request.url).searchParams.get('workspace') || null;
  return [
    http.get(
      '/api/orgs/:org/plan/draft',
      org((data, { request }) => HttpResponse.json(draftOf(data, scopeOf(request)))),
    ),
    http.get(
      '/api/orgs/:org/plan/status',
      org((data, { request }) => {
        const workspace = scopeOf(request);
        const lanes = data.lanes.filter((lane) => !workspace || lane.workspace === workspace).map((lane) => ({
          lane: lane.id,
          workspaces: [lane.workspace],
          readiness: BLOCKED_SIGNALS.has(lane.signal) ? 'needs_human' : 'ready',
          detail: null,
          last_signal: lane.signal,
          signal_at: lane.last_used,
          signal_stale: false,
          programs: [],
          pending: { approvals: data.approvals.filter((approval) => approval.lane === lane.id).length, runs: 0, broken: [] },
          calls_24h: 0,
          errors_24h: 0,
        }));
        return HttpResponse.json({
          at: new Date().toISOString(),
          today: dayOf(new Date()),
          sessions_on: false,
          lanes,
          unattached_approvals: {},
          unavailable: [],
        });
      }),
    ),
  ];
}
