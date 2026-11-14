/** Times on a node's clock. A node supplies its zone, so an unknown one falls back to the viewer's. */

const DAY = 86_400_000;

export interface WallClock {
  year: number;
  month: number;
  day: number;
  hour: number;
  minute: number;
  second: number;
}

const formatters = new Map<string, Intl.DateTimeFormat>();

function formatter(zone: string): Intl.DateTimeFormat {
  let found = formatters.get(zone);
  if (!found) {
    found = new Intl.DateTimeFormat('en-US', {
      timeZone: zone,
      hourCycle: 'h23',
      year: 'numeric',
      month: 'numeric',
      day: 'numeric',
      hour: 'numeric',
      minute: 'numeric',
      second: 'numeric',
    });
    formatters.set(zone, found);
  }
  return found;
}

export const localZone = () => Intl.DateTimeFormat().resolvedOptions().timeZone;

export function safeZone(zone: string | null | undefined): string {
  if (!zone) return localZone();
  try {
    formatter(zone);
    return zone;
  } catch {
    return localZone();
  }
}

export function wallClock(instant: number, zone: string): WallClock {
  const parts: Record<string, number> = {};
  for (const part of formatter(zone).formatToParts(instant)) parts[part.type] = Number(part.value);
  return {
    year: parts.year ?? 0,
    month: parts.month ?? 1,
    day: parts.day ?? 1,
    hour: parts.hour ?? 0,
    minute: parts.minute ?? 0,
    second: parts.second ?? 0,
  };
}

function offsetAt(instant: number, zone: string): number {
  const wall = wallClock(instant, zone);
  return Date.UTC(wall.year, wall.month - 1, wall.day, wall.hour, wall.minute, wall.second) - Math.floor(instant / 1_000) * 1_000;
}

/** The instant a wall clock in a zone shows this time. The day may overflow, as with Date.UTC. */
export function instantOf(zone: string, year: number, month: number, day: number, hour = 0, minute = 0): number {
  const guess = Date.UTC(year, month - 1, day, hour, minute);
  // Twice, because the offset at the guess can be the wrong side of a clock change.
  return guess - offsetAt(guess - offsetAt(guess, zone), zone);
}

export function startOfDay(instant: number, zone: string): number {
  const wall = wallClock(instant, zone);
  return instantOf(zone, wall.year, wall.month, wall.day);
}

const pad = (value: number) => String(value).padStart(2, '0');

export function zoneClock(instant: number, zone: string): string {
  const wall = wallClock(instant, zone);
  return `${pad(wall.hour)}:${pad(wall.minute)}`;
}

/** "Thu 5 Nov" on the zone's calendar. */
export function zoneDay(instant: number, zone: string): string {
  const parts = new Intl.DateTimeFormat('en-GB', { timeZone: zone, weekday: 'short', day: 'numeric', month: 'short' }).formatToParts(instant);
  const part = (type: string) => parts.find((item) => item.type === type)?.value ?? '';
  return `${part('weekday')} ${part('day')} ${part('month')}`;
}

const dayNumber = (wall: WallClock) => Date.UTC(wall.year, wall.month - 1, wall.day) / DAY;

/** "Today 09:00", "Tomorrow 09:00" or "Thu 5 Nov 09:00", read on the zone's own calendar. */
export function fireLabel(instant: number, zone: string, now = Date.now()): string {
  const days = dayNumber(wallClock(instant, zone)) - dayNumber(wallClock(now, zone));
  const time = zoneClock(instant, zone);
  if (days === 0) return `Today ${time}`;
  if (days === 1) return `Tomorrow ${time}`;
  return `${zoneDay(instant, zone)} ${time}`;
}

/** Whether two zones show different times right now, so a second reading is worth printing. */
export const zonesDiffer = (a: string, b: string, at = Date.now()) => offsetAt(at, a) !== offsetAt(at, b);

/** "Vilnius" from "Europe/Vilnius". */
export const zoneCity = (zone: string) => (zone.split('/').pop() ?? zone).replaceAll('_', ' ');
