import { createDb } from './seed';

const data = createDb().orgs.get('northwind-group')!;

describe('the demo world', () => {
  it('has four companies with six surfaces each, and personal with five', () => {
    expect(data.workspaces.map((item) => item.id)).toEqual(['aster', 'harbor', 'meridian', 'solstice', 'personal']);
    expect(data.lanes).toHaveLength(29);
    for (const id of ['aster', 'harbor', 'meridian', 'solstice']) {
      expect(data.lanes.filter((lane) => lane.workspace === id).map((lane) => lane.name).sort()).toEqual([
        'blog',
        'facebook',
        'instagram',
        'linkedin',
        'medium',
        'reddit',
      ]);
    }
    expect(data.lanes.filter((lane) => lane.workspace === 'personal')).toHaveLength(5);
  });

  it('runs on the two named nodes', () => {
    expect(data.nodes.map((node) => node.id)).toEqual(['home-mini', 'hetzner-1']);
    expect(new Set(data.lanes.map((lane) => lane.node))).toEqual(new Set(['home-mini', 'hetzner-1']));
  });

  it('is a realistic mix: mostly ok, one captcha, one logged out, several cold', () => {
    const count = (signal: string) => data.lanes.filter((lane) => lane.signal === signal).length;
    expect(count('captcha')).toBe(1);
    expect(count('logged_out')).toBe(1);
    expect(count('ok')).toBeGreaterThan(15);
    expect(data.lanes.filter((lane) => lane.state === 'cold').length).toBeGreaterThan(5);
  });

  it('agrees with itself: warm counts match the lanes on each node', () => {
    for (const node of data.nodes) {
      const warm = data.lanes.filter((lane) => lane.node === node.id && ['warm', 'active'].includes(lane.state)).length;
      expect(node.warm_lanes).toBe(warm);
      expect(warm).toBeLessThanOrEqual(node.max_warm_lanes);
    }
  });

  it('only ever uses documentation IP ranges', () => {
    const doc = /^(192\.0\.2|198\.51\.100|203\.0\.113)\.\d{1,3}$/;
    const ips = [
      ...data.lanes.flatMap((lane) => [lane.exit_ip, lane.proxy?.host]),
      ...data.audit.map((item) => item.ip),
      ...createDb().devices.map((item) => item.ip),
    ].filter(Boolean);
    expect(ips.length).toBeGreaterThan(20);
    for (const ip of ips) expect(ip).toMatch(doc);
  });

  it('keeps the vault to names', () => {
    const allowed = ['created_at', 'domain', 'id', 'kind', 'lanes', 'last_used', 'name'];
    for (const entry of data.vault) expect(Object.keys(entry).sort()).toEqual(allowed);
  });

  it('uses prefixed ids from the contract', () => {
    for (const approval of data.approvals) expect(approval.id).toMatch(/^ap_/);
    for (const run of data.runs) expect(run.id).toMatch(/^run_/);
    for (const member of data.members) expect(member.id).toMatch(/^mem_/);
  });
});
