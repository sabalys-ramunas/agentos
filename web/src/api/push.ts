/**
 * Phone push for this browser: the browser's subscription on one side, the member's device rows
 * on the other. Screens use `usePushDevice` and never touch PushManager themselves.
 */
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { isIos, isStandalone } from '@/lib/pwa';
import { fleet } from './client';
import { ApiError } from './http';
import { usePushConfig, usePushDevices } from './hooks';
import { keys } from './keys';

/** Why this browser cannot take push, or null when it can. */
export type PushBlock = 'unsupported' | 'needs_install' | 'denied' | null;

export function pushBlock(): PushBlock {
  const capable = typeof window !== 'undefined' && 'Notification' in window && 'serviceWorker' in navigator && 'PushManager' in window;
  // Safari on iPhone and iPad has no PushManager until openajar is on the Home Screen.
  if (!capable) return isIos() && !isStandalone() ? 'needs_install' : 'unsupported';
  if (Notification.permission === 'denied') return 'denied';
  return null;
}

const READY_MS = 8_000;

async function registration(): Promise<ServiceWorkerRegistration> {
  const timeout = new Promise<never>((_, reject) =>
    setTimeout(() => reject(new ApiError(0, 'push_unavailable', 'The app is still starting. Reload the page and try again.')), READY_MS),
  );
  return Promise.race([navigator.serviceWorker.ready, timeout]);
}

function keyBytes(base64url: string): Uint8Array<ArrayBuffer> {
  const padded = `${base64url}${'='.repeat((4 - (base64url.length % 4)) % 4)}`.replaceAll('-', '+').replaceAll('_', '/');
  const raw = atob(padded);
  const bytes = new Uint8Array(new ArrayBuffer(raw.length));
  for (let index = 0; index < raw.length; index += 1) bytes[index] = raw.charCodeAt(index);
  return bytes;
}

function sameKey(subscription: PushSubscription, key: Uint8Array): boolean {
  const current = subscription.options.applicationServerKey;
  if (!current) return true;
  const bytes = new Uint8Array(current);
  return bytes.length === key.length && bytes.every((value, index) => value === key[index]);
}

export async function currentSubscription(): Promise<PushSubscription | null> {
  if (pushBlock() !== null) return null;
  return (await registration()).pushManager.getSubscription();
}

/** Asks the browser, then subscribes. Must run from a tap: browsers refuse a prompt otherwise. */
async function subscribeBrowser(publicKey: string) {
  const permission = await Notification.requestPermission();
  if (permission !== 'granted') {
    throw new ApiError(0, 'push_denied', permission === 'denied' ? 'Notifications are blocked for this site.' : 'Notifications were not allowed.');
  }
  const pushManager = (await registration()).pushManager;
  const key = keyBytes(publicKey);
  let subscription = await pushManager.getSubscription();
  // A subscription made under an older server key cannot be reused.
  if (subscription && !sameKey(subscription, key)) {
    await subscription.unsubscribe();
    subscription = null;
  }
  subscription ??= await pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
  const json = subscription.toJSON();
  if (!json.endpoint || !json.keys?.p256dh || !json.keys.auth) {
    throw new ApiError(0, 'push_unavailable', 'This browser gave an incomplete subscription. Try again.');
  }
  return { endpoint: json.endpoint, keys: { p256dh: json.keys.p256dh, auth: json.keys.auth } };
}

const deviceKey = (org: string) => `agentos.push.device.${org}`;

function storedDevice(org: string): string | null {
  try {
    return localStorage.getItem(deviceKey(org));
  } catch {
    return null;
  }
}

function storeDevice(org: string, id: string | null) {
  try {
    if (id) localStorage.setItem(deviceKey(org), id);
    else localStorage.removeItem(deviceKey(org));
  } catch {
    // Private mode. This device is then recognised for this page only.
  }
}

/** Everything the "This device" card shows, and the two things it can do. */
export function usePushDevice(org: string) {
  const client = useQueryClient();
  const config = usePushConfig(org);
  const devices = usePushDevices(org);
  const [block, setBlock] = useState<PushBlock>(() => pushBlock());
  const [subscribed, setSubscribed] = useState<boolean | null>(null);
  const [deviceId, setDeviceId] = useState<string | null>(() => storedDevice(org));

  useEffect(() => {
    let live = true;
    currentSubscription()
      .then((subscription) => live && setSubscribed(subscription !== null))
      .catch(() => live && setSubscribed(false));
    return () => {
      live = false;
    };
  }, []);

  const enable = useMutation({
    meta: { inline: true },
    mutationFn: async () => {
      const key = config.data?.public_key;
      if (!key) throw new ApiError(0, 'push_unavailable', 'Phone push is not set up on this server.');
      try {
        const body = await subscribeBrowser(key);
        return await fleet.addPushDevice(org, body);
      } finally {
        setBlock(pushBlock());
      }
    },
    onSuccess: (device) => {
      storeDevice(org, device.id);
      setDeviceId(device.id);
      setSubscribed(true);
      void client.invalidateQueries({ queryKey: keys.pushDevices(org) });
    },
  });

  const disable = useMutation({
    meta: { inline: true },
    mutationFn: async () => {
      const subscription = await currentSubscription().catch(() => null);
      await subscription?.unsubscribe();
      if (deviceId) await fleet.removePushDevice(org, deviceId).catch((error) => (error instanceof ApiError && error.status === 404 ? undefined : Promise.reject(error)));
    },
    onSuccess: () => {
      storeDevice(org, null);
      setDeviceId(null);
      setSubscribed(false);
      void client.invalidateQueries({ queryKey: keys.pushDevices(org) });
    },
  });

  const list = devices.data ?? [];
  const thisDevice = deviceId ? (list.find((item) => item.id === deviceId) ?? null) : null;
  return {
    config,
    devices,
    block,
    /** Null until the browser has said. */
    on: subscribed === null ? null : subscribed && thisDevice !== null,
    thisDeviceId: thisDevice?.id ?? null,
    enable,
    disable,
  };
}

/**
 * Safe on every app open: with permission already given and a subscription in this browser, the
 * device row is refreshed (or made again after a sign-out removed it). It never asks for anything.
 */
export function usePushSync(org: string) {
  const config = usePushConfig(org);
  const key = config.data?.available ? config.data.public_key : null;
  useEffect(() => {
    if (!key || pushBlock() !== null || Notification.permission !== 'granted') return;
    let live = true;
    void (async () => {
      const subscription = await currentSubscription().catch(() => null);
      if (!subscription || !live || !sameKey(subscription, keyBytes(key))) return;
      const json = subscription.toJSON();
      if (!json.endpoint || !json.keys?.p256dh || !json.keys.auth) return;
      const device = await fleet.addPushDevice(org, { endpoint: json.endpoint, keys: { p256dh: json.keys.p256dh, auth: json.keys.auth } }).catch(() => null);
      if (device && live) storeDevice(org, device.id);
    })();
    return () => {
      live = false;
    };
  }, [org, key]);
}
