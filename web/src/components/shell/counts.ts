/**
 * The live numbers the shell shows: the rail's counts and the status line at its foot. Every one
 * comes from a hook, so the rail, the tab bar and the pages agree.
 *
 * `useRunningJobs` below is the one place the rail reads how many `openajar run` jobs are running. It
 * reads the same list as the Runs page (`useJobs`, the same key and limit), so the rail and the page
 * agree. While the list is not known, or this computer runs no commands, it answers `undefined` and
 * the rail shows no number (never a made-up one).
 */
import { isEnvOpen, useEnvironments, useJobs, useStatus } from '@/api/hooks';
import type { Status as OrgStatus } from '@/api/types';
import type { Tone } from '@/lib/labels';
import { useNeedsYouTotal } from '@/features/needs-you/count';

/** How many runs are going right now, or undefined while that is not known. */
export function useRunningJobs(org: string): number | undefined {
  const data = useJobs(org, { limit: 200 }).data;
  if (!data?.available) return undefined;
  return data.jobs.filter((job) => job.state === 'running').length;
}

/**
 * How many environments are open (not closed, not failed), from the same list as the Environments page
 * and Home. Undefined while that is not known or this computer does not serve environments.
 */
export function useOpenEnvironments(org: string): number | undefined {
  const data = useEnvironments(org).data;
  if (!data) return undefined;
  return data.environments.filter((env) => isEnvOpen(env.state)).length;
}

export interface Fleet {
  /** Machines the node knows about. */
  total: number;
  /** Answering: up or struggling. */
  online: number;
  /** Not answering: down. */
  down: number;
  /** Answering but slow or unwell. */
  struggling: number;
  /** One dot for every machine together: the worst of them. */
  tone: Tone;
}

/** What the status line says about the machines, from the status the node serves. */
export function fleetOf(status: OrgStatus | undefined): Fleet | undefined {
  if (!status) return undefined;
  const nodes = status.nodes;
  const down = nodes.filter((node) => node.state === 'down').length;
  const struggling = nodes.filter((node) => node.state !== 'down' && (node.state === 'degraded' || node.degraded)).length;
  const tone: Tone = nodes.length === 0 ? 'idle' : down ? 'bad' : struggling ? 'warn' : 'ok';
  return { total: nodes.length, online: nodes.length - down, down, struggling, tone };
}

/** The status line's words: "3 online · 1 not answering", "No machines yet". */
export function fleetWords(fleet: Fleet): string {
  if (fleet.total === 0) return 'No machines yet';
  const parts = [`${fleet.online} online`];
  if (fleet.struggling) parts.push(`${fleet.struggling} slow`);
  if (fleet.down) parts.push(`${fleet.down} not answering`);
  return parts.join(' · ');
}

export interface RailCounts {
  /** Runs going now; undefined until the node can say. */
  running: number | undefined;
  /** Environments open now; undefined until the node can say. */
  environments: number | undefined;
  /** Everything that waits for this person: the same number as Needs you. */
  needsYou: number;
  fleet: Fleet | undefined;
}

export function useRailCounts(org: string): RailCounts {
  const status = useStatus(org).data;
  return {
    running: useRunningJobs(org),
    environments: useOpenEnvironments(org),
    needsYou: useNeedsYouTotal(org, status),
    fleet: fleetOf(status),
  };
}
