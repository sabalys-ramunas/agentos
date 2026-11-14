import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { jobs, studio } from '../client';
import { ApiError } from '../http';
import { keys } from '../keys';
import type {
  JobFollow,
  StudioMissing,
} from '../types';
import { JOB_IDLE_POLL_MS, JOB_KEEP_CHARS, JOB_POLL_MS } from './jobs';

/** A list route that answered "studio is not installed here", with the command that adds it. */
export function isStudioMissing(data: unknown): data is StudioMissing {
  return !!data && typeof data === 'object' && (data as { available?: unknown }).available === false;
}

/** One studio run, image or diff asked of a computer without studio: the node's `unavailable` error. */
export function isUnavailable(error: unknown): boolean {
  return error instanceof ApiError && error.code === 'unavailable';
}

export const useJobs = (org: string, options: { lane?: string; state?: string; limit?: number } = {}, enabled = true) =>
  useQuery({
    queryKey: keys.jobs(org, options.lane, options.state),
    queryFn: () => jobs.list(org, options),
    enabled,
    // No run events on the stream yet: every 2 s while one runs, and a quiet look every 15 s otherwise, so a
    // run an agent starts shows up (the rail counts them). A refused or missing route is not asked again.
    refetchInterval: (query) =>
      query.state.error ? false : query.state.data?.jobs.some((job) => job.state === 'running') ? JOB_POLL_MS : query.state.data?.available === false ? false : JOB_IDLE_POLL_MS,
  });

/** One run, with the first part of its output. Read again while it runs. */
export const useJob = (org: string, id: string, enabled = true) =>
  useQuery({
    queryKey: keys.job(org, id),
    queryFn: () => jobs.get(org, id),
    enabled,
    refetchInterval: (query) => (query.state.data?.state === 'running' ? JOB_POLL_MS : false),
  });

function kept(text: string): [string, boolean] {
  return text.length > JOB_KEEP_CHARS ? [text.slice(text.length - JOB_KEEP_CHARS), true] : [text, false];
}

/**
 * A run's output followed from its start: each read asks for what came after the offsets already held,
 * at once while the computer has more, every JOB_POLL_MS while it runs, and stops when it has ended and
 * all of it is here. `trimmed` says the page let go of the oldest output.
 */
export function useJobFollow(org: string, id: string, enabled = true): JobFollow & { trimmed: boolean } {
  const client = useQueryClient();
  const [state, setState] = useState<JobFollow & { trimmed: boolean }>({
    job: null, stdout: '', stderr: '', done: false, error: null, loading: enabled, trimmed: false,
  });
  useEffect(() => {
    if (!enabled) return undefined;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let out = 0;
    let err = 0;
    let stdout = '';
    let stderr = '';
    let trimmed = false;
    setState({ job: null, stdout: '', stderr: '', done: false, error: null, loading: true, trimmed: false });
    const step = async () => {
      try {
        const { output, ...job } = await jobs.get(org, id, { stdout_offset: out, stderr_offset: err });
        if (stopped) return;
        let cut: boolean;
        [stdout, cut] = kept(stdout + output.stdout);
        trimmed ||= cut;
        [stderr, cut] = kept(stderr + output.stderr);
        trimmed ||= cut;
        out = output.next_stdout_offset;
        err = output.next_stderr_offset;
        const done = job.state !== 'running' && !output.more;
        setState({ job, stdout, stderr, done, error: null, loading: false, trimmed });
        if (done) {
          client.setQueryData(keys.job(org, id), { ...job, output });
          void client.invalidateQueries({ queryKey: keys.jobsAll(org) });
          return;
        }
        timer = setTimeout(step, output.more ? 0 : JOB_POLL_MS);
      } catch (error) {
        if (!stopped) setState((current) => ({ ...current, error, loading: false }));
      }
    };
    void step();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
    };
  }, [client, org, id, enabled]);
  return state;
}

export function useCancelJob(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => jobs.cancel(org, id),
    meta: { inline: true },
    onSuccess: (job) => {
      void client.invalidateQueries({ queryKey: keys.jobsAll(org) });
      void client.invalidateQueries({ queryKey: keys.job(org, job.id) });
    },
  });
}

export const useStudioRuns = (org: string, options: { url?: string; profile?: string; limit?: number } = {}, enabled = true) =>
  useQuery({ queryKey: keys.studioRuns(org, options.url, options.profile), queryFn: () => studio.runs(org, options), enabled });

export const useStudioRun = (org: string, id: string, enabled = true) =>
  useQuery({ queryKey: keys.studioRun(org, id), queryFn: () => studio.run(org, id), enabled, retry: (count, error) => !isUnavailable(error) && count < 2 });

export const useStudioBaseline = (org: string, url: string, profile?: string, enabled = true) =>
  useQuery({
    queryKey: keys.studioBaseline(org, url, profile),
    queryFn: () => studio.baseline(org, url, profile),
    enabled: enabled && !!url,
    retry: (count, error) => !isUnavailable(error) && count < 2,
  });

/** Two stored captures compared. A diff of two kept captures never changes, so it is kept as long as the page lives. */
export const useStudioDiff = (org: string, before: string, after: string, enabled = true) =>
  useQuery({
    queryKey: keys.studioDiff(org, before, after),
    queryFn: () => studio.diff(org, before, after),
    enabled: enabled && !!before && !!after,
    staleTime: Infinity,
    retry: (count, error) => !isUnavailable(error) && count < 2,
  });
