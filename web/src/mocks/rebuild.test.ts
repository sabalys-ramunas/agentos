import { fleet } from '@/api/client';
import { db, mockTiming } from './handlers';

const org = () => db.orgs.get('northwind-group')!;

describe('the mock answers the rebuild fields like the cloud', () => {
  it('gives every lane its health, so a quarantined lane is never "ok"', async () => {
    const lanes = (await fleet.workspaces('northwind-group')).flatMap((workspace) => workspace.lanes);
    expect(lanes.every((lane) => lane.health)).toBe(true);
    const wrong = lanes.find((lane) => lane.account?.status === 'wrong_account')!;
    expect(wrong.health).toMatchObject({ state: 'quarantined', blocks: true, reason: 'The account is switched off for safety.' });
    expect(lanes.find((lane) => lane.name === 'instagram')?.site).toMatchObject({ key: 'instagram', label: 'Instagram' });
  });

  it('places an approval and says when its account blocks a yes', async () => {
    const approvals = await fleet.approvals('northwind-group');
    expect(approvals[0]).toHaveProperty('place');
    const target = org().lanes.find((lane) => lane.id === approvals.find((item) => item.lane)!.lane)!;
    target.state = 'quarantined';
    const again = (await fleet.approvals('northwind-group')).find((item) => item.lane === target.id)!;
    expect(again.lane_state).toMatchObject({ blocks: true, warning: 'The account is switched off for safety. If you say yes now, nothing happens until it is fixed.' });
    expect(again.place).toMatch(/'s /);
  });

  it('fails a yes on a blocked account, lists it back, and clears it with Got it', async () => {
    const approval = (await fleet.approvals('northwind-group')).find((item) => item.effect === 'commit' && item.lane)!;
    org().lanes.find((lane) => lane.id === approval.lane)!.state = 'quarantined';
    mockTiming.undoMs = 20;
    const decided = await fleet.approve('northwind-group', approval.id, approval.content_hash);
    // Held for the undo window first, as the cloud does, and only then sent.
    expect(decided.outcome).toMatchObject({ state: 'pending' });
    expect(decided.undo_until).toBeTruthy();
    await new Promise((resolve) => setTimeout(resolve, 60));
    const sent = await fleet.approval('northwind-group', approval.id);
    expect(sent.undo_until).toBeNull();
    expect(sent.outcome).toMatchObject({ state: 'failed', reason: 'The account is switched off for safety.' });
    expect(sent.outcome?.title).toMatch(/^Couldn't post to /);
    expect((await fleet.failedApprovals('northwind-group')).map((item) => item.id)).toContain(approval.id);
    const cleared = await fleet.clearOutcome('northwind-group', approval.id);
    expect(cleared.outcome?.cleared_by).toBe(db.user.name);
    expect((await fleet.failedApprovals('northwind-group')).map((item) => item.id)).not.toContain(approval.id);
  });

  it('takes a decision back inside the undo window, and refuses once it has gone', async () => {
    mockTiming.undoMs = 3_000;
    const approval = (await fleet.approvals('northwind-group')).find((item) => item.effect === 'commit')!;
    await fleet.approve('northwind-group', approval.id, approval.content_hash);
    const back = await fleet.undo('northwind-group', approval.id);
    expect(back).toMatchObject({ state: 'pending', outcome: null, undo_until: null });
    expect(org().approvals.some((item) => item.id === approval.id)).toBe(true);
    mockTiming.undoMs = 10;
    await fleet.deny('northwind-group', approval.id, approval.content_hash);
    // The window closes on a timer, which a busy test run can delay: wait for it, not a fixed time.
    await vi.waitFor(async () => expect((await fleet.approval('northwind-group', approval.id)).undo_until).toBeNull(), { timeout: 2000 });
    await expect(fleet.undo('northwind-group', approval.id)).rejects.toMatchObject({ code: 'undo_closed' });
  });

  it('says when the digest lands, in a real time zone', async () => {
    const settings = await fleet.notifications('northwind-group');
    expect(settings.digest?.label).toMatch(/^08:00, .+ time$/);
    expect(settings.timezone).not.toBe('UTC');
  });
});
