import { describeCron, firesPerDay, parseCron, projectFires } from './cron';
import { fireLabel, instantOf, safeZone, startOfDay, wallClock, zoneCity, zoneClock, zoneDay, zonesDiffer } from './zone';

const VILNIUS = 'Europe/Vilnius';
const NEW_YORK = 'America/New_York';
const iso = (at: number) => new Date(at).toISOString();

describe('cron lines in plain words', () => {
  it.each([
    ['0 9 * * 1,3,5', 'Mon, Wed, Fri at 09:00'],
    ['0 18 * * *', 'Every day at 18:00'],
    ['30 8 * * 1', 'Mon at 08:30'],
    ['0 9 * * 1-5', 'Weekdays at 09:00'],
    ['*/30 8-20 * * *', 'Every 30 minutes, 08:00 to 20:59'],
    ['0 */2 * * *', 'Every 2 hours at :00'],
    ['15 * * * *', 'Hourly at :15'],
  ])('reads %s', (line, words) => {
    expect(describeCron(line)).toBe(words);
  });

  it('keeps the raw line rather than guessing', () => {
    expect(describeCron('0 9 1 * *')).toBe('0 9 1 * *');
    expect(describeCron('@daily')).toBe('@daily');
  });

  it('counts fires in a day', () => {
    expect(firesPerDay('*/30 8-20 * * *')).toBe(26);
    expect(firesPerDay('0 */2 * * *')).toBe(12);
    expect(firesPerDay('0 18 * * *')).toBe(1);
    expect(firesPerDay('@daily')).toBe(null);
  });
});

describe('parseCron', () => {
  it('reads lists, ranges and steps', () => {
    const cron = parseCron('0,30 9-11 * * 1-5')!;
    expect(cron.minutes).toEqual([0, 30]);
    expect(cron.hours).toEqual([9, 10, 11]);
    expect([...cron.weekdays!].sort()).toEqual([1, 2, 3, 4, 5]);
    expect(cron.days).toBeNull();
    expect(parseCron('*/15 * * * *')?.minutes).toEqual([0, 15, 30, 45]);
  });

  it('treats 7 as Sunday', () => expect([...parseCron('0 9 * * 7')!.weekdays!]).toEqual([0]));

  it.each(['@daily', '0 9 * *', '0 25 * * *', '60 9 * * *', '0 9 * * MON', '*/0 * * * *', '5-1 * * * *'])('declines %s', (line) =>
    expect(parseCron(line)).toBeNull(),
  );
});

describe('projectFires', () => {
  const from = Date.parse('2026-11-04T00:00:00Z');
  const week = from + 7 * 86_400_000;

  it('fires on the node clock, not the viewer clock', () => {
    const fires = projectFires('0 9 * * *', from, from + 86_400_000, VILNIUS)!;
    // 09:00 in Vilnius in November is 07:00 UTC.
    expect(fires.map(iso)).toEqual(['2026-11-04T07:00:00.000Z']);
    expect(projectFires('0 9 * * *', from, from + 86_400_000, NEW_YORK)?.map(iso)).toEqual(['2026-11-04T14:00:00.000Z']);
  });

  it('picks the right weekdays on the zone calendar', () => {
    // 4 Nov 2026 is a Wednesday.
    const fires = projectFires('0 9 * * 1,3,5', from, week, VILNIUS)!;
    expect(fires.map((at) => zoneDay(at, VILNIUS))).toEqual(['Wed 4 Nov', 'Fri 6 Nov', 'Mon 9 Nov']);
  });

  it('keeps the wall clock across a clock change', () => {
    // Vilnius left summer time on 25 Oct 2026, so one day at 09:00 is 06:00 UTC and the next 07:00.
    const start = Date.parse('2026-10-24T00:00:00Z');
    const fires = projectFires('0 9 * * *', start, start + 2 * 86_400_000, VILNIUS)!;
    expect(fires.map(iso)).toEqual(['2026-10-24T06:00:00.000Z', '2026-10-25T07:00:00.000Z']);
    expect(fires.every((at) => zoneClock(at, VILNIUS) === '09:00')).toBe(true);
  });

  it('matches a restricted day or weekday, either one, as cron does', () => {
    const fires = projectFires('0 9 5 * 1', from, week, VILNIUS)!;
    expect(fires.map((at) => zoneDay(at, VILNIUS))).toEqual(['Thu 5 Nov', 'Mon 9 Nov']);
  });

  it('stops at the cap', () => expect(projectFires('* * * * *', from, week, VILNIUS, 50)).toHaveLength(50));

  it('answers null when it cannot read the line', () => expect(projectFires('@daily', from, week, VILNIUS)).toBeNull());
});

describe('zone', () => {
  const at = Date.parse('2026-11-04T22:30:00Z');

  it('reads a time on the zone clock', () => {
    expect(wallClock(at, VILNIUS)).toMatchObject({ day: 5, hour: 0, minute: 30 });
    expect(zoneClock(at, NEW_YORK)).toBe('17:30');
  });

  it('finds the instant for a wall clock, with day overflow', () => {
    expect(iso(instantOf(VILNIUS, 2026, 11, 5, 9))).toBe('2026-11-05T07:00:00.000Z');
    expect(iso(instantOf(VILNIUS, 2026, 10, 32, 9))).toBe('2026-11-01T07:00:00.000Z');
  });

  it('starts a day at the zone midnight', () => expect(iso(startOfDay(at, VILNIUS))).toBe('2026-11-04T22:00:00.000Z'));

  it('says today and tomorrow on the zone calendar, not the viewer one', () => {
    const now = Date.parse('2026-11-04T21:00:00Z');
    expect(fireLabel(at, VILNIUS, now)).toBe('Tomorrow 00:30');
    expect(fireLabel(at, NEW_YORK, now)).toBe('Today 17:30');
    expect(fireLabel(at + 3 * 86_400_000, VILNIUS, now)).toBe('Sun 8 Nov 00:30');
  });

  it('falls back from a zone a node made up', () => {
    expect(safeZone('Mars/Olympus')).toBe(Intl.DateTimeFormat().resolvedOptions().timeZone);
    expect(safeZone(VILNIUS)).toBe(VILNIUS);
  });

  it('compares zones and names a city', () => {
    expect(zonesDiffer(VILNIUS, NEW_YORK, at)).toBe(true);
    expect(zonesDiffer(VILNIUS, 'Europe/Kyiv', at)).toBe(false);
    expect(zoneCity('America/New_York')).toBe('New York');
  });
});
