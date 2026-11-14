import { connectors, engage, surfaces } from '@/api/client';
import { ApiError } from '@/api/http';

const ORG = 'northwind-group';

/** The engagement answers the screens depend on, from the mock itself (the same handlers the app runs on). */
describe('the engagement answers on the mock', () => {
  it('lists each connector with the surfaces it runs on and how healthy each one is', async () => {
    const ops = await connectors.list(ORG);
    const comment = ops.find((op) => op.operation === 'reddit.comment');
    expect(comment?.surfaces).toEqual([
      expect.objectContaining({ surface: 'harbor/reddit', health: 'held', health_detail: expect.objectContaining({ code: 'captcha' }) }),
    ]);
  });

  it('queues a run on a surface the connector runs on, and refuses one it does not', async () => {
    const ran = await connectors.run(ORG, 'reddit.post_text', { on: 'harbor/reddit' });
    expect(ran).toMatchObject({ flow: 'connector.reddit.post_text', status: 'queued', duplicate: false });
    await expect(connectors.run(ORG, 'reddit.post_text', { on: 'aster/instagram' })).rejects.toMatchObject({ status: 400 });
    await expect(connectors.run(ORG, 'no.such_operation', { on: 'harbor/reddit' })).rejects.toMatchObject({ status: 404 });
  });

  it('adds a surface, reads it back, and removes it once; a second removal is not found', async () => {
    const added = await surfaces.add(ORG, {
      id: 'aster/shop', site: 'instagram', lane: 'aster/shop', acts_as: { kind: 'ig_business', id: '17841400000009', name: '@aster.shop' },
    });
    expect(added).toMatchObject({ id: 'aster/shop', workspace: 'aster', name: 'shop', site: 'instagram' });
    expect(await surfaces.get(ORG, 'aster', 'shop')).toMatchObject({ acts_as: { name: '@aster.shop' } });
    expect(await surfaces.remove(ORG, 'aster', 'shop')).toEqual({ deleted: 'aster/shop' });
    await expect(surfaces.get(ORG, 'aster', 'shop')).rejects.toBeInstanceOf(ApiError);
    await expect(surfaces.remove(ORG, 'aster', 'shop')).rejects.toMatchObject({ status: 404 });
  });

  it('refuses a daily limit above the hard ceiling, and takes a limit back to the ramp', async () => {
    const refused = await engage.setCap(ORG, 'aster', 'instagram', 'like', 999).catch((error: unknown) => error);
    expect(refused).toMatchObject({ status: 400, code: 'cap_ceiling' });
    const set = await engage.setCap(ORG, 'aster', 'instagram', 'like', 12);
    expect(set).toEqual({ surface: 'aster/instagram', kind: 'like', cap: 12, hard_ceiling: 80 });
    expect(await engage.setCap(ORG, 'aster', 'instagram', 'like', null)).toMatchObject({ cap: null });
  });

  it('clears a hold once, and says zero the second time', async () => {
    expect(await engage.clearHolds(ORG, 'aster', 'instagram', { kind: null, id: null })).toEqual({ surface: 'aster/instagram', cleared: 1 });
    expect(await engage.clearHolds(ORG, 'aster', 'instagram', { kind: null, id: null })).toEqual({ surface: 'aster/instagram', cleared: 0 });
  });
});
