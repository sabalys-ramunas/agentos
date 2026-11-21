import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useParams } from '@tanstack/react-router';
import { Fingerprint, Laptop, Link2, Plus, Smartphone } from 'lucide-react';
import { useEffect, useState } from 'react';
import { toast } from 'sonner';
import { auth } from '@/api/client';
import { useDeletePasskey, useDevices, usePasskeys, useProviders, useRevokeDevice, useSession, useUpdateMe } from '@/api/hooks';
import { isCancelled } from '@/api/http';
import { keys } from '@/api/keys';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/dialog';
import { AppTile } from '@/components/ui/tile';
import { TechnicalToggle } from '@/components/shell/technical-toggle';
import { Card, FormCard } from '@/components/ui/card';
import { Field, Input } from '@/components/ui/field';
import { FormActions, FormNote, useSaved } from '@/components/ui/form';
import { UnsavedChanges } from '@/components/unsaved-changes';
import { EmptyState, QueryView, Skeleton } from '@/components/ui/states';
import { describeError } from '@/lib/errors';
import { date, timeAgo } from '@/lib/format';
import { createPasskey, passkeyError } from '@/lib/webauthn';
import { SettingsHeader } from './page';

export function ProfileSettings() {
  useParams({ from: '/$org/settings/account' });
  const user = useSession().data?.user;
  const update = useUpdateMe();
  const { saved, savedAt, flash, clear } = useSaved();
  const [name, setName] = useState(user?.name ?? '');
  const [tried, setTried] = useState(false);
  useEffect(() => setName(user?.name ?? ''), [user?.name]);
  // A computer has no email: its people sign in with a passkey or this computer's code.
  const hasEmail = Boolean(user?.email);
  const dirty = name.trim() !== (user?.name ?? '');
  const problem = tried && !name.trim() ? 'Type the name people in this organization know you by.' : null;
  const error = problem ?? (update.isError ? describeError(update.error) : null);
  const save = () => {
    setTried(true);
    if (!name.trim()) return;
    update.mutate({ name: name.trim() }, { onSuccess: flash });
  };
  return (
    <>
      <SettingsHeader title="Profile" description="You, as others in this organization see you." />
      <UnsavedChanges when={dirty} what="your name" />
      <div className="grid gap-4">
        <FormCard
          title="Your details"
          onSubmit={save}
          footnote={<FormNote dirty={dirty} saving={update.isPending} savedAt={savedAt} problems={error ? 1 : 0} />}
          action={
            <FormActions
              dirty={dirty}
              saving={update.isPending}
              saved={saved}
              onDiscard={() => {
                setName(user?.name ?? '');
                setTried(false);
                update.reset();
              }}
            />
          }
        >
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Name" error={error} required>
              <Input
                value={name}
                autoComplete="name"
                onChange={(event) => {
                  setName(event.target.value);
                  clear();
                  update.reset();
                }}
                onBlur={() => setTried(true)}
              />
            </Field>
            <Field label="Email" hint={hasEmail ? 'Your email is your sign-in, so it cannot be changed here.' : 'Your sign-in is a passkey, or a code from this computer.'}>
              <Input value={user?.email ?? ''} readOnly placeholder={hasEmail ? undefined : 'No email on this computer'} />
            </Field>
          </div>
        </FormCard>
        <FormCard title="What you see">
          <TechnicalToggle variant="row" />
        </FormCard>
      </div>
    </>
  );
}

/** Linking is a full page visit to Google from this signed-in session, as sign-in is. */
function LinkGoogle({ org }: { org: string }) {
  const google = useProviders().data?.google;
  if (!google) return null;
  return (
    <FormCard title="Google" description="Sign in with Google as well. Only a Google account for this same email can be linked.">
      <Button size="sm" className="justify-self-start" asChild>
        <a href={auth.googleStartUrl({ link: true, next: `/${org}/settings/passkeys` })}>
          <Link2 className="size-4" />
          Link Google
        </a>
      </Button>
    </FormCard>
  );
}

export function PasskeySettings() {
  const { org } = useParams({ from: '/$org/settings/passkeys' });
  const client = useQueryClient();
  const passkeys = usePasskeys();
  const remove = useDeletePasskey();
  const [removing, setRemoving] = useState<{ id: string; name: string } | null>(null);
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
          <Button variant="primary" loading={add.isPending} icon={<Plus className="size-4" />} onClick={() => add.mutate()}>
            Add passkey
          </Button>
        }
      />
      <div className="grid gap-4">
      <Card>
        <QueryView query={passkeys} skeleton={<Skeleton className="m-6 h-10" />}>
          {(items) =>
            items.length ? (
              <ul className="divide-y divide-line">
                {items.map((item) => (
                  <li key={item.id} className="flex min-h-15 items-center gap-3 py-3 pr-3 pl-4 sm:pl-5">
                    <AppTile tone="green" icon={Fingerprint} size="sm" />
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-label text-fg" title={item.name}>
                        {item.name}
                      </p>
                      <p className="truncate text-secondary text-muted">
                        Added {date(item.created_at)}
                        {item.last_used_at ? ` · Used ${timeAgo(item.last_used_at).toLowerCase()}` : ''}
                      </p>
                    </div>
                    <Button size="sm" variant="ghost" aria-label={`Remove ${item.name}`} onClick={() => setRemoving(item)}>
                      Remove
                    </Button>
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
      </div>
      <ConfirmDialog
        open={removing !== null}
        onOpenChange={(open) => !open && setRemoving(null)}
        title={`Remove ${removing?.name ?? 'this passkey'}?`}
        description={
          (passkeys.data?.length ?? 0) > 1
            ? 'You can no longer sign in or confirm with it. Your other passkeys keep working.'
            : 'It is your only passkey. You can still sign in with your email, and add a passkey again any time.'
        }
        confirmLabel="Remove passkey"
        danger
        loading={remove.isPending}
        onConfirm={() =>
          removing &&
          remove.mutate(removing.id, {
            onSuccess: () => {
              setRemoving(null);
              toast('Passkey removed');
            },
          })
        }
      />
    </>
  );
}

export function DeviceSettings() {
  useParams({ from: '/$org/settings/devices' });
  const devices = useDevices();
  const revoke = useRevokeDevice();
  const [everywhere, setEverywhere] = useState(false);
  const others = (devices.data ?? []).filter((item) => !item.current);
  const signOutOthers = async () => {
    for (const item of others) await revoke.mutateAsync(item.id).catch(() => undefined);
    setEverywhere(false);
    toast('Signed out everywhere else');
  };
  return (
    <>
      <SettingsHeader
        title="Devices"
        description="Where you are signed in. Sign out anything you do not recognise."
        action={others.length > 1 ? <Button onClick={() => setEverywhere(true)}>Sign out everywhere else</Button> : undefined}
      />
      <Card>
        <QueryView query={devices} skeleton={<Skeleton className="m-6 h-10" />}>
          {(items) => (
            <ul className="divide-y divide-line">
              {items.map((item) => {
                const Icon = /iphone|android/i.test(item.label) ? Smartphone : Laptop;
                return (
                  <li key={item.id} className="flex min-h-15 items-center gap-3 py-3 pr-3 pl-4 sm:pl-5">
                    <AppTile tone="ink" icon={Icon} size="sm" />
                    <div className="min-w-0 flex-1">
                      <p className="flex items-center gap-2 text-label text-fg">
                        <span className="truncate">{item.label}</span>
                        {item.current ? <Badge tone="accent">This device</Badge> : null}
                      </p>
                      <p className="truncate text-secondary text-muted">
                        <span className="mono text-code-caption">{item.ip}</span>
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
      <ConfirmDialog
        open={everywhere}
        onOpenChange={setEverywhere}
        title="Sign out everywhere else?"
        description={`${others.length} other devices are signed out at once. This one stays signed in, and you can sign in again on any of them.`}
        confirmLabel="Sign them out"
        loading={revoke.isPending}
        onConfirm={() => void signOutOthers()}
      />
    </>
  );
}
