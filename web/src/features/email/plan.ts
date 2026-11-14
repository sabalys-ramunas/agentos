/**
 * The send plan as data, kept pure so its promises can be tested: nobody gets two first emails, a
 * follow-up waits its full three days, and no day ever goes over the mailbox's limit.
 */
export interface PlanPerson {
  id: string;
  name: string;
}

export interface PlanInput {
  people: PlanPerson[];
  /** At most this many first emails a day. */
  pace: number;
  /** The mailbox's limit, shared with everything else it sends. */
  limit: number;
  /** What the mailbox sends on a normal day for other reasons. */
  other: number;
  start: number;
  follow: boolean;
  followAfterDays?: number;
  /** Follow-ups already due from people who were written to before today. */
  due?: { id: string; name: string; at: number }[];
}

export interface PlanDay {
  at: number;
  other: number;
  first: PlanPerson[];
  follow: PlanPerson[];
}

const DAY = 86_400_000;
const LAST_DAY = 40;

const isWeekend = (at: number) => [0, 6].includes(new Date(at).getDay());

function nextWorkday(at: number): number {
  let day = new Date(at).setHours(0, 0, 0, 0);
  while (isWeekend(day)) day += DAY;
  return day;
}

export function planDays({ people, pace, limit, other, start, follow, followAfterDays = 3, due = [] }: PlanInput): PlanDay[] {
  const waiting = [...people];
  const chasing = due.map((item) => ({ id: item.id, name: item.name, at: nextWorkday(item.at) }));
  const days: PlanDay[] = [];
  let at = nextWorkday(start);
  for (let index = 0; index < LAST_DAY && (waiting.length || chasing.length); index += 1) {
    const room = Math.max(0, limit - other);
    const ready = chasing.filter((item) => item.at <= at).slice(0, room);
    for (const item of ready) chasing.splice(chasing.indexOf(item), 1);
    const first = waiting.splice(0, Math.min(pace, room - ready.length));
    if (follow) for (const person of first) chasing.push({ ...person, at: nextWorkday(at + followAfterDays * DAY) });
    days.push({ at, other, first, follow: ready.map(({ id, name }) => ({ id, name })) });
    at = nextWorkday(at + DAY);
  }
  return days;
}
