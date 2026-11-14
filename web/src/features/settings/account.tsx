import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useParams } from '@tanstack/react-router';
import { Fingerprint, Laptop, Smartphone, Trash2 } from 'lucide-react';
import { useEffect, useState } from 'react';
import { toast } from 'sonner';
import { auth } from '@/api/client';
import { useDeletePasskey, useDevices, usePasskeys, useProviders, useRevokeDevice, useSession, useUpdateMe } from '@/api/hooks';
import { isCancelled } from '@/api/http';
import { keys } from '@/api/keys';
import { Badge } from '@/components/ui/badge';
import { Button, IconButton } from '@/components/ui/button';
import { TechnicalToggle } from '@/components/shell/technical-toggle';
import { Card, FormCard } from '@/components/ui/card';
import { Field, Input } from '@/components/ui/field';
import { EmptyState, QueryView, Skeleton } from '@/components/ui/states';
import { describeError } from '@/lib/errors';
import { date, timeAgo } from '@/lib/format';
import { createPasskey, passkeyError } from '@/lib/webauthn';
import { SettingsHeader } from './page';

export function ProfileSettings() {
  useParams({ from: '/$org/settings/account' });
  const user = useSession().data?.user;
  const update = useUpdateMe();
  const [name, setName] = useState(user?.name ?? '');
  useEffect(() => setName(user?.name ?? ''), [user?.name]);
  // A computer has no email: its people sign in with a passkey or this computer's code.
  const hasEmail = Boolean(user?.email);
  return (
    <>
      <SettingsHeader title="Profile" />
      <FormCard
        title="Your details"
        footnote={hasEmail ? 'Your email is your sign-in, so it cannot be changed here.' : 'Your sign-in is a passkey, or a code from this computer.'}
        action={
          <Button
            variant="primary"
            size="sm"
            loading={update.isPending}
            disabled={!name.trim() || name === user?.name}
            onClick={() => update.mutate({ name: name.trim() }, { onSuccess: () => toast('Saved') })}
          >
            Save
          </Button>
        }
      >
        <div className="grid max-w-sm gap-4">
          <Field label="Name">
            <Input value={name} onChange={(event) => setName(event.target.value)} />
          </Field>
          {hasEmail ? (
            <Field label="Email">
              <Input value={user?.email ?? ''} readOnly className="bg-subtle text-muted hover:border-line" />
            </Field>
          ) : null}
        </div>
      </FormCard>
      <FormCard title="What you see" className="mt-6">
        <TechnicalToggle variant="row" />
      </FormCard>
    </>
  );
}

/** Linking is a full page visit to Google from this signed-in session, as sign-in is. */
function LinkGoogle({ org }: { org: string }) {
  const google = useProviders().data?.google;
  if (!google) return null;
  return (
    <FormCard
      className="mt-6"
      title="Google"
      description="Sign in with Google as well. Only a Google account for this same email can be linked."
      action={
        <Button size="sm" asChild>
          <a href={auth.googleStartUrl({ link: true, next: `/${org}/settings/passkeys` })}>Link Google</a>
        </Button>
      }
    />
  );
}

export function PasskeySettings() {
  const { org } = useParams({ from: '/$org/settings/passkeys' });
  const client = useQueryClient();
  const passkeys = usePasskeys();
  const remove = useDeletePasskey();
  const add = useMutation({
    mutationFn: async () => {
      const options = await auth.passkeyRegisterOptions();
      return auth.passkeyRegisterVerify(await createPasskey(options));
    },
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.passkeys });
      void client.invalidateQueries({ queryKey: keys.session });
      toast('Passkey added');
    },
    onError: (error) => {
      if (!isCancelled(error)) toast.error(passkeyError(error) ?? describeError(error));
    },
  });
  return (
    <>
      <SettingsHeader
        title="Passkeys"
        description="Sign in and confirm sensitive actions with your device, without a password."
        action={
          <Button loading={add.isPending} icon={<Fingerprint className="size-4" />} onClick={() => add.mutate()}>
            Add passkey
          </Button>
        }
      />
      <Card>
        <QueryView query={passkeys} skeleton={<Skeleton className="m-6 h-10" />}>
          {(items) =>
            items.length ? (
              <ul className="divide-y divide-line">
                {items.map((item) => (
                  <li key={item.id} className="flex items-center gap-4 px-4 py-3 sm:px-6">
                    <Fingerprint className="size-4 text-faint" />
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-14">{item.name}</p>
                      <p className="truncate text-13 text-faint">
                        Added {date(item.created_at)}
                        {item.last_used_at ? ` · Used ${timeAgo(item.last_used_at).toLowerCase()}` : ''}
                      </p>
                    </div>
                    <IconButton label={`Remove ${item.name}`} size="sm" onClick={() => remove.mutate(item.id, { onSuccess: () => toast('Passkey removed') })}>
                      <Trash2 className="size-4" />
                    </IconButton>
                  </li>
                ))}
              </ul>
            ) : (
              <EmptyState
                icon={<Fingerprint />}
                title="No passkeys yet"
                description="Add one to sign in with a tap. It also makes confirming sensitive actions a single touch."
              />
            )
          }
        </QueryView>
      </Card>
      <LinkGoogle org={org} />
    </>
  );
}

export function DeviceSettings() {
  useParams({ from: '/$org/settings/devices' });
  const devices = useDevices();
  const revoke = useRevokeDevice();
  return (
    <>
      <SettingsHeader title="Devices" description="Where you are signed in. Sign out anything you do not recognise." />
      <Card>
        <QueryView query={devices} skeleton={<Skeleton className="m-6 h-10" />}>
          {(items) => (
            <ul className="divide-y divide-line">
              {items.map((item) => {
                const Icon = /iphone|android/i.test(item.label) ? Smartphone : Laptop;
                return (
                  <li key={item.id} className="flex items-center gap-4 px-4 py-3 sm:px-6">
                    <Icon className="size-4 text-faint" />
                    <div className="min-w-0 flex-1">
                      <p className="flex items-center gap-2 text-14">
                        <span className="truncate">{item.label}</span>
                        {item.current ? <Badge>This device</Badge> : null}
                      </p>
                      <p className="truncate text-13 text-faint">
                        <span className="mono">{item.ip}</span>
                        {item.location ? ` · ${item.location}` : ''}
                        {item.current ? '' : ` · ${timeAgo(item.last_active_at)}`}
                      </p>
                    </div>
                    {item.current ? null : (
                      <Button size="sm" variant="ghost" onClick={() => revoke.mutate(item.id, { onSuccess: () => toast('Signed out') })}>
                        Sign out
                      </Button>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
        </QueryView>
      </Card>
    </>
  );
}
