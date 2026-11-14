import { effectLabel, effectOrder, health, laneState, nodeState, runStatus, signal } from './labels';

describe('honest labels', () => {
  it('never reduces a run to a bare "Done"', () => {
    expect(Object.values(runStatus).map((item) => item.label)).not.toContain('Done');
  });

  it('separates verified from unverified', () => {
    expect(runStatus.succeeded_verified.label).toBe('Verified');
    expect(runStatus.succeeded_unverified.label).toBe('Unverified');
    expect(runStatus.succeeded_unverified.tone).not.toBe('ok');
  });

  it('gives every status a short label', () => {
    for (const map of [runStatus, laneState, signal, health, nodeState]) {
      for (const item of Object.values(map)) expect(item.label.length).toBeLessThanOrEqual(18);
    }
  });

  it('uses the everyday words for accounts and computers', () => {
    expect(laneState.cold.label).toBe('Sleeping');
    expect(laneState.quarantined.label).toBe('Locked for safety');
    expect(signal.ok.label).toBe('Fine');
    expect(signal.logged_out.label).toBe('Signed out');
    expect(nodeState.up.label).toBe('Online');
    expect(nodeState.down.label).toBe('Offline');
  });

  it('orders every effect, heaviest first', () => {
    expect([...effectOrder].sort()).toEqual(Object.keys(effectLabel).sort());
    expect(effectOrder.indexOf('irreversible')).toBeLessThan(effectOrder.indexOf('commit'));
  });
});
