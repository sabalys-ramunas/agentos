import { Link, useParams } from '@tanstack/react-router';
import { useRunsAlone } from '@/features/nodes/add-node';
import { useEffect, useState } from 'react';
import { useAccess, useNotifications, usePushConfig, useProviders, useSaveNotifications, useTestNotification, useWebhooks } from '@/api/hooks';
import type { NotificationSettings, QuietHours, Webhook, WebhookEvent } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Card, FormCard } from '@/components/ui/card';
import { Input, Label } from '@/components/ui/field';
import { SaveStatus, SettingRow } from '@/components/ui/form';
import { AppTile } from '@/components/ui/tile';
import { Mail, Moon } from 'lucide-react';
import { Select } from '@/components/ui/select';
import { QueryView, Skeleton } from '@/components/ui/states';
import { Switch } from '@/components/ui/toggle';
import { cn } from '@/lib/cn';
import { describeError } from '@/lib/errors';
import { DigestPreview } from './digest-preview';
import { SettingsHeader } from './page';
import { PushCard } from './push-device';

const BATCH = [
  { value: '0', label: 'At once' },
  { value: '5', label: '5 minutes' },
  { value: '15', label: '15 minutes' },
  { value: '30', label: '30 minutes' },
  { value: '60', label: '1 hour' },
];

const ZONES = [
  'UTC',
  'Europe/London',
  'Europe/Berlin',
  'Europe/Vilnius',
  'America/New_York',
  'America/Chicago',
  'America/Los_Angeles',
  'Asia/Singapore',
  'Asia/Tokyo',
  'Australia/Sydney',
];

const browserZone = () => Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';

const CLOCK = /^([01]\d|2[0-3]):[0-5]\d$/;
const minutesOf = (clock: string) => Number(clock.slice(0, 2)) * 60 + Number(clock.slice(3, 5));

/** Now, as a clock in the given zone. */
function clockIn(zone: string, at = new Date()): string {
  try {
    return at.toLocaleTimeString('en-GB', { timeZone: zone, hour: '2-digit', minute: '2-digit', hour12: false });
  } catch {
    return at.toISOString().slice(11, 16);
  }
}

/**
 * The day as one bar, with the quiet window shaded and a tick at the current time in that zone.
 * A window that crosses midnight is drawn as two pieces, which is what it is.
 */
function QuietStrip({ quiet }: { quiet: QuietHours }) {
  const from = minutesOf(quiet.from);
  const to = minutesOf(quiet.to);
  const day = 24 * 60;
  const pieces = from === to ? [] : from < to ? [[from, to]] : [[from, day], [0, to]];
  const now = minutesOf(clockIn(quiet.timezone));
  const inside = pieces.some(([start = 0, end = 0]) => now >= start && now < end);
  return (
    <figure className="grid gap-2">
      <div className="relative h-6 overflow-hidden rounded-md border border-line bg-inset" aria-hidden="true">
        {pieces.map(([start = 0, end = 0]) => (
          <span
            key={start}
            className="absolute inset-y-0 border-x border-accent-line bg-accent-soft transition-[left,width] duration-(--duration-base) ease-standard"
            style={{ left: `${(start / day) * 100}%`, width: `${((end - start) / day) * 100}%` }}
          />
        ))}
        <span className="absolute inset-y-0 w-px bg-fg" style={{ left: `${(now / day) * 100}%` }} />
      </div>
      <div className="mono tabular flex justify-between text-code-caption text-faint" aria-hidden="true">
        <span>00:00</span>
        <span>06:00</span>
        <span>12:00</span>
        <span>18:00</span>
        <span>24:00</span>
      </div>
      <figcaption className="text-secondary text-muted">
        It is {clockIn(quiet.timezone)} in {quiet.timezone.replaceAll('_', ' ')}, so quiet hours are {inside ? 'on now' : 'not on now'}.
      </figcaption>
    </figure>
  );
}

/** A time field that saves once a whole time is in, and never mid-typing. */
function ClockField({ id, label, value, onCommit }: { id: string; label: string; value: string; onCommit: (value: string) => void }) {
  const [draft, setDraft] = useState(value);
  useEffect(() => setDraft(value), [value]);
  return (
    <div className="grid gap-2">
      <Label htmlFor={id}>{label}</Label>
      <Input
        id={id}
        type="time"
        className="mono tabular"
        value={draft}
        onChange={(event) => {
          setDraft(event.target.value);
          if (CLOCK.test(event.target.value) && event.target.value !== value) onCommit(event.target.value);
        }}
        onBlur={() => setDraft(value)}
      />
    </div>
  );
}

type Cell = { text: string; off?: boolean };

function webhookCell(hooks: Webhook[], events: WebhookEvent[]): Cell {
  const count = hooks.filter((hook) => hook.enabled && hook.events.some((event) => events.includes(event))).length;
  return count ? { text: count === 1 ? '1 webhook' : `${count} webhooks` } : { text: 'None', off: true };
}

/** What each kind of event does, worked out from the settings and webhooks as they are now. */
function routes(value: NotificationSettings, hooks: Webhook[], pushable = true): { event: string; detail: string; push: Cell; email: Cell; webhook: Cell }[] {
  // The saved choice can be on while this server cannot push at all. Then nothing is pushed.
  const push = value.push && pushable;
  const digest: Cell = value.email_digest === 'off' ? { text: 'Off', off: true } : { text: `${value.email_digest === 'daily' ? 'Daily' : 'Weekly'} digest` };
  const none: Cell = { text: String.fromCharCode(0x2013), off: true };
  const quietNote = value.quiet_hours.enabled && value.quiet_hours.urgent_breaks_through ? ', even in quiet hours' : '';
  return [
    {
      event: 'Payments and irreversible actions',
      detail: 'Urgent. Never grouped or held.',
      push: push ? { text: `At once${quietNote}` } : { text: 'Off', off: true },
      email: value.email_urgent ? { text: 'At once' } : { text: 'Off', off: true },
      webhook: webhookCell(hooks, ['approval.requested']),
    },
    {
      event: 'Other approvals and questions',
      detail: 'Publishing, logins, commands and more.',
      push: push ? { text: value.batch_minutes ? `Grouped, within ${value.batch_minutes} min` : 'At once' } : { text: 'Off', off: true },
      email: digest,
      webhook: webhookCell(hooks, ['approval.requested']),
    },
    {
      event: 'Decided or expired',
      detail: 'A push already sent is taken back.',
      push: none,
      email: none,
      webhook: webhookCell(hooks, ['approval.decided', 'approval.expired']),
    },
    { event: 'Failed runs', detail: 'Runs that failed or only partly worked.', push: none, email: digest, webhook: webhookCell(hooks, ['run.failed']) },
  ];
}

function Value({ cell }: { cell: Cell }) {
  return <span className={cn('text-secondary', cell.off ? 'text-faint' : 'text-fg')}>{cell.text}</span>;
}

function WhereThingsGo({ org, value, pushable, emailable }: { org: string; value: NotificationSettings; pushable: boolean; emailable: boolean }) {
  const canHook = useAccess(org).can('webhooks.manage');
  const hooks = useWebhooks(org).data ?? [];
  const rows = routes(value, hooks, pushable);
  return (
    <Card>
      <div className="px-6 pt-5 pb-4">
        <h3 className="text-section-title">Where things go</h3>
        <p className="mt-1 text-secondary text-muted">Worked out from your choices on this page. Whatever is off, approvals still wait in Needs you.</p>
      </div>
      {/* A real table from a tablet up. On a phone each event is a short list instead. */}
      <table className="hidden w-full border-t border-line text-left md:table">
        <caption className="sr-only">Where each kind of event goes</caption>
        <thead>
          <tr className="h-10 border-b border-line text-table-header text-faint">
            <th scope="col" className="pl-6 font-normal">
              Event
            </th>
            <th scope="col" className="font-normal">
              Push
            </th>
            {emailable ? (
              <th scope="col" className="font-normal">
                Email
              </th>
            ) : null}
            <th scope="col" className="pr-6 font-normal">
              {canHook ? (
                <Link to="/$org/settings/webhooks" params={{ org }} className="hover:text-fg">
                  Webhooks
                </Link>
              ) : (
                'Webhooks'
              )}
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.event} className="border-b border-line last:border-b-0">
              <th scope="row" className="py-3 pr-4 pl-6 align-top font-normal">
                <span className="block text-body">{row.event}</span>
                <span className="block text-secondary text-faint">{row.detail}</span>
              </th>
              <td className="py-3 pr-4 align-top">
                <Value cell={row.push} />
              </td>
              {emailable ? (
                <td className="py-3 pr-4 align-top">
                  <Value cell={row.email} />
                </td>
              ) : null}
              <td className="py-3 pr-6 align-top">
                <Value cell={row.webhook} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <ul className="divide-y divide-line border-t border-line md:hidden">
        {rows.map((row) => (
          <li key={row.event} className="px-6 py-3">
            <p className="text-body">{row.event}</p>
            <dl className="mt-2 grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1">
              <dt className="text-secondary text-faint">Push</dt>
              <dd>
                <Value cell={row.push} />
              </dd>
              {emailable ? (
                <>
                  <dt className="text-secondary text-faint">Email</dt>
                  <dd>
                    <Value cell={row.email} />
                  </dd>
                </>
              ) : null}
              <dt className="text-secondary text-faint">Webhooks</dt>
              <dd>
                <Value cell={row.webhook} />
              </dd>
            </dl>
          </li>
        ))}
      </ul>
    </Card>
  );
}

function SaveState({ save }: { save: ReturnType<typeof useSaveNotifications> }) {
  return <SaveStatus state={save.status} error={save.isError ? `${describeError(save.error)} Your last saved choice is back.` : null} />;
}

function Form({ org, value }: { org: string; value: NotificationSettings }) {
  const save = useSaveNotifications(org);
  const test = useTestNotification(org);
  const alone = useRunsAlone(org);
  const patch = (next: Partial<NotificationSettings>) => save.mutate({ ...value, ...next });
  const quiet = (next: Partial<QuietHours>) => {
    // The API's default zone is UTC. Turning quiet hours on uses this browser's zone instead,
    // unless one was chosen before.
    const zoneless = next.enabled && value.quiet_hours.timezone === 'UTC' && browserZone() !== 'UTC';
    patch({ quiet_hours: { ...value.quiet_hours, ...(zoneless ? { timezone: browserZone() } : {}), ...next } });
  };
  const zone = value.quiet_hours.timezone;
  // Known to be impossible here (not set up, or not on the plan). Unknown while loading counts as possible.
  const config = usePushConfig(org).data;
  const pushable = config ? config.available : true;
  // A computer sends no email, so it offers no email choice at all. Only a server that says it sends mail gets them,
  // and the note about it waits until the server has said so, so a cloud never shows it while the answer loads.
  const sendsEmail = useProviders().data?.magic_link;
  const emailable = sendsEmail === true;
  const zones = [...new Set([zone, browserZone(), ...ZONES])].map((item) => ({ value: item, label: item.replaceAll('_', ' ') }));

  return (
    <div className="grid gap-4">
      <PushCard org={org} enabledInSettings={value.push} />
      <FormCard
        tile={<AppTile tone="blue" icon={Mail} size="sm" />}
        title="Channels"
        description={alone ? 'Yours alone, on this computer. Nobody else sees or changes them.' : 'Yours alone, in this organization. Nobody else sees or changes them.'}
        footnote={save.isIdle ? undefined : <SaveState save={save} />}
        action={
          emailable ? (
            <Button size="sm" loading={test.isPending} onClick={() => test.mutate()}>
              {test.isSuccess ? 'Test email sent' : 'Send a test email'}
            </Button>
          ) : undefined
        }
      >
        <div className="divide-y divide-line">
          <SettingRow
            label="Push to my devices"
            hint={pushable ? 'Turns push off everywhere at once, without removing any device.' : 'Not available here, so nothing is pushed. Approvals still wait in Needs you.'}
          >
            <Switch aria-label="Push to my devices" checked={value.push && pushable} disabled={!pushable} onCheckedChange={(push) => patch({ push })} />
          </SettingRow>
          {emailable ? (
            <>
              <SettingRow label="Urgent email" hint="Payments and irreversible actions, the moment they ask.">
                <Switch aria-label="Urgent email" checked={value.email_urgent} onCheckedChange={(email_urgent) => patch({ email_urgent })} />
              </SettingRow>
              <SettingRow label="Email digest" hint={`What your agents did, and what waits. It lands at ${value.digest?.label ?? `08:00 in ${zone.replaceAll('_', ' ')}`}.`}>
                <Select
                  className="w-32 shrink-0"
                  aria-label="Email digest"
                  value={value.email_digest}
                  onValueChange={(email_digest) => patch({ email_digest: email_digest as NotificationSettings['email_digest'] })}
                  options={[
                    { value: 'off', label: 'Off' },
                    { value: 'daily', label: 'Daily' },
                    { value: 'weekly', label: 'Weekly' },
                  ]}
                />
              </SettingRow>
            </>
          ) : null}
        </div>
        {sendsEmail === false ? <p className="text-secondary text-faint">This computer sends no email. Approvals still wait in Needs you.</p> : null}
      </FormCard>
      <WhereThingsGo org={org} value={value} pushable={pushable} emailable={emailable} />
      <FormCard
        tile={<AppTile tone="violet" icon={Moon} size="sm" />}
        title="Timing"
        description="When a push may reach you. Nothing is lost while it waits: everything stays in Needs you."
      >
        <div className="divide-y divide-line">
          <SettingRow label="Group routine approvals" hint="Several that arrive close together come as one push. Urgent ones never wait.">
            <Select
              className="w-36 shrink-0"
              aria-label="Group routine approvals"
              value={String(value.batch_minutes)}
              onValueChange={(minutes) => patch({ batch_minutes: Number(minutes) })}
              options={BATCH.some((item) => item.value === String(value.batch_minutes)) ? BATCH : [...BATCH, { value: String(value.batch_minutes), label: `${value.batch_minutes} minutes` }]}
            />
          </SettingRow>
          <SettingRow label="Quiet hours" hint="Pushes wait until the end, in the time zone below.">
            <Switch aria-label="Quiet hours" checked={value.quiet_hours.enabled} onCheckedChange={(enabled) => quiet({ enabled })} />
          </SettingRow>
          {value.quiet_hours.enabled ? (
            <div className="grid gap-4 py-4">
              <div className="grid gap-4 sm:grid-cols-[repeat(2,minmax(0,8rem))_minmax(0,1fr)]">
                <ClockField id="quiet-from" label="From" value={value.quiet_hours.from} onCommit={(from) => quiet({ from })} />
                <ClockField id="quiet-to" label="To" value={value.quiet_hours.to} onCommit={(to) => quiet({ to })} />
                <div className="grid gap-2">
                  <span id="quiet-zone" className="text-label text-fg">
                    Time zone
                  </span>
                  <Select aria-describedby="quiet-zone" aria-label="Time zone" value={zone} onValueChange={(timezone) => quiet({ timezone })} options={zones} />
                </div>
              </div>
              <QuietStrip quiet={value.quiet_hours} />
            </div>
          ) : null}
          {value.quiet_hours.enabled ? (
            <SettingRow label="Urgent comes through" hint="Payments and irreversible actions still reach you in quiet hours.">
              <Switch
                aria-label="Urgent comes through"
                checked={value.quiet_hours.urgent_breaks_through}
                onCheckedChange={(urgent_breaks_through) => quiet({ urgent_breaks_through })}
              />
            </SettingRow>
          ) : null}
        </div>
      </FormCard>
      {emailable ? <DigestPreview org={org} period={value.email_digest} timezone={zone} /> : null}
    </div>
  );
}

export function NotificationSettingsScreen() {
  const { org } = useParams({ from: '/$org/settings/notifications' });
  const query = useNotifications(org);
  return (
    <>
      <SettingsHeader title="Notifications" description="How openajar reaches you. Approvals always wait in Needs you, whatever you turn off." />
      <QueryView card query={query} skeleton={<Skeleton className="h-96 rounded-xl" />}>
        {(data) => <Form org={org} value={data} />}
      </QueryView>
    </>
  );
}
