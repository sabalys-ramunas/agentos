import type { Flow, Schedule } from '@/api/types';
import { weekOf } from './week-ahead';
import { NO_ROUTINE } from './schedule-timeline';

const ZONE = 'Europe/Vilnius';
// A Monday morning, so every day of the week has a 09:00 in it.
const NOW = Date.parse('2026-11-09T06:00:00+02:00');

const recorded = (overrides: Partial<Flow> = {}): Flow => ({ id: 'biz-a.social.post', lane: 'biz-a/social', version: 'sha256:1', params: {}, effect: 'commit', last_run: null, name: 'Post a photo', ...overrides });
const schedule = (overrides: Partial<Schedule>): Schedule => ({ id: 'sch_1', flow: '', params: {}, enabled: true, ...overrides });
const firesOf = (days: ReturnType<typeof weekOf>) => days.flatMap((day) => day.fires);

describe('The week ahead (L3)', () => {
  it('counts a recorded routine\'s schedule, which has no trigger of its own', () => {
    const days = weekOf([recorded()], [schedule({ flow: 'biz-a.social.post', cron: '0 9 * * *' })], NOW, ZONE);
    const fires = firesOf(days);
    expect(fires.length).toBeGreaterThanOrEqual(7);
    expect(fires.every((fire) => fire.title === 'Post a photo' && fire.flow === 'biz-a.social.post' && fire.site === 'social')).toBe(true);
  });

  it('counts a schedule with no routine too, named as one, and leaves out a paused one', () => {
    const days = weekOf([], [schedule({ id: 'sch_a', cron: '0 9 * * *' }), schedule({ id: 'sch_b', cron: '0 10 * * *', enabled: false })], NOW, ZONE);
    const fires = firesOf(days);
    expect(fires.length).toBeGreaterThanOrEqual(7);
    expect(fires.every((fire) => fire.title === NO_ROUTINE && fire.flow === null)).toBe(true);
  });

  it('counts a routine built in the app once, from its own schedule, and not again from its trigger', () => {
    const built = recorded({ id: 'biz-a.routine.orders', name: 'Check orders', trigger: { kind: 'schedule', cron: '0 9 * * *' } as Flow['trigger'] });
    const once = weekOf([built], [schedule({ flow: built.id, cron: '0 9 * * *', routine: true })], NOW, ZONE);
    const fromTrigger = weekOf([built], [], NOW, ZONE);
    expect(firesOf(once).length).toBe(firesOf(fromTrigger).length);
    expect(firesOf(once).every((fire) => fire.title === 'Check orders')).toBe(true);
  });
});
