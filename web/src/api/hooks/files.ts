import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { accountFiles } from '../client';
import { keys } from '../keys';
import type {
  AccountFiles,
} from '../types';

export const useAccountFiles = (org: string, ws: string, name: string) =>
  useQuery({ queryKey: keys.laneFiles(org, `${ws}/${name}`), queryFn: (): Promise<AccountFiles> => accountFiles.list(org, ws, name) });

export function useUploadAccountFile(org: string, ws: string, name: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ file, fileName }: { file: Blob; fileName: string }) => accountFiles.upload(org, ws, name, file, fileName),
    meta: { inline: true },
    onSuccess: () => void client.invalidateQueries({ queryKey: keys.laneFiles(org, `${ws}/${name}`) }),
  });
}

export function useDeleteAccountFile(org: string, ws: string, name: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => accountFiles.remove(org, ws, name, id),
    meta: { inline: true },
    onSuccess: () => void client.invalidateQueries({ queryKey: keys.laneFiles(org, `${ws}/${name}`) }),
  });
}

/** One file, as the bytes it is, for the person to save. Nothing in the app opens it. */
export function useDownloadAccountFile(org: string, ws: string, name: string) {
  return useMutation({
    mutationFn: (id: string) => accountFiles.download(org, ws, name, id),
    meta: { inline: true },
  });
}
