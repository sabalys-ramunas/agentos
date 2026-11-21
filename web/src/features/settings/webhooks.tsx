import { InlineError } from '@/components/ui/states';
import { useParams } from '@tanstack/react-router';
import { MoreHorizontal, Plus, Webhook as WebhookIcon } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';
import {
  useAccess,
  useCreateWebhook,
  useDeleteWebhook,
  useRotateWebhook,
  useTestWebhook,
  useUpdateWebhook,
  useWebhookDeliveries,
  useWebhooks,
} from '@/api/hooks';
import { isCancelled, isNotServed } from '@/api/http';
import type { Webhook, WebhookDelivery, WebhookEvent } from '@/api/types';
import { Button, IconButton } from '@/components/ui/button';
import { Card, FormCard } from '@/components/ui/card';
import { ConfirmDialog, Dialog, Drawer } from '@/components/ui/dialog';
import { Field, Input } from '@/components/ui/field';
import { Menu, MenuContent, MenuItem, MenuSeparator, MenuTrigger } from '@/components/ui/menu';
import { CodeBlock } from '@/components/ui/misc';
import { Status } from '@/components/ui/status';
import { CheckCard } from '@/components/ui/choice';
import { EmptyState, QueryView, Skeleton } from '@/components/ui/states';
import { Switch } from '@/components/ui/toggle';
import { mainCell, Table, Td, Th, Tr } from '@/components/ui/table';
import { cn } from '@/lib/cn';
import { describeError } from '@/lib/errors';
import { dateTime, timeAgo } from '@/lib/format';
import { deliveryState, webhookEvent } from '@/lib/labels';
import { SettingsHeader } from './page';

const EVENTS: WebhookEvent[] = ['approval.requested', 'approval.decided', 'approval.expired', 'run.failed'];

function eventsLine(events: WebhookEvent[]) {
  if (events.length === EVENTS.length) return 'Every event';
  return events.map((event) => webhookEvent[event].label).join(', ');
}

function EventPicker({ value, onChange }: { value: WebhookEvent[]; onChange: (next: WebhookEvent[]) => void }) {
  return (
    <fieldset className="grid gap-2">
      <legend className="mb-2 text-label text-fg">Send these events</legend>
      {EVENTS.map((event) => (
        <CheckCard
          key={event}
          className="p-3"
          checked={value.includes(event)}
          onCheckedChange={(checked) => onChange(checked ? [...value, event] : value.filter((item) => item !== event))}
          title={webhookEvent[event].label}
          description={webhookEvent[event].hint}
        />
      ))}
    </fieldset>
  );
}

/** A secret is shown once, here, and never again. */
function SecretDialog({ secret, rotated, onClose }: { secret: string | null; rotated: boolean; onClose: () => void }) {
  return (
    <Dialog
      open={secret !== null}
      onOpenChange={(open) => !open && onClose()}
      title={rotated ? 'Copy the new signing secret' : 'Copy the signing secret'}
      description="You will not see this again. openajar keeps it sealed and never shows it back, so store it where your receiver can read it."
      footer={
        <Button variant="primary" onClick={onClose}>
          Done
        </Button>
      }
    >
      <div className="grid gap-4">
        <CodeBlock code={secret ?? ''} label="Copy signing secret" />
        {rotated ? <p className="text-secondary text-muted">The old secret stopped working just now. Update your receiver before the next delivery.</p> : null}
      </div>
    </Dialog>
  );
}

function AddDialog({ org, open, onOpenChange, onCreated }: { org: string; open: boolean; onOpenChange: (open: boolean) => void; onCreated: (secret: string) => void }) {
  const create = useCreateWebhook(org);
  const [url, setUrl] = useState('');
  const [events, setEvents] = useState<WebhookEvent[]>(EVENTS);
  const close = (next: boolean) => {
    onOpenChange(next);
    if (!next) {
      setUrl('');
      setEvents(EVENTS);
      create.reset();
    }
  };
  const submit = (event: React.FormEvent) => {
    event.preventDefault();
    create.mutate(
      { url: url.trim(), events },
      {
        onSuccess: (hook) => {
          close(false);
          onCreated(hook.secret);
        },
      },
    );
  };
  const error = create.isError && !isCancelled(create.error) ? describeError(create.error) : null;
  return (
    <Dialog
      open={open}
      onOpenChange={close}
      title="Add a webhook"
      description="openajar sends a signed POST to this address. Only https on port 443 or 8443, and never to a private address."
      footer={
        <>
          <Button onClick={() => close(false)}>Cancel</Button>
          <Button variant="primary" type="submit" form="add-webhook" disabled={!url.trim() || !events.length} loading={create.isPending}>
            Add webhook
          </Button>
        </>
      }
    >
      <form id="add-webhook" className="grid gap-6" onSubmit={submit}>
        <Field label="Address" error={error}>
          <Input
            mono
            autoFocus
            type="url"
            inputMode="url"
            autoComplete="off"
            spellCheck={false}
            placeholder="https://hooks.example.com/openajar"
            value={url}
            onChange={(event) => setUrl(event.target.value)}
          />
        </Field>
        <EventPicker value={events} onChange={setEvents} />
      </form>
    </Dialog>
  );
}

/** The last deliveries as a row of marks, oldest on the left, so a run of failures shows at a glance. */
function DeliveryStrip({ items }: { items: WebhookDelivery[] }) {
  const shown = items.slice(0, 30).reverse();
  if (!shown.length) return null;
  const ok = items.filter((item) => item.state === 'delivered').length;
  return (
    <figure className="grid gap-2">
      <div className="flex h-6 items-end gap-1" aria-hidden="true">
        {shown.map((item) => (
          <span
            key={item.id}
            title={`${deliveryState[item.state].label} · ${dateTime(item.created_at)}`}
            className={cn(
              'w-2 rounded-sm',
              item.state === 'delivered' ? 'h-full bg-fill-strong' : item.state === 'failed' ? 'h-full bg-bad' : 'h-1/2 border border-line-strong',
            )}
          />
        ))}
      </div>
      <figcaption className="text-secondary text-muted">
        {ok} of {items.length} delivered
      </figcaption>
    </figure>
  );
}

function DeliveriesDrawer({ org, hook, onOpenChange }: { org: string; hook: Webhook | null; onOpenChange: (open: boolean) => void }) {
  const deliveries = useWebhookDeliveries(org, hook?.id ?? null);
  return (
    <Drawer open={hook !== null} onOpenChange={onOpenChange} title="Recent deliveries" description={hook?.origin}>
      <QueryView
        query={deliveries}
        skeleton={
          <div className="grid gap-4">
            <Skeleton className="h-6" />
            <Skeleton />
            <Skeleton />
          </div>
        }
      >
        {(items) =>
          items.length ? (
            <div className="grid gap-6">
              <DeliveryStrip items={items} />
              <ul className="divide-y divide-line border-t border-line">
                {items.map((item) => (
                  <li key={item.id} className="grid gap-1 py-3">
                    <div className="flex items-center justify-between gap-4">
                      <span className="text-body">{webhookEvent[item.kind].label}</span>
                      <Status status={deliveryState[item.state]} />
                    </div>
                    <p className="mono tabular text-caption text-faint">
                      {dateTime(item.created_at)} · {item.attempts === 1 ? '1 try' : `${item.attempts} tries`}
                      {item.status !== null ? ` · HTTP ${item.status}` : ''}
                    </p>
                    {item.error ? <p className="text-secondary break-words text-muted">{item.error}</p> : null}
                    {item.state === 'pending' && item.next_attempt_at ? (
                      <p className="text-secondary text-muted">Next try {timeAgo(item.next_attempt_at).toLowerCase() === 'just now' ? 'any moment' : `at ${dateTime(item.next_attempt_at)}`}.</p>
                    ) : null}
                  </li>
                ))}
              </ul>
            </div>
          ) : (
            <EmptyState icon={<WebhookIcon />} title="Nothing sent yet" description="Send a test, or wait for the next event you chose." className="py-10" />
          )
        }
      </QueryView>
    </Drawer>
  );
}

function EventsDialog({ org, hook, onOpenChange }: { org: string; hook: Webhook | null; onOpenChange: (open: boolean) => void }) {
  const update = useUpdateWebhook(org);
  const [events, setEvents] = useState<WebhookEvent[]>(hook?.events ?? EVENTS);
  const [seen, setSeen] = useState(hook?.id);
  if (hook && hook.id !== seen) {
    setSeen(hook.id);
    setEvents(hook.events);
  }
  return (
    <Dialog
      open={hook !== null}
      onOpenChange={onOpenChange}
      title="Choose events"
      description={hook?.origin}
      footer={
        <>
          <Button onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button
            variant="primary"
            disabled={!events.length}
            loading={update.isPending}
            onClick={() => hook && update.mutate({ id: hook.id, events }, { onSuccess: () => onOpenChange(false) })}
          >
            Save events
          </Button>
        </>
      }
    >
      <EventPicker value={events} onChange={setEvents} />
    </Dialog>
  );
}

function Signature() {
  return (
    <FormCard
      title="Checking a delivery"
      description="Every delivery is a POST with a JSON body { type, at, data }. Check it before you trust it."
      footnote="Refuse a timestamp more than five minutes old, so an old delivery cannot be sent again."
    >
      <CodeBlock
        label="Copy the signature rule"
        code={'X-Agentos-Signature: sha256=HMAC_SHA256(secret, X-Agentos-Timestamp + "." + body)'}
      />
      <p className="text-secondary text-muted">
        Also sent: <span className="mono">X-Agentos-Event</span> and <span className="mono">X-Agentos-Delivery</span>, which stays the same when a
        delivery is tried again. Redirects are never followed.
      </p>
    </FormCard>
  );
}

export function WebhooksSettings() {
  const { org } = useParams({ from: '/$org/settings/webhooks' });
  const access = useAccess(org);
  const canManage = access.can('webhooks.manage');
  const hooks = useWebhooks(org);
  const update = useUpdateWebhook(org);
  const remove = useDeleteWebhook(org);
  const rotate = useRotateWebhook(org);
  const test = useTestWebhook(org);
  const [adding, setAdding] = useState(false);
  const [secret, setSecret] = useState<{ value: string; rotated: boolean } | null>(null);
  const [logOf, setLogOf] = useState<Webhook | null>(null);
  const [editing, setEditing] = useState<Webhook | null>(null);
  const [rotating, setRotating] = useState<Webhook | null>(null);
  const [removing, setRemoving] = useState<Webhook | null>(null);

  const sendTest = (hook: Webhook) =>
    test.mutate(hook.id, {
      onSuccess: () => {
        toast('Test sent. It shows in the log in a moment.');
        setLogOf(hook);
      },
    });

  return (
    <>
      <SettingsHeader
        title="Webhooks"
        description="Signed messages to a server you run when something needs you or a run fails. Yours alone: nobody else in the org sees them."
        action={
          canManage && !isNotServed(hooks.error) && hooks.data?.length ? (
            <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setAdding(true)}>
              Add webhook
            </Button>
          ) : undefined
        }
      />
      <div className="grid gap-4">
        <Card className="overflow-hidden">
          <QueryView
            query={hooks}
            skeleton={
              <div className="grid gap-4 p-6">
                <Skeleton />
                <Skeleton className="w-2/3" />
              </div>
            }
          >
            {(items) =>
              items.length ? (
                <Table>
                  <caption className="sr-only">Your webhooks</caption>
                  <thead className="hidden md:table-header-group">
                    <tr>
                      <Th>Address</Th>
                      <Th className="w-36">Last delivery</Th>
                      <Th className="w-16">On</Th>
                      <Th className="w-12">
                        <span className="sr-only">Actions</span>
                      </Th>
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((hook) => {
                      const last = hook.last_delivery;
                      return (
                        <Tr key={hook.id}>
                          <Td className={mainCell}>
                            <p className="mono truncate text-code text-fg" title={hook.origin}>
                              {hook.origin}
                            </p>
                            <p className="truncate text-secondary text-faint">{eventsLine(hook.events)}</p>
                            <p className="truncate text-secondary text-faint md:hidden">
                              {last ? `${deliveryState[last.state].label} ${timeAgo(last.created_at).toLowerCase()}` : 'Nothing sent yet'}
                            </p>
                          </Td>
                          <Td className="hidden md:table-cell">
                            {last ? (
                              <button
                                type="button"
                                className="-mx-2 grid gap-1 rounded-md px-2 py-1 text-left hover:bg-hover"
                                onClick={() => setLogOf(hook)}
                                aria-label={`Deliveries to ${hook.origin}`}
                              >
                                <Status status={deliveryState[last.state]} />
                                <span className="text-caption text-faint">{timeAgo(last.created_at)}</span>
                              </button>
                            ) : (
                              <span className="text-secondary text-faint">Nothing sent yet</span>
                            )}
                          </Td>
                          <Td>
                            <Switch
                              aria-label={`Send to ${hook.origin}`}
                              checked={hook.enabled}
                              disabled={!canManage}
                              onCheckedChange={(enabled) => update.mutate({ id: hook.id, enabled })}
                            />
                          </Td>
                          <Td className="pr-4 md:pr-6">
                            <Menu>
                              <MenuTrigger asChild>
                                <IconButton label={`Actions for ${hook.origin}`} size="sm">
                                  <MoreHorizontal className="size-4" />
                                </IconButton>
                              </MenuTrigger>
                              <MenuContent align="end">
                                {canManage ? <MenuItem onSelect={() => sendTest(hook)}>Send a test</MenuItem> : null}
                                <MenuItem onSelect={() => setLogOf(hook)}>Recent deliveries</MenuItem>
                                {canManage ? <MenuItem onSelect={() => setEditing(hook)}>Choose events</MenuItem> : null}
                                {canManage ? <MenuItem onSelect={() => setRotating(hook)}>Rotate secret</MenuItem> : null}
                                <MenuSeparator />
                                <MenuItem className="text-bad" onSelect={() => setRemoving(hook)}>
                                  Remove
                                </MenuItem>
                              </MenuContent>
                            </Menu>
                          </Td>
                        </Tr>
                      );
                    })}
                  </tbody>
                </Table>
              ) : (
                <EmptyState
                  icon={<WebhookIcon />}
                  title="No webhooks yet"
                  description={
                    canManage
                      ? 'Send approvals and failed runs to a server you run, signed so it can tell they came from openajar.'
                      : 'Webhooks are for members who operate lanes. An admin can change your role.'
                  }
                  action={
                    canManage ? (
                      <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setAdding(true)}>
                        Add webhook
                      </Button>
                    ) : undefined
                  }
                />
              )
            }
          </QueryView>
        </Card>
        {update.isError && !isCancelled(update.error) ? (
          <InlineError>{describeError(update.error)} It is back as it was.</InlineError>
        ) : null}
        <Signature />
      </div>
      <AddDialog org={org} open={adding} onOpenChange={setAdding} onCreated={(value) => setSecret({ value, rotated: false })} />
      <SecretDialog secret={secret?.value ?? null} rotated={secret?.rotated ?? false} onClose={() => setSecret(null)} />
      <DeliveriesDrawer org={org} hook={logOf} onOpenChange={(open) => !open && setLogOf(null)} />
      <EventsDialog org={org} hook={editing} onOpenChange={(open) => !open && setEditing(null)} />
      <ConfirmDialog
        open={rotating !== null}
        onOpenChange={(open) => !open && setRotating(null)}
        title="Rotate the signing secret?"
        description="The old secret stops working at once. Every delivery after this is signed with the new one, so update your receiver straight away."
        confirmLabel="Rotate secret"
        loading={rotate.isPending}
        onConfirm={() =>
          rotating &&
          rotate.mutate(rotating.id, {
            onSuccess: ({ secret: value }) => {
              setRotating(null);
              setSecret({ value, rotated: true });
            },
            onError: (error) => {
              if (!isCancelled(error)) toast.error(describeError(error));
            },
          })
        }
      />
      <ConfirmDialog
        open={removing !== null}
        onOpenChange={(open) => !open && setRemoving(null)}
        title={`Remove ${removing?.origin ?? 'this webhook'}?`}
        description="Nothing more is sent to it, including deliveries still waiting to be tried again."
        confirmLabel="Remove webhook"
        danger
        loading={remove.isPending}
        onConfirm={() =>
          removing &&
          remove.mutate(removing.id, {
            onSuccess: () => {
              setRemoving(null);
              toast('Webhook removed');
            },
          })
        }
      />
    </>
  );
}
