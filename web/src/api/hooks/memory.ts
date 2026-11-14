import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { memory } from '../client';
import { keys } from '../keys';
import type {
  MemoryDetail,
  MemoryList,
  MemoryWorkspaces,
} from '../types';

// Memory (the owner in person; see client.ts `memory`). Changes are never optimistic: the
// screen says what changed once the node has done it, and a refusal is shown as the node sent it.

export const useMemoryWorkspaces = (org: string) =>
  useQuery({ queryKey: keys.memoryWorkspaces(org), queryFn: (): Promise<MemoryWorkspaces> => memory.workspaces(org) });

export const useMemoryList = (org: string, filters: { workspace?: string; q?: string; type?: string; trust?: string; unreviewed?: boolean }) =>
  useQuery({
    queryKey: keys.memoryList(org, JSON.stringify(filters)),
    queryFn: (): Promise<MemoryList> => memory.list(org, filters),
    placeholderData: keepPreviousData,
  });

export const useMemoryEntry = (org: string, id: string | null, workspace: string) =>
  useQuery({
    queryKey: keys.memoryEntry(org, id ?? ''),
    queryFn: (): Promise<MemoryDetail> => memory.entry(org, id ?? '', workspace),
    enabled: Boolean(id),
  });

/** After any change to memory: the entry, every list and the counts are read again. */
function useMemoryChanged(org: string) {
  const client = useQueryClient();
  return () => {
    void client.invalidateQueries({ queryKey: keys.memoryAll(org) });
  };
}

export function useConfirmMemory(org: string) {
  const changed = useMemoryChanged(org);
  return useMutation({
    mutationFn: (body: { id: string; workspace: string; version: number; title?: string; body?: string }) =>
      memory.confirm(org, body.id, { workspace: body.workspace, version: body.version, title: body.title, body: body.body }),
    meta: { inline: true },
    onSuccess: changed,
  });
}

export function useReviewMemory(org: string) {
  const changed = useMemoryChanged(org);
  return useMutation({
    mutationFn: ({ id, workspace }: { id: string; workspace: string }) => memory.review(org, id, workspace),
    meta: { inline: true },
    onSuccess: changed,
  });
}

export function useForgetMemory(org: string) {
  const changed = useMemoryChanged(org);
  return useMutation({
    mutationFn: ({ id, workspace, reason }: { id: string; workspace: string; reason: string }) => memory.forget(org, id, workspace, reason),
    meta: { inline: true },
    onSuccess: changed,
  });
}

/** The memory as one plain file (JSON lines or Markdown), every workspace or one. Nothing in it is rendered. */
export function useExportMemory(org: string) {
  return useMutation({
    mutationFn: (options: { workspace?: string; format: 'jsonl' | 'md' }) => memory.exportFile(org, options.workspace, options.format),
    meta: { inline: true },
  });
}
