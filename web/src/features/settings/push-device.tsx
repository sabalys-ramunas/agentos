/**
 * Push on this device, and the member's other devices. Every state the browser, the OS, the server
 * or the plan can put push in says what happened and what to do, so a dead switch never sits there.
 */
import { InlineError } from '@/components/ui/states';
import { Link } from '@tanstack/react-router';
import { BellOff, BellRing, Laptop, Smartphone } from 'lucide-react';
import type { ReactNode } from 'react';
import { usePushDevice } from '@/api/push';
import type { PushDevice } from '@/api/types';
import { useProviders, useRemovePushDevice, useTestPush } from '@/api/hooks';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { FormCard } from '@/components/ui/card';
import { AppTile } from '@/components/ui/tile';
import { Skeleton } from '@/components/ui/states';
import { StatusDot } from '@/components/ui/status';
import { Switch } from '@/components/ui/toggle';
import { describeError } from '@/lib/errors';
import { timeAgo } from '@/lib/format';
import { isIos, useInstall } from '@/lib/pwa';
import { useTechnical } from '@/lib/technical';
import { switched, toastChange } from '@/components/ui/toast';

function Note({ title, children, action }: { title: string; children: ReactNode; action?: ReactNode }) {
  return (
    <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between sm:gap-6">
      <div className="flex min-w-0 gap-3">
        <span className="flex h-5 shrink-0 items-center">
          <BellOff aria-hidden className="size-4 text-faint" />
        </span>
        <div className="min-w-0">
          <p className="text-label text-fg">{title}</p>
          <div className="mt-1 text-secondary text-muted">{children}</div>
        </div>
      </div>
      {action ? <div className="shrink-0 pl-7 sm:pl-0">{action}</div> : null}
    </div>
  );
}

/** Where to turn notifications back on, in the words of the browser the person is using. */
function unblockSteps(): string {
  if (isIos()) return 'Open Settings, then Notifications, then openajar, and turn on Allow Notifications.';
  if (/Firefox\//.test(navigator.userAgent)) return 'Click the icon left of the address, open Permissions, and clear Notifications.';
  if (/Safari\//.test(navigator.userAgent) && !/Chrome\//.test(navigator.userAgent)) {
    return 'Open Safari, then Settings, then Websites, then Notifications, and set this site to Allow.';
  }
  return 'Click the icon left of the address, open Site settings, and set Notifications to Allow.';
}

function InstallFirst() {
  const install = useInstall();
  return (
    <Note title="Add openajar to your Home Screen first">
      On iPhone and iPad, push reaches only apps on the Home Screen (iOS 16.4 or later).{' '}
      {install.installed ? 'Open openajar from the Home Screen and turn it on there.' : 'Tap Share, then Add to Home Screen, and open openajar from there.'}
    </Note>
  );
}

function DeviceIcon({ label }: { label: string }) {
  const Icon = /iPhone|iPad|Android/.test(label) ? Smartphone : Laptop;
  return <Icon className="size-4 shrink-0 text-faint" />;
}

function DeviceRow({ org, device, current }: { org: string; device: PushDevice; current: boolean }) {
  const remove = useRemovePushDevice(org);
  return (
    <li className="flex min-h-14 items-center gap-3 py-2">
      <DeviceIcon label={device.label} />
      <div className="min-w-0 flex-1">
        <p className="flex items-center gap-2 text-body">
          <span className="truncate">{device.label}</span>
          {current ? <Badge>This device</Badge> : null}
        </p>
        <p className="text-secondary text-faint">
          Added {timeAgo(device.created_at).toLowerCase()} · {device.last_sent_at ? `last push ${timeAgo(device.last_sent_at).toLowerCase()}` : 'no push yet'}
        </p>
      </div>
      {current ? null : (
        <Button size="sm" variant="ghost" loading={remove.isPending && remove.variables === device.id} onClick={() => remove.mutate(device.id)}>
          Remove
        </Button>
      )}
    </li>
  );
}

export function PushCard({ org, enabledInSettings }: { org: string; enabledInSettings: boolean }) {
  const push = usePushDevice(org);
  const test = useTestPush(org);
  const { config, devices, block, on } = push;
  // A computer sends no email, so its note does not promise an urgent one.
  const emailable = useProviders().data?.magic_link !== false;
  const [technical] = useTechnical();
  const busy = push.enable.isPending || push.disable.isPending;
  const failure = push.enable.error ?? push.disable.error ?? test.error;
  const others = (devices.data ?? []).filter((item) => item.id !== push.thisDeviceId);
  const current = (devices.data ?? []).find((item) => item.id === push.thisDeviceId);

  let body: ReactNode;
  if (config.isPending || on === null) {
    body = (
      <div className="flex items-center justify-between gap-6" role="status" aria-label="Loading">
        <div className="grid flex-1 gap-2">
          <Skeleton className="h-4 w-40" />
          <Skeleton className="h-3 w-64" />
        </div>
        <Skeleton className="h-5 w-9 rounded-full" />
      </div>
    );
  } else if (config.data?.reason === 'not_configured') {
    body = (
      <Note title="Phone push is not set up on this server">
        {emailable
          ? 'Approvals still wait in Needs you, and urgent ones come by email. The person who runs this server can turn push on.'
          : enabledInSettings
            ? 'Approvals still wait in Needs you. They also reach your phone through ntfy, the free app.'
            : (
                <>
                  Approvals still wait in Needs you. To reach your phone instead, install the free ntfy app, then set it up on this computer, in a terminal.
                  {technical ? <span className="mono mt-1 block text-caption text-faint">openajar policy notify ntfy</span> : null}
                </>
              )}
      </Note>
    );
  } else if (config.data?.reason === 'plan') {
    body = (
      <Note
        title="Your plan does not include phone push"
        action={
          <Button size="sm" asChild>
            <Link to="/$org/settings/billing" params={{ org }}>
              See plans
            </Link>
          </Button>
        }
      >
        Approvals still wait in Needs you, and urgent ones come by email.
      </Note>
    );
  } else if (block === 'needs_install') {
    body = <InstallFirst />;
  } else if (block === 'unsupported') {
    body = <Note title="This browser cannot receive push">Approvals still wait in Needs you, and urgent ones come by email. Chrome, Edge, Firefox and Safari can all receive push.</Note>;
  } else if (block === 'denied') {
    body = <Note title="Notifications are blocked for this site">{unblockSteps()} Then come back and turn push on.</Note>;
  } else {
    body = (
      <div className="flex items-center justify-between gap-6">
        <div className="min-w-0">
          <p className="flex items-center gap-2 text-label text-fg">
            <StatusDot tone={on ? 'ok' : 'idle'} />
            {on ? 'Push is on for this device' : 'Push is off for this device'}
          </p>
          <p className="mt-1 text-secondary text-muted">
            {on && !enabledInSettings
              ? 'Push to your devices is turned off below, so nothing is sent until you turn it back on.'
              : 'An approval arrives with Review and Deny. Approving always happens in openajar, signed in.'}
          </p>
        </div>
        <Switch
          aria-label="Push on this device"
          checked={on}
          aria-busy={busy || undefined}
          onCheckedChange={(next) => {
            if (busy) return;
            // Undo turns it the other way through the same path: on subscribes this browser again, off removes it.
            const words = switched('Push on this device', next);
            const undo = () => (next ? push.disable.mutateAsync() : push.enable.mutateAsync());
            (next ? push.enable : push.disable).mutate(undefined, { onSuccess: () => toastChange({ ...words, undo }) });
          }}
        />
      </div>
    );
  }

  const canTest = (devices.data?.length ?? 0) > 0 && config.data?.available;
  return (
    <FormCard
      tile={<AppTile tone="pink" icon={BellRing} size="sm" />}
      title="Push"
      description="On your phone or computer, even when openajar is closed."
      footnote={
        canTest ? (
          <span role="status">
            {test.isSuccess
              ? `Sent to ${test.data.devices === 1 ? 'your device' : `your ${test.data.devices} devices`}. It ignores quiet hours.`
              : 'A test goes to every device you have, and ignores quiet hours.'}
          </span>
        ) : undefined
      }
      action={
        canTest ? (
          <Button size="sm" loading={test.isPending} onClick={() => test.mutate()}>
            Send a test
          </Button>
        ) : undefined
      }
    >
      {body}
      {failure ? <InlineError>{describeError(failure)}</InlineError> : null}
      {current || others.length ? (
        <div className="border-t border-line pt-1">
          <ul className="divide-y divide-line">
            {current ? <DeviceRow org={org} device={current} current /> : null}
            {others.map((device) => (
              <DeviceRow key={device.id} org={org} device={device} current={false} />
            ))}
          </ul>
        </div>
      ) : null}
    </FormCard>
  );
}
