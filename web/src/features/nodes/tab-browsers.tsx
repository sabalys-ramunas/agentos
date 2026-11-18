import { Globe, Plus, Trash2 } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';
import { isNotServed } from '@/api/http';
import { useAccess, useBrowserDevices, useCreateBrowserDevice, useCreateProxy, useDeviceMovePlan, useMoveLaneDevice, useProxies, useRemoveBrowserDevice } from '@/api/hooks';
import type { BrowserDevice, Proxy } from '@/api/types';
import { Button, IconButton } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { ConfirmDialog, Dialog } from '@/components/ui/dialog';
import { Field, Input } from '@/components/ui/field';
import { Select } from '@/components/ui/select';
import { Status } from '@/components/ui/status';
import { EmptyState, InlineError, NotLive, QueryView, Skeleton } from '@/components/ui/states';
import { plural } from '@/lib/format';
import { THINGS } from '@/lib/vocabulary';
import { useTechnical } from '@/lib/technical';
import { FormDialog } from '@/features/settings/on-computer';

const NAME = /^[a-z0-9][a-z0-9_-]{0,31}$/;

/** A connection's address is plumbing: it shows only with Technical details. */
function addressOf(proxies: Proxy[] | undefined, label: string | null) {
  const found = proxies?.find((item) => item.label === label);
  return found?.host && found.port ? `${found.host}:${found.port}` : null;
}

function AddBrowserDialog({ org, open, onOpenChange, proxies }: { org: string; open: boolean; onOpenChange: (open: boolean) => void; proxies: Proxy[] }) {
  const create = useCreateBrowserDevice(org);
  const [name, setName] = useState('');
  const [connection, setConnection] = useState('');
  const [note, setNote] = useState('');
  const ready = NAME.test(name.trim()) && connection !== '';
  const close = (next: boolean) => {
    if (!next) {
      setName('');
      setConnection('');
      setNote('');
    }
    onOpenChange(next);
  };
  return (
    <Dialog
      open={open}
      onOpenChange={close}
      title="Add a browser"
      description="A browser keeps one fingerprint and one connection. The accounts you put in it share both, and each sign-in stays in its own browser."
      footer={
        <>
          <Button onClick={() => close(false)}>Cancel</Button>
          <Button
            variant="primary"
            disabled={!ready}
            loading={create.isPending}
            onClick={() =>
              create.mutate(
                { name: name.trim(), proxy_label: connection, note: note.trim() || null },
                {
                  onSuccess: () => {
                    toast(`${name.trim()} is ready to use`);
                    close(false);
                  },
                },
              )
            }
          >
            Add the browser
          </Button>
        </>
      }
    >
      {create.isError ? <InlineError className="mb-4">That did not work. Nothing was added.</InlineError> : null}
      <div className="grid gap-4">
        <Field label="Name" hint="Lowercase letters, numbers and dashes."><Input className="mono" value={name} onChange={(event) => setName(event.target.value.toLowerCase())} placeholder="shop-browser" autoFocus /></Field>
        <div className="grid gap-2 text-13 font-medium">
          <span>Connection</span>
          {proxies.length ? (
            <Select
              value={connection}
              onValueChange={setConnection}
              options={proxies.map((item) => ({ value: item.label, label: item.label }))}
              placeholder="Choose a connection"
              aria-label="Connection"
            />
          ) : (
            <p className="text-13 font-normal text-muted">Add a connection first. A browser needs one to reach the site.</p>
          )}
        </div>
        <Field label="Note"><Input value={note} onChange={(event) => setNote(event.target.value)} placeholder="The shop accounts" /></Field>
      </div>
    </Dialog>
  );
}

function AddConnectionDialog({ org, open, onOpenChange }: { org: string; open: boolean; onOpenChange: (open: boolean) => void }) {
  const create = useCreateProxy(org);
  const [label, setLabel] = useState('');
  const [host, setHost] = useState('');
  const [port, setPort] = useState('8080');
  const [username, setUsername] = useState('');
  const [computer, setComputer] = useState(false);
  const ready = NAME.test(label.trim()) && host.trim() && Number(port) >= 1 && Number(port) <= 65535;
  const fields = { label: label.trim(), host: host.trim(), port: String(Number(port)), username: username.trim() };
  const close = (next: boolean) => {
    if (!next) {
      setLabel('');
      setHost('');
      setPort('8080');
      setUsername('');
      setComputer(false);
    }
    onOpenChange(next);
  };
  if (computer) {
    return (
      <FormDialog
        org={org}
        open={open}
        onOpenChange={(next) => {
          if (!next) setComputer(false);
          onOpenChange(next);
        }}
        title={`Add ${fields.label}`}
        description="The password, if the connection has one, is typed on this machine. agentos Cloud never sees it."
        kind="proxy"
        fields={fields}
        confirmLabel="Continue on this machine"
        doneWords="Connection added on this machine."
      />
    );
  }
  return (
    <Dialog
      open={open}
      onOpenChange={close}
      title="Add a connection"
      description="How a browser reaches the internet. Its address is kept on the machine."
      footer={
        <>
          <Button onClick={() => close(false)}>Cancel</Button>
          <Button disabled={!ready} onClick={() => setComputer(true)}>It needs a password</Button>
          <Button
            variant="primary"
            disabled={!ready}
            loading={create.isPending}
            onClick={() => create.mutate({ ...fields, port: Number(port), username: fields.username || null }, { onSuccess: () => close(false) })}
          >
            Add the connection
          </Button>
        </>
      }
    >
      {create.isError ? <InlineError className="mb-4">That did not work. Nothing was added.</InlineError> : null}
      <div className="grid gap-4">
        <Field label="Name"><Input className="mono" value={label} onChange={(event) => setLabel(event.target.value.toLowerCase())} placeholder="de1" autoFocus /></Field>
        <div className="grid gap-4 sm:grid-cols-[minmax(0,1fr)_112px]">
          <Field label="Address"><Input className="mono" value={host} onChange={(event) => setHost(event.target.value)} placeholder="203.0.113.10" /></Field>
          <Field label="Port"><Input className="mono" inputMode="numeric" value={port} onChange={(event) => setPort(event.target.value)} /></Field>
        </div>
        <Field label="User name (optional)"><Input value={username} onChange={(event) => setUsername(event.target.value)} /></Field>
        <p className="text-12 text-faint">If it needs a password, use "It needs a password". The password is typed on this machine.</p>
      </div>
    </Dialog>
  );
}

function MoveDialog({ org, lane, from, devices, onClose }: { org: string; lane: string; from: string; devices: BrowserDevice[]; onClose: () => void }) {
  const ws = lane.split('/')[0] ?? '';
  const name = lane.slice(ws.length + 1);
  const [target, setTarget] = useState(from);
  const plan = useDeviceMovePlan(org, ws, name, target === 'own' ? null : target);
  const move = useMoveLaneDevice(org, ws, name);
  const [confirmed, setConfirmed] = useState(false);
  const options = [{ value: 'own', label: 'Its own browser' }, ...devices.map((item) => ({ value: item.name, label: item.name }))];
  const needsConfirm = Boolean(plan.data?.confirm) && !confirmed;
  return (
    <Dialog
      open
      onOpenChange={(next) => !next && onClose()}
      title={`Move ${lane}`}
      description="The account signs in from the browser you choose. Its sign-in stays where it is."
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button
            variant="primary"
            disabled={target === from || needsConfirm || !plan.data}
            loading={move.isPending}
            onClick={() => move.mutate({ device: target === 'own' ? null : target, confirm: confirmed }, { onSuccess: onClose })}
          >
            Move the account
          </Button>
        </>
      }
    >
      {move.isError ? <InlineError className="mb-4">That did not work. Nothing moved.</InlineError> : null}
      <div className="grid gap-4">
        <div className="grid gap-2 text-13 font-medium">
          <span>Browser</span>
          <Select value={target} onValueChange={(value) => { setTarget(value); setConfirmed(false); }} options={options} aria-label="Browser" />
        </div>
        {plan.data ? (
          <div className="grid gap-2 text-14 text-muted" aria-live="polite">
            {plan.data.changes.map((line) => (
              <p key={line}>{line}</p>
            ))}
            {plan.data.warnings.map((line) => (
              <p key={line} className="text-amber">{line}</p>
            ))}
          </div>
        ) : null}
        {plan.data?.confirm ? (
          <label className="flex items-start gap-3 text-14">
            <input type="checkbox" className="mt-1" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} />
            The site may ask to check the account. Move it anyway.
          </label>
        ) : null}
      </div>
    </Dialog>
  );
}

function DeviceRow({ org, device, proxies, canManage, onMove }: { org: string; device: BrowserDevice; proxies: Proxy[] | undefined; canManage: boolean; onMove: (lane: string) => void }) {
  const [technical] = useTechnical();
  const remove = useRemoveBrowserDevice(org);
  const [removing, setRemoving] = useState(false);
  const address = addressOf(proxies, device.proxy);
  const status = device.pinned ? { label: 'Set up', tone: 'ok' as const } : { label: 'Not used yet', tone: 'idle' as const };
  return (
    <li className="grid gap-3 px-4 py-4 sm:px-6">
      <div className="flex min-w-0 items-center gap-3">
        <div className="min-w-0 flex-1">
          <p className="mono truncate text-14">{device.name}</p>
          <p className="truncate text-13 text-muted">
            {device.proxy ? `Connection ${device.proxy}` : 'No connection'} · {plural(device.lanes.length, 'account')}
            {device.note ? ` · ${device.note}` : ''}
          </p>
          {technical && address ? <p className="mono truncate text-12 text-faint">{address}</p> : null}
        </div>
        <Status status={status} className="shrink-0" />
        {canManage && device.lanes.length === 0 ? (
          <IconButton label={`Remove ${device.name}`} onClick={() => setRemoving(true)}>
            <Trash2 className="size-4" />
          </IconButton>
        ) : null}
      </div>
      {device.lanes.length ? (
        <ul className="flex flex-wrap gap-2">
          {device.lanes.map((lane) => (
            <li key={lane.id} className="flex items-center gap-1 rounded-md bg-fill-1 py-1 pr-1 pl-2">
              <span className="mono text-13">{lane.id}</span>
              {canManage ? (
                <Button size="sm" variant="ghost" onClick={() => onMove(lane.id)} aria-label={`Move ${lane.id}`}>
                  Move
                </Button>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
      <ConfirmDialog
        open={removing}
        onOpenChange={setRemoving}
        title={`Remove ${device.name}?`}
        description="The browser's fingerprint is kept for a while in case you need it back. No account uses it now."
        confirmLabel="Remove the browser"
        danger
        loading={remove.isPending}
        onConfirm={() => remove.mutate(device.name, { onSuccess: () => setRemoving(false) })}
      />
    </li>
  );
}

export function BrowsersTab({ org }: { org: string }) {
  const devices = useBrowserDevices(org);
  const proxies = useProxies(org);
  const access = useAccess(org);
  const canManage = access.can('browsers.manage');
  const [adding, setAdding] = useState(false);
  const [connecting, setConnecting] = useState(false);
  const [moving, setMoving] = useState<{ lane: string; from: string } | null>(null);
  // The cloud does not keep browsers or their connections: the machine does, so the tab says where.
  if (isNotServed(devices.error)) return <Card className="p-4 sm:p-6"><NotLive /></Card>;

  return (
    <div className="grid gap-10">
      <Card className="grid gap-4 p-4 sm:p-6">
        <div className="flex items-end justify-between gap-4">
          <div className="min-w-0">
            <h2 className="text-16 font-semibold">{THINGS.browser.word}</h2>
            <p className="mt-1 text-13 text-muted">Each browser keeps one fingerprint and one connection. The accounts in it share both.</p>
          </div>
          {canManage && devices.isSuccess ? (
            <Button icon={<Plus className="size-4" />} onClick={() => setAdding(true)} disabled={!proxies.data?.length}>
              Add a browser
            </Button>
          ) : null}
        </div>
        <QueryView
          query={devices}
          skeleton={
            <div className="grid gap-4">
              <Skeleton />
              <Skeleton className="w-2/3" />
            </div>
          }
        >
          {(items) =>
            items.length ? (
              <ul className="-mx-4 divide-y divide-line sm:-mx-6">
                {items.map((device) => (
                  <DeviceRow key={device.name} org={org} device={device} proxies={proxies.data} canManage={canManage} onMove={(lane) => setMoving({ lane, from: device.name })} />
                ))}
              </ul>
            ) : (
              <EmptyState
                icon={<Globe />}
                title="No browsers yet"
                description={
                  !canManage
                    ? 'The owner sets up browsers. Accounts sign in from them.'
                    : proxies.data?.length
                      ? 'Add a browser, then put accounts in it. Each one signs in from its own browser.'
                      : 'Add a connection below first. A browser reaches the site through one.'
                }
                action={canManage && proxies.data?.length ? <Button variant="primary" onClick={() => setAdding(true)}>Add a browser</Button> : undefined}
              />
            )
          }
        </QueryView>
      </Card>

      <section className="grid gap-4">
        <div className="flex items-end justify-between gap-4">
          <div className="min-w-0">
            <h2 className="text-16 font-semibold">{THINGS.connection.word}</h2>
            <p className="mt-1 text-13 text-muted">How browsers reach the internet. An address stays behind Technical details.</p>
          </div>
          {canManage ? (
            <Button onClick={() => setConnecting(true)}>Add a connection</Button>
          ) : null}
        </div>
        <Card className="overflow-hidden">
          <QueryView
            query={proxies}
            skeleton={<Skeleton className="m-6 h-16" />}
          >
            {(items) =>
              items.length ? (
                <ul className="divide-y divide-line">
                  {items.map((proxy) => (
                    <ConnectionRow key={proxy.label} proxy={proxy} devices={devices.data ?? []} />
                  ))}
                </ul>
              ) : (
                <EmptyState title="No connections yet" description="A browser reaches the site through a connection. Add one to start a browser." />
              )
            }
          </QueryView>
        </Card>
      </section>

      <AddBrowserDialog org={org} open={adding} onOpenChange={setAdding} proxies={proxies.data ?? []} />
      <AddConnectionDialog org={org} open={connecting} onOpenChange={setConnecting} />
      {moving ? <MoveDialog org={org} lane={moving.lane} from={moving.from} devices={devices.data ?? []} onClose={() => setMoving(null)} /> : null}
    </div>
  );
}

function ConnectionRow({ proxy, devices }: { proxy: Proxy; devices: BrowserDevice[] }) {
  const [technical] = useTechnical();
  const using = devices.filter((device) => device.proxy === proxy.label).length;
  return (
    <li className="flex min-w-0 items-center gap-3 px-4 py-4 sm:px-6">
      <div className="min-w-0 flex-1">
        <p className="mono truncate text-14">{proxy.label}</p>
        <p className="truncate text-13 text-muted">{using ? `Used by ${plural(using, 'browser')}` : 'Not used by a browser yet'}</p>
        {technical && proxy.host ? <p className="mono truncate text-12 text-faint">{`${proxy.host}${proxy.port ? `:${proxy.port}` : ''}`}</p> : null}
      </div>
    </li>
  );
}
