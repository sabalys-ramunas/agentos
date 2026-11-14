/**
 * The words of work handed to an AI model (web/DESIGN.md: words live in one place). "Job" means a
 * worker's step, so a task is only ever "handed" or "given" to a model, and a finished one is
 * "Finished, not checked" until something has checked it, never a bare "Done".
 */
import type { BrainTask, BrainTaskStatus } from '@/api/types';
import type { PersonState } from '@/lib/states';

export const TASK_WORDS: Record<BrainTaskStatus, { label: string; state: PersonState; sentence: string }> = {
  queued: { label: 'Queued', state: 'waiting', sentence: 'Waiting for its turn with an AI model.' },
  starting: { label: 'Starting', state: 'working', sentence: 'An AI model is picking it up now.' },
  running: { label: 'Working now', state: 'working', sentence: 'An AI model is working on it now.' },
  waiting_approval: { label: 'Waiting for you', state: 'waiting', sentence: 'It needs a yes from you before it goes on.' },
  succeeded_unverified: {
    label: 'Finished, not checked',
    state: 'good',
    sentence: 'The model finished. Nothing has checked the result yet.',
  },
  failed: { label: 'Did not finish', state: 'stuck', sentence: 'It stopped with an error before it finished.' },
  cancelled: { label: 'Stopped', state: 'sleeping', sentence: 'It was stopped before it finished.' },
  interrupted: {
    label: 'Interrupted',
    state: 'stuck',
    sentence: 'The computer restarted while it worked. It can carry on from where it stopped.',
  },
};

const ACTIVE: BrainTaskStatus[] = ['queued', 'starting', 'running', 'waiting_approval'];

/** A task that can still be stopped: it has not ended. */
export const canStop = (task: Pick<BrainTask, 'status'>) => ACTIVE.includes(task.status);

/** A task that ended and can carry on from where it stopped, as the node says it can. */
export const canResume = (task: Pick<BrainTask, 'status' | 'resumable'>) => task.resumable && !ACTIVE.includes(task.status);
