import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import {
  isStudioMissing,
  isUnavailable,
  JOB_POLL_MS,
  useBrandComponents,
  useBrandProfile,
  useBrandProfiles,
  useBrandProposals,
  useCancelJob,
  useDecideBrand,
  useJob,
  useJobFollow,
  useJobs,
  useNodes,
  useStudioBaseline,
  useStudioDiff,
  useStudioRun,
  useStudioRuns,
  useVms,
} from './hooks';
import { studioCropUrl, studioShotUrl } from './client';
import { db } from '@/mocks/handlers';
import { CAPTURE_AFTER, CAPTURE_BEFORE, RUN_BLOCKED, SHOP_URL } from '@/mocks/studio';

const ORG = 'northwind-group';
const org = () => db.orgs.get(ORG)!;

function wrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

describe('Runs', () => {
  it('lists runs in every state, newest first, with what is known and nothing more', async () => {
    const { result } = renderHook(() => useJobs(ORG), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.data).toBeDefined());
    const jobs = result.current.data!.jobs;
    expect(result.current.data!.available).toBe(true);
    expect(new Set(jobs.map((job) => job.state))).toEqual(new Set(['running', 'passed', 'failed', 'cancelled', 'timed_out', 'lost']));
    expect([...jobs].sort((a, b) => b.started_at.localeCompare(a.started_at)).map((job) => job.id)).toEqual(jobs.map((job) => job.id));
    const failed = jobs.find((job) => job.state === 'failed')!;
    expect(failed.tests).toMatchObject({ failed: 2, source: 'output' });
    expect(failed.source?.told_by).toBe('client');
    const older = jobs.find((job) => job.id === 'job-7a8b9c0d1e')!;
    expect(older.source).toBeUndefined();
    expect(older.tests).toBeUndefined();
  });

  it('filters by state', async () => {
    const { result } = renderHook(() => useJobs(ORG, { state: 'lost' }), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.data).toBeDefined());
    expect(result.current.data!.jobs.map((job) => job.state)).toEqual(['lost']);
  });

  it('follows a running run until it ends, with all of its output', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      const { result } = renderHook(() => useJobFollow(ORG, 'job-1a2b3c4d5e'), { wrapper: wrapper() });
      await waitFor(() => expect(result.current.job?.state).toBe('running'));
      for (let step = 0; step < 8 && !result.current.done; step += 1) {
        await act(async () => {
          await vi.advanceTimersByTimeAsync(JOB_POLL_MS);
        });
      }
      await waitFor(() => expect(result.current.done).toBe(true));
      expect(result.current.job?.state).toBe('passed');
      // The runner's colours stay in the output; the page strips them where it reads it.
      // eslint-disable-next-line no-control-regex
      expect(result.current.stdout.replace(/\u001b\[[0-9;]*m/g, '')).toContain('Tests  118 passed (118)');
      expect(result.current.job?.tests).toMatchObject({ runner: 'vitest', passed: 118 });
      expect(result.current.trimmed).toBe(false);
    } finally {
      vi.useRealTimers();
    }
  });

  it('reads one run with its output', async () => {
    const { result } = renderHook(() => useJob(ORG, 'job-3c4d5e6f7a'), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.data).toBeDefined());
    expect(result.current.data!.output.stdout).toContain('2 failed');
    expect(result.current.data!.output.stderr).toContain('AssertionError');
    expect(result.current.data!.output.more).toBe(false);
  });

  it('cancels a running run', async () => {
    const { result } = renderHook(() => useCancelJob(ORG), { wrapper: wrapper() });
    const job = await act(() => result.current.mutateAsync('job-1a2b3c4d5e'));
    expect(job.state).toBe('cancelled');
    expect(org().jobs!.find((item) => item.job.id === 'job-1a2b3c4d5e')!.job.state).toBe('cancelled');
  });

  it('an unknown run is not found', async () => {
    const { result } = renderHook(() => useJob(ORG, 'job-0000000000'), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.isError).toBe(true));
  });
});

describe('Checks', () => {
  it('lists checks with blockers and a clean one', async () => {
    const { result } = renderHook(() => useStudioRuns(ORG), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.data).toBeDefined());
    const data = result.current.data!;
    expect(isStudioMissing(data)).toBe(false);
    if (isStudioMissing(data)) return;
    const blocked = data.runs.find((run) => run.id === RUN_BLOCKED)!;
    expect(blocked.counts.blocker).toBe(2);
    expect(blocked.screens_failed).toBe(1);
    expect(data.runs.some((run) => run.counts.total === 0)).toBe(true);
  });

  it('reads one check with its findings by component, its screens and its images', async () => {
    const { result } = renderHook(() => useStudioRun(ORG, RUN_BLOCKED), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.data).toBeDefined());
    const run = result.current.data!;
    expect(run.groups[0]!.findings[0]!.severity).toBe('blocker');
    expect(run.screen_list.filter((s) => !s.ok)).toHaveLength(1);
    const finding = run.groups[0]!.findings[0]!;
    const crop = await fetch(studioCropUrl(ORG, run.id, finding.id, finding.crop_screens[0]));
    expect(crop.headers.get('content-type')).toContain('image/svg+xml');
    const shot = await fetch(studioShotUrl(ORG, run.capture, 'default@360-light', 400));
    expect(shot.ok).toBe(true);
  });

  it('has a baseline for the shop and a diff of two captures', async () => {
    const baseline = renderHook(() => useStudioBaseline(ORG, SHOP_URL), { wrapper: wrapper() });
    await waitFor(() => expect(baseline.result.current.data).toBeDefined());
    expect(baseline.result.current.data!.capture).toBe(CAPTURE_BEFORE);
    const diff = renderHook(() => useStudioDiff(ORG, CAPTURE_BEFORE, CAPTURE_AFTER), { wrapper: wrapper() });
    await waitFor(() => expect(diff.result.current.data).toBeDefined());
    expect(diff.result.current.data!.screens.find((s) => s.status === 'changed')!.lines.length).toBeGreaterThan(0);
  });

  it('gives component use and look-alikes for the shop profile', async () => {
    const { result } = renderHook(() => useBrandComponents(ORG, 'harbor/shop'), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.data).toBeDefined());
    const data = result.current.data!;
    if (isStudioMissing(data)) throw new Error('studio is there');
    expect(data.components[0]!.uses).toBeGreaterThan(0);
    expect(data.look_alikes?.look_alike).toBe(9);
    expect(data.look_alike_findings.length).toBeGreaterThan(0);
  });

  it('says studio is missing with the command that adds it', async () => {
    org().studioMissing = true;
    const list = renderHook(() => useStudioRuns(ORG), { wrapper: wrapper() });
    await waitFor(() => expect(list.result.current.data).toBeDefined());
    expect(isStudioMissing(list.result.current.data)).toBe(true);
    if (isStudioMissing(list.result.current.data)) expect(list.result.current.data.command).toBe('uv sync --extra studio');
    const one = renderHook(() => useStudioRun(ORG, RUN_BLOCKED), { wrapper: wrapper() });
    await waitFor(() => expect(one.result.current.isError).toBe(true));
    expect(isUnavailable(one.result.current.error)).toBe(true);
  });
});

describe('Brand', () => {
  it('lists product and business profiles, one with a waiting proposal', async () => {
    const { result } = renderHook(() => useBrandProfiles(ORG), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.data).toBeDefined());
    const rows = result.current.data!.profiles;
    expect(rows.map((row) => row.kind).sort()).toEqual(['business', 'product']);
    expect(rows.find((row) => row.ref === 'harbor/shop')!.draft_waiting).toBe(4);
    expect(rows.find((row) => row.ref === 'aster/cafe')!.draft_waiting).toBeNull();
  });

  it('reads the shop with its draft, and Needs you lists the proposal', async () => {
    const profile = renderHook(() => useBrandProfile(ORG, 'harbor/shop'), { wrapper: wrapper() });
    await waitFor(() => expect(profile.result.current.data).toBeDefined());
    const shop = profile.result.current.data!;
    expect(shop.brand!.tokens.length).toBeGreaterThan(5);
    expect(shop.draft!.reason).toContain('contrast');
    const proposals = renderHook(() => useBrandProposals(ORG), { wrapper: wrapper() });
    await waitFor(() => expect(proposals.result.current.data).toBeDefined());
    expect(proposals.result.current.data!.map((item) => item.id)).toEqual(['brand:harbor/shop@4']);
  });

  it('approves the version read, and the proposal leaves Needs you', async () => {
    const decide = renderHook(() => useDecideBrand(ORG), { wrapper: wrapper() });
    const out = await act(() => decide.result.current.mutateAsync({ ref: 'harbor/shop', version: 4, approve: true }));
    expect(out).toEqual({ ref: 'harbor/shop', version: 4, state: 'approved' });
    const proposals = renderHook(() => useBrandProposals(ORG), { wrapper: wrapper() });
    await waitFor(() => expect(proposals.result.current.data).toEqual([]));
  });

  it('refuses a version that is not the one waiting', async () => {
    const decide = renderHook(() => useDecideBrand(ORG), { wrapper: wrapper() });
    await expect(decide.result.current.mutateAsync({ ref: 'harbor/shop', version: 3, approve: false })).rejects.toThrow();
  });

  it('shows no proposals to someone who is not the owner', async () => {
    org().role = 'approver';
    const proposals = renderHook(() => useBrandProposals(ORG), { wrapper: wrapper() });
    await waitFor(() => expect(proposals.result.current.data).toEqual([]));
  });
});

describe('Machines', () => {
  it('says what each computer is doing', async () => {
    const { result } = renderHook(() => useNodes(ORG), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.data).toBeDefined());
    const hetzner = result.current.data!.find((node) => node.id === 'hetzner-1')!;
    expect(hetzner.jobs_running).toBe(1);
    expect(hetzner.lanes_by_kind?.browser).toBeGreaterThan(0);
    expect(hetzner.approvals_waiting).toBeGreaterThan(0);
  });

  it('lists the disposable VMs, read only', async () => {
    const { result } = renderHook(() => useVms(ORG), { wrapper: wrapper() });
    await waitFor(() => expect(result.current.data).toBeDefined());
    expect(result.current.data!.vms.map((vm) => vm.state)).toEqual(['ready', 'booting']);
  });
});
