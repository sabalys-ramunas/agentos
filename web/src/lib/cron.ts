/** Cron lines in plain words. Anything this cannot read honestly stays as the cron line itself. */

import { instantOf, wallClock } from './zone';

const DAY = 86_400_000;
const DAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

const pad = (value: number) => String(value).padStart(2, '0');

/** "1-5" and "1,3,5" to a list. Null when the field is not a plain list or range. */
function list(field: string, max: number): number[] | null {
  const values: number[] = [];
  for (const part of field.split(',')) {
    const range = /^(\d+)-(\d+)$/.exec(part);
    if (range) {
      for (let value = Number(range[1]); value <= Number(range[2]); value += 1) values.push(value);
    } else if (/^\d+$/.test(part)) values.push(Number(part));
    else return null;
  }
  return values.every((value) => value <= max) ? values : null;
}

function dayWords(field: string): string | null {
  if (field === '*') return 'Every day';
  const days = list(field, 7)?.map((day) => day % 7);
  if (!days) return null;
  const sorted = [...new Set(days)].sort((a, b) => a - b);
  if (sorted.join() === '1,2,3,4,5') return 'Weekdays';
  if (sorted.join() === '0,6') return 'Weekends';
  return sorted.map((day) => DAYS[day]).join(', ');
}

/** "Every 30 minutes, 08:00 to 20:59" or "Mon, Wed, Fri at 09:00". */
export function describeCron(line: string): string {
  const parts = line.trim().split(/\s+/);
  if (parts.length !== 5) return line;
  const [minute = '', hour = '', dayOfMonth = '', month = '', weekday = ''] = parts;
  if (dayOfMonth !== '*' || month !== '*') return line;
  const days = dayWords(weekday);
  if (!days) return line;
  const onDays = days === 'Every day' ? '' : `, ${days.toLowerCase().replace(/\b(mon|tue|wed|thu|fri|sat|sun)\b/g, (day) => day.charAt(0).toUpperCase() + day.slice(1))}`;

  const everyMinutes = /^\*\/(\d+)$/.exec(minute);
  if (everyMinutes) {
    const step = `Every ${everyMinutes[1]} minutes`;
    if (hour === '*') return `${step}${onDays}`;
    const range = /^(\d+)-(\d+)$/.exec(hour);
    return range ? `${step}, ${pad(Number(range[1]))}:00 to ${pad(Number(range[2]))}:59${onDays}` : line;
  }
  if (!/^\d+$/.test(minute)) return line;
  const everyHours = /^\*\/(\d+)$/.exec(hour);
  if (hour === '*') return `Hourly at :${pad(Number(minute))}${onDays}`;
  if (everyHours) return `Every ${everyHours[1]} hours at :${pad(Number(minute))}${onDays}`;
  const hours = list(hour, 23);
  if (!hours) return line;
  return `${days} at ${hours.map((value) => `${pad(value)}:${pad(Number(minute))}`).join(', ')}`;
}

/** How many times a cron line fires in a day, on the days it runs. Null when it cannot be read. */
export function firesPerDay(line: string): number | null {
  const [minute = '', hour = ''] = line.trim().split(/\s+/);
  const minutes = /^\*\/(\d+)$/.exec(minute) ? Math.ceil(60 / Number(minute.slice(2))) : /^\d+$/.test(minute) ? 1 : null;
  if (minutes === null) return null;
  if (hour === '*') return minutes * 24;
  const every = /^\*\/(\d+)$/.exec(hour);
  if (every) return minutes * Math.ceil(24 / Number(every[1]));
  const hours = list(hour, 23);
  return hours ? minutes * hours.length : null;
}

function field(text: string, min: number, max: number): Set<number> | null {
  const values = new Set<number>();
  for (const part of text.split(',')) {
    const [range = '', stepText] = part.split('/');
    const step = stepText === undefined ? 1 : Number(stepText);
    if (!Number.isInteger(step) || step < 1) return null;
    let from = min;
    let to = max;
    if (range !== '*') {
      const match = /^(\d+)(?:-(\d+))?$/.exec(range);
      if (!match) return null;
      from = Number(match[1]);
      to = match[2] !== undefined ? Number(match[2]) : stepText !== undefined ? max : from;
    }
    if (from < min || to > max || from > to) return null;
    for (let value = from; value <= to; value += step) values.add(value);
  }
  return values;
}

export interface Cron {
  minutes: number[];
  hours: number[];
  months: Set<number>;
  /** Null when the field is `*`, because a restricted day and weekday match on either one. */
  days: Set<number> | null;
  weekdays: Set<number> | null;
}

/** Five numeric fields. Names and macros return null: the node's own next time still shows. */
export function parseCron(line: string): Cron | null {
  const parts = line.trim().split(/\s+/);
  if (parts.length !== 5) return null;
  const [minute = '', hour = '', day = '', month = '', weekday = ''] = parts;
  const minutes = field(minute, 0, 59);
  const hours = field(hour, 0, 23);
  const days = field(day, 1, 31);
  const months = field(month, 1, 12);
  const weekdays = field(weekday, 0, 7);
  if (!minutes || !hours || !days || !months || !weekdays) return null;
  if (weekdays.delete(7)) weekdays.add(0);
  const ascending = (a: number, b: number) => a - b;
  return {
    minutes: [...minutes].sort(ascending),
    hours: [...hours].sort(ascending),
    months,
    days: day === '*' ? null : days,
    weekdays: weekday === '*' ? null : weekdays,
  };
}

const matchesDay = (cron: Cron, day: number, weekday: number) => {
  if (cron.days && cron.weekdays) return cron.days.has(day) || cron.weekdays.has(weekday);
  if (cron.days) return cron.days.has(day);
  return cron.weekdays ? cron.weekdays.has(weekday) : true;
};

/**
 * Every fire in [from, to) on the zone's clock, oldest first, up to `cap`. Null when the line
 * cannot be read here, which only means no projection, not that the schedule is wrong.
 */
export function projectFires(line: string, from: number, to: number, zone: string, cap = 600): number[] | null {
  const cron = parseCron(line);
  if (!cron) return null;
  const first = wallClock(from, zone);
  const last = wallClock(to, zone);
  const stop = Date.UTC(last.year, last.month - 1, last.day);
  const fires: number[] = [];
  for (let cursor = Date.UTC(first.year, first.month - 1, first.day); cursor <= stop; cursor += DAY) {
    const date = new Date(cursor);
    const month = date.getUTCMonth() + 1;
    const day = date.getUTCDate();
    if (!cron.months.has(month) || !matchesDay(cron, day, date.getUTCDay())) continue;
    for (const hour of cron.hours) {
      for (const minute of cron.minutes) {
        const at = instantOf(zone, date.getUTCFullYear(), month, day, hour, minute);
        if (at < from || at >= to) continue;
        fires.push(at);
        if (fires.length >= cap) return fires;
      }
    }
  }
  return fires;
}
