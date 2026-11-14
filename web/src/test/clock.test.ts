import { afternoonZone } from '../../build/test-zone';

// vite.config.ts runs the tests in a zone where it is afternoon, so mock data dated "3 hours ago" is
// still today whenever the suite runs.
describe('the test clock', () => {
  it('runs the tests in the afternoon, local time', () => {
    expect(new Date().getHours()).toBeGreaterThanOrEqual(15);
  });

  it('picks a zone with the afternoon in it for every hour of the day', () => {
    for (let hour = 0; hour < 24; hour += 1) {
      const now = new Date(Date.UTC(2026, 10, 14, hour, 30));
      const local = Number(new Intl.DateTimeFormat('en-GB', { hour: 'numeric', hourCycle: 'h23', timeZone: afternoonZone(now) }).format(now));
      expect(local).toBe(15);
    }
  });
});
