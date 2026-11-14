import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { fleet } from '../client';
import { served } from '../http';
import { keys } from '../keys';

export const useBrowserDevices = (org: string) =>
  useQuery({ queryKey: keys.browserDevices(org), queryFn: () => served(fleet.browserDevices(org)) });

export function useCreateBrowserDevice(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (body: Parameters<typeof fleet.createBrowserDevice>[1]) => fleet.createBrowserDevice(org, body),
    onSuccess: () => void client.invalidateQueries({ queryKey: keys.browserDevices(org) }),
  });
}

export function useRemoveBrowserDevice(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (name: string) => fleet.removeBrowserDevice(org, name),
    onSuccess: () => void client.invalidateQueries({ queryKey: keys.browserDevices(org) }),
  });
}

/** What moving a lane to a device changes. Asked only when the person picks a device to move to. */
export const useDeviceMovePlan = (org: string, ws: string, name: string, to: string | null) =>
  useQuery({
    queryKey: keys.deviceMove(org, ws, name, to ?? 'none'),
    queryFn: () => fleet.deviceMovePlan(org, ws, name, to),
    enabled: to !== undefined,
  });

export function useMoveLaneDevice(org: string, ws: string, name: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (body: { device: string | null; confirm?: boolean }) => fleet.moveLaneDevice(org, ws, name, body),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.browserDevices(org) });
      void client.invalidateQueries({ queryKey: keys.lanesAll(org) });
    },
  });
}

export const useProxies = (org: string) =>
  useQuery({ queryKey: keys.proxies(org), queryFn: () => served(fleet.proxies(org)) });

export function useCreateProxy(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (body: Parameters<typeof fleet.createProxy>[1]) => fleet.createProxy(org, body),
    onSuccess: () => void client.invalidateQueries({ queryKey: keys.proxies(org) }),
  });
}
