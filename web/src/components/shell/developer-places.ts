/**
 * "Hidden until first used": the two places For developers (Runs and checks, Site APIs) stay out of the
 * rail and the phone's More until they are first used, so a person who never starts a run never meets an
 * empty developer page (nav-proposal.png, 2026-11-21).
 *
 * The rule, per organisation:
 * - Runs and checks shows once the organisation has a run or a check; Site APIs once it has a Site API.
 * - Either shows once the person opens it on this device (from the command bar, a link or its address).
 * - Once shown it stays shown on this device, even if the runs are later gone: it is remembered.
 * - While the node has not answered yet, it stays hidden rather than appear and vanish.
 *
 * The command bar, the g keys and the addresses reach both places at all times.
 */
import { useRouterState } from '@tanstack/react-router';
import { useEffect, useMemo, useSyncExternalStore } from 'react';
import { isStudioMissing, useJobs, useSiteApis, useStudioRuns } from '@/api/hooks';
import { placeOf } from './nav';
import type { NavTo } from './nav';

const RUNS: NavTo = '/$org/runs';
const SITE_APIS: NavTo = '/$org/site-apis';

const storageKey = (org: string) => `openajar:used-places:${org}`;
const listeners = new Set<() => void>();
/** What this page knows, for when storage refuses (private mode): it then lasts for this page only. */
const memory = new Map<string, string>();

function read(org: string): string {
  try {
    return localStorage.getItem(storageKey(org)) ?? '';
  } catch {
    return memory.get(org) ?? '';
  }
}

function remember(org: string, to: NavTo) {
  const now = read(org);
  if (now.split(' ').includes(to)) return;
  const next = [now, to].filter(Boolean).join(' ');
  try {
    localStorage.setItem(storageKey(org), next);
  } catch {
    memory.set(org, next);
  }
  listeners.forEach((listener) => listener());
}

function useRemembered(org: string): string {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => read(org),
    () => '',
  );
}

/** The developer places to show in the rail and More for this organisation. */
export function useUsedPlaces(org: string, enabled = true): ReadonlySet<NavTo> {
  const remembered = useRemembered(org);
  const knownRuns = remembered.split(' ').includes(RUNS);
  const knownApis = remembered.split(' ').includes(SITE_APIS);
  // The same lists (and keys) as the Runs, Checks and Site APIs pages, so nothing is asked twice; none is
  // asked once the place is known.
  const jobs = useJobs(org, { limit: 200 }, enabled && !knownRuns).data;
  const checks = useStudioRuns(org, { limit: 200 }, enabled && !knownRuns).data;
  const apis = useSiteApis(org, enabled && !knownApis).data;
  const pathname = useRouterState({ select: (state) => state.location.pathname });
  const here = placeOf(pathname)?.to;

  const hasRuns = Boolean(jobs?.available && jobs.jobs.length) || Boolean(checks && !isStudioMissing(checks) && checks.runs.length);
  const hasApis = Boolean(apis?.length);
  useEffect(() => {
    if (!enabled) return;
    if (hasRuns || here === RUNS) remember(org, RUNS);
    if (hasApis || here === SITE_APIS) remember(org, SITE_APIS);
  }, [enabled, org, hasRuns, hasApis, here]);

  return useMemo(() => new Set(remembered.split(' ').filter(Boolean) as NavTo[]), [remembered]);
}
