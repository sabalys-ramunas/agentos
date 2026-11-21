/**
 * What a person reads about workers, in one place. Pure, so it is tested without a screen. No model
 * name, token or price reaches these strings: those are read straight from the data, and only with
 * Technical details on.
 */
import type { Brain, WorkerAccount, WorkerJob, WorkerOverview, WorkerPlace, WorkerStepKind, WorkerStop, WorkerThing } from '@/api/types';
import { ASKS, AWAY, RUNS } from '@/lib/ask-first';
import { aiName, computerName } from '@/lib/computers';
import { plural } from '@/lib/format';
import type { PersonState } from '@/lib/states';
import { WORKER_JOBS } from '@/lib/vocabulary';

export const WORKER_NAME = { browser: 'Browser worker', android: 'Android worker', desktop: 'Desktop worker' } as const;

/** An AI service, and how it reaches a computer. A key and a sign-in never pass through the panel. */
export interface ServiceInfo {
  id: string;
  name: string;
  /** `key` is pasted on the computer, `sign-in` happens on the computer, `local` needs neither. */
  how: 'key' | 'sign-in' | 'local';
  brainKind: Brain['kind'];
  provider: string;
  sentence: string;
  /** Where a person gets a key, when that is known. */
  keysAt: string | null;
}

export const SERVICES: ServiceInfo[] = [
  { id: 'typesafe', name: 'TypeSafe', how: 'key', brainKind: 'metered', provider: 'typesafe', sentence: 'Costs very little. It decides each step, and never writes text.', keysAt: null },
  { id: 'openrouter', name: 'OpenRouter', how: 'key', brainKind: 'metered', provider: 'openrouter', sentence: 'One account with several models. It decides each step, and small models write text.', keysAt: 'https://openrouter.ai/keys' },
  { id: 'claude-max', name: 'Your Claude plan', how: 'sign-in', brainKind: 'subscription', provider: 'anthropic', sentence: 'Thinks for your agents, and helps when a worker is unsure.', keysAt: null },
  { id: 'ollama-local', name: 'Local AI', how: 'local', brainKind: 'local', provider: 'ollama', sentence: 'Runs on the computer itself and writes text. Nothing leaves it.', keysAt: null },
];

export const serviceInfo = (id: string | undefined) => SERVICES.find((item) => item.id === id);

/** The real command, for Technical details and for a computer with no screen. */
export function addCommand(service: ServiceInfo): string {
  const line = `openajar brains add ${service.id} --kind ${service.brainKind} --provider ${service.provider}`;
  return service.how === 'key' ? `${line} --stdin` : line;
}

export function serviceName(brain: Pick<Brain, 'name' | 'kind' | 'provider'>): string {
  if (brain.provider === 'typesafe') return 'TypeSafe';
  if (brain.provider === 'openrouter') return 'OpenRouter';
  return aiName(brain.name, brain.kind);
}

const JOB_PHRASE: Record<WorkerJob, string> = { decide: 'decide each step', write: 'write text', rescue: 'help when it is unsure' };

/** What a service is for, in one sentence, and which of a worker's jobs it holds. */
export function serviceSentence(brain: Brain, overview: WorkerOverview | undefined): string {
  const can = new Set((brain.models ?? []).flatMap((model) => model.can));
  const jobs = (overview?.jobs ?? []).filter((item) => item.brain === brain.name).map((item) => JOB_PHRASE[item.job]);
  const used = jobs.length ? ` The browser worker uses it to ${inWords(jobs)}.` : '';
  if (can.has('decide') && !can.has('write')) return `Decides each step, and never writes text.${used}`;
  if (can.has('decide')) return `Decides each step and writes text.${used}`;
  if (can.has('agent')) return `Thinks for your agents.${used}`;
  if (can.has('write')) return `Writes text, and runs on the computer itself.${used}`;
  return `Ready for your agents.${used}`;
}

/** "a, b and c". */
export function inWords(items: string[]): string {
  if (items.length < 2) return items.join('');
  return `${items.slice(0, -1).join(', ')} and ${items.at(-1)}`;
}

/** Only what can do the job is offered, so a service that never writes is never a choice for writing. */
export const servicesFor = (job: WorkerJob, brains: Brain[]) => brains.filter((brain) => brain.models?.some((model) => model.can.includes(job)));

export const JOB_NOTE: Record<WorkerJob, string> = {
  decide: 'Picks the next click or key. This is nearly every step.',
  write: 'Only when a step needs words, such as a caption or a form field.',
  rescue: 'A stronger AI model looks at the page and says what to do.',
};

export const jobWord = (job: WorkerJob) => WORKER_JOBS[job].word;

/** "Mac mini (home) is a Mac, and Cloud server has 11 GB to spare." */
export function laterFacts(places: { node: string; fact: string }[]): string {
  return places.length ? `${inWords(places.map((place) => `${computerName(place.node)} ${place.fact}`))}.` : '';
}

/** The answer above the list of AI services: how many can think right now, and what is not fine. */
export function modelsAnswer(brains: Brain[]): string {
  const ready = brains.filter((brain) => brain.state === 'ready').length;
  const others = brains.filter((brain) => brain.state !== 'ready');
  if (!others.length) return ready === 1 ? 'Your AI service is ready.' : `All ${ready} AI services are ready.`;
  const [first] = others;
  const why = first?.state === 'cooling_down' ? 'is cooling down, and comes back by itself' : first?.state === 'needs_auth' ? 'needs you to sign in on its computer' : 'is down';
  return `${ready} of ${brains.length} are ready. ${first ? serviceName(first) : ''} ${why}.`;
}

/** Said beside the switch, so it never promises more than the switch does. */
export const NEVER = [
  'It cannot say yes to its own post. On an account that asks you first, what goes out waits for your yes.',
  'A robot check or a new device approval always goes to a person.',
  'Only a person turns it on. An agent can ask, and cannot press this.',
];

export const STOP_WORDS: Record<WorkerStop, string> = {
  unsure: 'It was still unsure, so it stopped and asked you. Nothing was changed.',
  robot_check: 'A robot check always goes to a person. It waits for you.',
  new_device: 'A new device approval always goes to a person. It waits for you.',
  limit: 'It reached today\'s limit and stopped here. Nothing went out.',
};

export const STEP_WHO: Record<WorkerStepKind, string> = {
  decide: 'The worker decided this',
  write: 'The worker wrote this',
  rescue: 'A stronger AI model decided this',
  you: 'It stopped here for you',
  yes: 'A person decides this',
};

const on = (data: WorkerOverview) => data.places.filter((place) => place.worker === 'on');

export interface WorkerAnswer {
  state: PersonState;
  /** The word beside the glyph, when it is not the state's own. */
  label?: string;
  lead: string;
  rest: string;
  /** What the panel's button does, when there is one thing to do next. */
  next: 'add-service' | 'turn-on' | 'needs-you' | null;
}

/** The sentence at the top of the page: it answers before anything lists. */
export function workerAnswer(data: WorkerOverview): WorkerAnswer {
  const running = on(data);
  if (!running.length) {
    const ready = data.places.some((place) => place.worker === 'off');
    return ready
      ? { state: 'sleeping', label: 'Off', lead: 'The browser worker is off.', rest: 'Nothing changes until you turn it on.', next: 'turn-on' }
      : { state: 'sleeping', label: 'Off', lead: 'The browser worker is off.', rest: 'It needs an AI service to decide each step.', next: 'add-service' };
  }
  const away = running.filter((place) => place.state === 'down');
  if (away.length === running.length) {
    return { state: 'stuck', label: 'Offline', lead: `${computerName(away[0]?.node ?? '')} is offline.`, rest: 'The worker stopped with it, and carries on when it is back.', next: null };
  }
  const { today, limits, latest } = data;
  if (!latest && !today.things) return { state: 'good', label: 'On', lead: 'It is on.', rest: 'It has nothing to do yet, and starts when an agent asks.', next: null };
  if (today.steps >= limits.steps_per_day) {
    return { state: 'sleeping', label: 'Stopped for today', lead: 'It used all of today\'s steps.', rest: 'It starts again at midnight.', next: null };
  }
  const did = `It did ${plural(today.things, 'thing')} today in ${today.steps.toLocaleString('en-GB')} steps.`;
  const helped = today.helped ? `${today.helped} needed help` : 'None needed help';
  const asked = today.asked ? `${today.asked} stopped for you` : 'none stopped for you';
  if (latest?.state === 'asked' && latest.stop) {
    const person = latest.stop === 'robot_check' || latest.stop === 'new_device';
    const before = `It did ${plural(today.things, 'thing')} today before this.`;
    return { state: 'waiting', lead: person ? 'A robot check stopped it.' : 'It stopped and asked you.', rest: person ? 'That always goes to a person.' : before, next: 'needs-you' };
  }
  return { state: latest?.state === 'working' ? 'working' : 'good', lead: did, rest: `${helped}, and ${asked}.`, next: null };
}

/** How a limit reads as a heading: the answer, then the numbers. */
export function limitsTitle(data: WorkerOverview): string {
  const used = Math.max(data.today.steps / data.limits.steps_per_day, data.today.helped / data.limits.help_per_day);
  if (used >= 1) return 'It reached a limit today';
  if (used >= 0.8) return 'Close to its limits today';
  return 'Inside its limits today';
}

/** What stands in a computer's way, or how it is doing. Never a switch that could not do anything. */
export function placeSentence(place: WorkerPlace): string {
  if (place.state === 'down') return place.worker === 'on' ? 'Offline, so nothing runs here now. It carries on when the computer is back.' : 'Offline, so this can change once it is back.';
  if (place.worker === 'needs_service') return 'Nothing here can decide each step yet. AI services stay on the computer that keeps them.';
  if (place.worker === 'off') return `Could work on ${plural(place.accounts, 'account')} here.`;
  if (place.restart_needed) return 'On. It starts the next time openajar starts on this computer.';
  if (!place.things_today) return `Ready for ${plural(place.accounts, 'account')}.`;
  return `${plural(place.things_today, 'thing')} done today, on ${plural(place.accounts, 'account')}.`;
}

/** Its posts follow each account's Ask me first, so this counts those and adds no word of its own. */
export function placeAsks(place: Pick<WorkerPlace, 'accounts' | 'accounts_asking' | 'after_a_page_asks'>): string {
  if (!place.accounts) return '';
  const every = place.accounts === 1 ? 'its one account' : `all ${place.accounts} accounts`;
  if (place.after_a_page_asks) return `Posts: ${ASKS} on ${every}, because it reads a page first.`;
  if (!place.accounts_asking) return `Posts: ${RUNS} on ${every}.`;
  if (place.accounts_asking === place.accounts) return `Posts: ${ASKS} on ${every}.`;
  return `Posts: ${ASKS} on ${place.accounts_asking} of ${plural(place.accounts, 'account')}.`;
}

/** What off means, said either way round, because that is what a person is choosing. */
export function accountWorkerSentence(worker: Pick<WorkerAccount, 'on' | 'online'>): string {
  if (!worker.online) return AWAY;
  return worker.on ? 'With it off, an agent can still work here directly, and you can take over.' : 'An agent can still work here directly, and you can take over.';
}

/** One line for an account or a list: where the worker stands on this thing. */
export function thingLine(thing: WorkerThing): string {
  const count = plural(thing.steps.length, 'step');
  switch (thing.state) {
    case 'working':
      return `The browser worker is on step ${thing.steps.length}.`;
    case 'asked':
      return thing.stop === 'robot_check' || thing.stop === 'new_device' ? `The browser worker stopped after ${count}, because this needs a person.` : thing.stop ? `The browser worker stopped after ${count} and asked you.` : `The browser worker took ${count}, and waits for a yes.`;
    case 'stopped':
      return `The browser worker stopped after ${count}.`;
    default:
      return `The browser worker did this in ${count}.`;
  }
}
