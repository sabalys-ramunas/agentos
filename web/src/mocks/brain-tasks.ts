/**
 * The mock of what agents handed to an AI model (docs/API.md, "Brains"): the owner's list, one task
 * with what it last said, and stopping or carrying on from one. Its states follow the node's, and a
 * member is refused as the node refuses them.
 */
import { http, HttpResponse } from 'msw';
import type { BrainTask, BrainTaskDetail } from '@/api/types';
import type { Helpers } from './s3';
import type { OrgData } from './seed';

const ACTIVE = new Set(['queued', 'starting', 'running', 'waiting_approval']);
const minutesAgo = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString();

/** A few tasks in each state, so the list shows what it looks like when it is busy and when it is quiet. */
function seedTasks(): BrainTask[] {
  const base = (over: Partial<BrainTask>): BrainTask => ({
    id: 'bt_x',
    workspace: 'aster',
    status: 'running',
    status_reason: null,
    brain: 'claude-max',
    attempt: 1,
    tokens: 0,
    turns: 0,
    cost_usd: null,
    summary: '',
    started_at: null,
    finished_at: null,
    lane: null,
    resumable: false,
    ...over,
  });
  return [
    base({ id: 'bt_run', status: 'running', summary: 'Write three captions for the spring menu', started_at: minutesAgo(3), tokens: 12_400, turns: 6, cost_usd: 0.42 }),
    base({ id: 'bt_wait', status: 'waiting_approval', workspace: 'harbor', summary: 'Draft the reply to the Halvard invoice', started_at: minutesAgo(20), tokens: 3_100, turns: 2, cost_usd: 0.11 }),
    base({ id: 'bt_queued', status: 'queued', workspace: 'solstice', summary: 'Tidy the mood board for the June shoot', brain: null }),
    base({ id: 'bt_stop', status: 'interrupted', workspace: 'meridian', summary: 'Compare three flight options for Oslo', started_at: minutesAgo(140), resumable: true, status_reason: 'The computer restarted.', tokens: 8_900, turns: 4, cost_usd: 0.3 }),
    base({ id: 'bt_fail', status: 'failed', workspace: 'harbor', summary: 'Pull the week’s open orders into a list', started_at: minutesAgo(600), resumable: true, status_reason: 'The model timed out.', attempt: 2, tokens: 21_000, turns: 9, cost_usd: 0.78 }),
    base({ id: 'bt_done', status: 'succeeded_unverified', workspace: 'aster', summary: 'Sort this week’s orders into groups', started_at: minutesAgo(1_440), finished_at: minutesAgo(1_410), tokens: 5_200, turns: 3, cost_usd: 0.19 }),
  ];
}

const TRANSCRIPT = [
  'Reading the spring menu draft.',
  'Caption 1 of 3 is written.',
  'Checking the caption length against the account’s limit.',
  'Caption 2 of 3 is written.',
];

export function brainTaskHandlers({ org, failure }: Pick<Helpers, 'org' | 'failure'>) {
  const tasksOf = (record: OrgData): BrainTask[] => {
    record.brainTasks ??= seedTasks();
    return record.brainTasks;
  };
  const notFound = () => failure(404, 'not_found', 'No such task.');
  const ownerOnly = () => failure(403, 'denied', 'Only the owner sees what was handed to a model.');

  return [
    http.get(
      '/api/orgs/:org/brains/tasks',
      org((record) => (record.role === 'owner' ? HttpResponse.json(tasksOf(record)) : ownerOnly())),
    ),
    http.get(
      '/api/orgs/:org/brains/tasks/:id',
      org((record, { params }) => {
        if (record.role !== 'owner') return ownerOnly();
        const task = tasksOf(record).find((item) => item.id === params.id);
        if (!task) return notFound();
        const detail: BrainTaskDetail = { ...task, transcript_tail: task.status === 'queued' ? [] : TRANSCRIPT, tail: task.status === 'queued' ? [] : TRANSCRIPT };
        return HttpResponse.json(detail);
      }),
    ),
    http.post(
      '/api/orgs/:org/brains/tasks/:id/cancel',
      org((record, { params }) => {
        if (record.role !== 'owner') return ownerOnly();
        const task = tasksOf(record).find((item) => item.id === params.id);
        if (!task) return notFound();
        if (!ACTIVE.has(task.status)) return failure(409, 'conflict', 'That task already ended.');
        task.status = 'cancelled';
        task.resumable = true;
        task.finished_at = new Date().toISOString();
        return HttpResponse.json(task);
      }),
    ),
    http.post(
      '/api/orgs/:org/brains/tasks/:id/resume',
      org((record, { params }) => {
        if (record.role !== 'owner') return ownerOnly();
        const task = tasksOf(record).find((item) => item.id === params.id);
        if (!task) return notFound();
        if (ACTIVE.has(task.status) || !task.resumable) return failure(409, 'conflict', 'That task cannot carry on.');
        task.status = 'running';
        task.resumable = false;
        task.attempt += 1;
        task.started_at = new Date().toISOString();
        task.finished_at = null;
        return HttpResponse.json(task);
      }),
    ),
  ];
}
