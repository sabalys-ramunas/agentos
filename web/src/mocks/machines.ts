/**
 * Machines at other sizes, for seeing the table at both ends:
 *   ?mock=one-machine    one node on its own and nothing rented, the view most people start with
 *   ?mock=machines-many  ten nodes and the two VMs, twelve rows: one not answering, one busy, the lanes spread over them
 * Screens stay unaware: this only changes what the mock backend answers.
 */
import { mockJobs } from './jobs';
import { scenario } from './scenario';
import type { MockDb, NodeRecord } from './seed';

const MANY: [id: string, os: string, arch: string, cpu: number, usedGb: number, totalGb: number, zone: string][] = [
  ['office-nuc', 'Ubuntu 24.04', 'x86_64', 31, 13, 32, 'Europe/Vilnius'],
  ['studio-mini', 'macOS 15.1', 'arm64', 18, 9, 16, 'Europe/Vilnius'],
  ['hel-2', 'Debian 12', 'x86_64', 88, 12, 16, 'Europe/Helsinki'],
  ['fsn-build', 'Debian 12', 'x86_64', 57, 40, 64, 'Europe/Berlin'],
  ['nbg-1', 'Debian 12', 'x86_64', 12, 3, 8, 'Europe/Berlin'],
  ['ash-1', 'Ubuntu 24.04', 'x86_64', 44, 7, 16, 'America/New_York'],
  ['shop-box', 'Ubuntu 22.04', 'x86_64', 0, 0, 16, 'Europe/Vilnius'],
  ['pi-garage', 'Debian 12', 'aarch64', 23, 2, 8, 'Europe/Vilnius'],
];

function extra(now: number): NodeRecord[] {
  return MANY.map(([id, os, arch, cpu, usedGb, totalGb, timezone]) => {
    const down = id === 'shop-box';
    const mem = { used_mb: usedGb * 1024, total_mb: totalGb * 1024 };
    return {
      id,
      state: down ? 'down' : 'up',
      capabilities: ['browser', 'terminal', 'service'],
      cpu,
      mem,
      warm_lanes: down ? 0 : 2,
      max_warm_lanes: 6,
      timezone,
      last_seen: new Date(now - (down ? 52 * 60_000 : 8_000)).toISOString(),
      version: '0.1.0',
      degraded: false,
      os,
      arch,
      load: { cpu_pct: cpu, mem_used_mb: mem.used_mb, mem_total_mb: mem.total_mb, disk_free_mb: 64_000 },
    };
  });
}

export function withMachines(db: MockDb): MockDb {
  const data = db.orgs.get('northwind-group');
  if (!data) return db;
  if (scenario.has('one-machine')) {
    const only = data.nodes.find((node) => node.id === 'home-mini');
    if (only) {
      data.nodes = [only];
      for (const lane of data.lanes) lane.node = only.id;
      for (const item of mockJobs(data)) item.job = { ...item.job, node: only.id };
    }
    data.vms = [];
    // One machine is a node on its own, which is how the app runs on a person's own machine.
    data.org.plan = 'self_hosted';
  }
  if (scenario.has('machines-many')) {
    const now = Date.now();
    const more = extra(now);
    data.nodes.push(...more);
    // Spread every third lane over the new machines, so their counts are real counts of real lanes.
    const up = more.filter((node) => node.state !== 'down');
    data.lanes.forEach((lane, index) => {
      if (index % 3 === 2) lane.node = up[index % up.length]!.id;
    });
  }
  return db;
}
