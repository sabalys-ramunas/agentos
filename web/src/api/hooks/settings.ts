import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { fleet } from '../client';
import { served } from '../http';
import { keys } from '../keys';
import type {
  NotificationSettings,
  PushDevice,
  Webhook,
  WebhookEvent,
} from '../types';

// Notifications

export const useNotifications = (org: string) =>
  useQuery({ queryKey: keys.notifications(org), queryFn: () => served(fleet.notifications(org)) });

/**
 * Each change saves at once and shows at once. If the server refuses it, the screen goes back to
 * what was saved before, and the error says nothing changed.
 */
export function useSaveNotifications(org: string) {
  const client = useQueryClient();
  const key = keys.notifications(org);
  return useMutation({
    mutationFn: (body: NotificationSettings) => fleet.saveNotifications(org, body),
    onMutate: async (body) => {
      await client.cancelQueries({ queryKey: key });
      const before = client.getQueryData<NotificationSettings>(key);
      client.setQueryData(key, body);
      return { before };
    },
    onError: (_error, _body, context) => client.setQueryData(key, context?.before),
    onSuccess: (data) => client.setQueryData(key, data),
  });
}

export function useTestNotification(org: string) {
  return useMutation({ mutationFn: () => fleet.testNotification(org) });
}

// Phone push

export const usePushConfig = (org: string) =>
  useQuery({ queryKey: keys.pushConfig(org), queryFn: () => fleet.pushConfig(org), staleTime: 5 * 60_000 });

export const usePushDevices = (org: string) =>
  useQuery({ queryKey: keys.pushDevices(org), queryFn: () => fleet.pushDevices(org) });

export function useRemovePushDevice(org: string) {
  const client = useQueryClient();
  const key = keys.pushDevices(org);
  return useMutation({
    mutationFn: (id: string) => fleet.removePushDevice(org, id),
    onMutate: async (id) => {
      await client.cancelQueries({ queryKey: key });
      const before = client.getQueryData<PushDevice[]>(key);
      client.setQueryData<PushDevice[]>(key, (items) => items?.filter((item) => item.id !== id));
      return { before };
    },
    onError: (_error, _id, context) => client.setQueryData(key, context?.before),
    onSettled: () => client.invalidateQueries({ queryKey: key }),
  });
}

export function useTestPush(org: string) {
  return useMutation({ mutationFn: () => fleet.testPush(org), meta: { inline: true } });
}

// Webhooks

export const useWebhooks = (org: string) =>
  useQuery({ queryKey: keys.webhooks(org), queryFn: () => served(fleet.webhooks(org)) });

export const useWebhookDeliveries = (org: string, id: string | null) =>
  useQuery({
    queryKey: keys.webhookDeliveries(org, id ?? ''),
    queryFn: () => fleet.webhookDeliveries(org, id ?? ''),
    enabled: id !== null,
    // A test lands in a moment, so the log looks again while it is open.
    refetchInterval: 4_000,
  });

export function useCreateWebhook(org: string) {
  const client = useQueryClient();
  return useMutation({
    meta: { inline: true },
    mutationFn: (body: { url: string; events: WebhookEvent[] }) => fleet.createWebhook(org, body),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.webhooks(org) }),
  });
}

/** Turning one on or off, or changing its events, shows at once and rolls back on a refusal. */
export function useUpdateWebhook(org: string) {
  const client = useQueryClient();
  const key = keys.webhooks(org);
  return useMutation({
    mutationFn: ({ id, ...body }: { id: string; enabled?: boolean; events?: WebhookEvent[] }) => fleet.updateWebhook(org, id, body),
    onMutate: async ({ id, ...body }) => {
      await client.cancelQueries({ queryKey: key });
      const before = client.getQueryData<Webhook[]>(key);
      client.setQueryData<Webhook[]>(key, (items) => items?.map((item) => (item.id === id ? { ...item, ...body } : item)));
      return { before };
    },
    onError: (_error, _body, context) => client.setQueryData(key, context?.before),
    onSuccess: (data) => client.setQueryData<Webhook[]>(key, (items) => items?.map((item) => (item.id === data.id ? data : item))),
  });
}

export function useDeleteWebhook(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => fleet.deleteWebhook(org, id),
    onSuccess: () => client.invalidateQueries({ queryKey: keys.webhooks(org) }),
  });
}

export function useRotateWebhook(org: string) {
  return useMutation({ meta: { inline: true }, mutationFn: (id: string) => fleet.rotateWebhook(org, id) });
}

export function useTestWebhook(org: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (id: string) => fleet.testWebhook(org, id),
    onSuccess: (_data, id) => {
      client.invalidateQueries({ queryKey: keys.webhookDeliveries(org, id) });
      client.invalidateQueries({ queryKey: keys.webhooks(org) });
    },
  });
}

// Retention

/** The owner's export, through the usual step-up. The screen saves the file. */
export function useExportActivity(org: string) {
  return useMutation({
    meta: { inline: true },
    mutationFn: (options: { format: 'csv' | 'ndjson'; start?: string; workspace?: string }) => fleet.exportActivity(org, options),
  });
}

export const useRetention = (org: string) =>
  useQuery({ queryKey: keys.retention(org), queryFn: () => served(fleet.retention(org)), staleTime: 5 * 60_000 });
