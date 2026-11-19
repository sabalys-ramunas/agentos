/**
 * The mailboxes, as what they are: a place mail comes in on one side and goes out on the other. Each
 * row is the two wires a mailbox keeps open, the same two the connect page checks, so a broken one is
 * seen where it broke. Today's arrivals sit on the wire in, and the day's limit is the wire out.
 * Concept stage: answered by the mock only.
 */
import { Link, useParams } from '@tanstack/react-router';
import { ArrowRight, Mail, Plus, Repeat } from 'lucide-react';
import { useMemo } from 'react';
import { useLeadBatches, useMailboxes, useWorkspaces } from '@/api/hooks';
import { isNotServed } from '@/api/http';
import type { MailArrival, Mailbox, MailboxDay, Workspace } from '@/api/types';
import { BusinessMark } from '@/components/brand/identity';
import { SiteMark } from '@/components/brand/site-mark';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { StateBadge } from '@/components/ui/state-badge';
import { EmptyState, ErrorState, NotLive, Skeleton } from '@/components/ui/states';
import { cn } from '@/lib/cn';
import { clock, plural } from '@/lib/format';
import { DayGauge, EmailTabs } from './shared';
import { agoWords, HEALTH_WORD } from './words';

const count = (arrivals: MailArrival[] | null, kind: MailArrival['kind']) => (arrivals ?? []).filter((item) => item.kind === kind).length;

/** What the day holds, in words. The node keeps what was sent, not what is planned: without a plan, only sent is said. */
function sendWords(mailbox: Mailbox): string {
  const share = mailbox.sent_today / mailbox.daily_limit;
  const planned = mailbox.planned_today;
  if (planned === null) return share >= 0.8 ? 'Close to the limit' : 'Sent so far today';
  const left = mailbox.daily_limit - mailbox.sent_today - planned;
  if (share >= 0.8) return left > 0 ? `Close to the limit, room for ${left} more` : 'What is planned fills the day';
  return planned ? `${planned} more planned, plenty of room` : 'Plenty of room';
}

function Hero({ mailboxes }: { mailboxes: Mailbox[] }) {
  const fine = mailboxes.filter((item) => item.health === 'good').length;
  const needs = mailboxes.length - fine;
  const freshKnown = mailboxes.some((item) => item.new_count !== null);
  const fresh = mailboxes.reduce((sum, item) => sum + (item.new_count ?? 0), 0);
  const replies = mailboxes.reduce((sum, item) => sum + count(item.arrivals, 'reply'), 0);
  const sent = mailboxes.reduce((sum, item) => sum + item.sent_today, 0);
  return (
    <div className="min-w-0">
      <h1 className="text-headline text-balance sm:text-40">
        {plural(fine, 'mailbox is', 'mailboxes are')} fine. {needs ? <span className="text-faint">{plural(needs, 'needs', 'need')} you.</span> : null}
      </h1>
      <p className="mt-3 max-w-2xl text-lede text-pretty text-muted">
        {freshKnown ? <><strong className="font-semibold text-fg">{fresh} new</strong> today, {plural(replies, 'of them a reply', 'of them replies')}. </> : null}<strong className="font-semibold text-fg">{sent} sent</strong>, inside every daily limit.
      </p>
    </div>
  );
}

/** The wire in: today's mail, placed by when it came, and heading for the mailbox. */
function WireIn({ org, mailbox }: { org: string; mailbox: Mailbox }) {
  const ticks = useMemo(() => {
    const end = Date.now();
    const arrivals = mailbox.arrivals ?? [];
    const start = Math.min(new Date(end).setHours(0, 0, 0, 0), ...arrivals.map((item) => new Date(item.at).getTime()));
    return arrivals.map((item) => ({ ...item, left: 4 + ((new Date(item.at).getTime() - start) / (end - start)) * 88 }));
  }, [mailbox.arrivals]);
  const replies = count(mailbox.arrivals, 'reply');
  const out = count(mailbox.arrivals, 'unsubscribe');
  const broken = mailbox.health === 'needs_password' || mailbox.health === 'cannot_read';
  return (
    <div className="min-w-0">
      {broken ? (
        <div className="md:pr-6">
          <p className="text-heading text-faint">Not reading</p>
          <p className="text-secondary text-muted">New mail is safe on the server</p>
        </div>
      ) : (
        <Link to="/$org/email/new-mail" params={{ org }} search={{ mailbox: mailbox.id }} className="group block md:pr-6">
          <p className="text-heading group-hover:underline group-hover:decoration-1 group-hover:underline-offset-4">{mailbox.new_count === null ? 'Not read yet' : `${mailbox.new_count} new`}</p>
          <p className="text-secondary text-muted">{mailbox.arrivals === null ? 'Replies are not counted on this computer yet' : [replies ? plural(replies, 'reply', 'replies') : null, out ? plural(out, 'unsubscribe') : null].filter(Boolean).join(', ') || 'No replies yet today'}</p>
        </Link>
      )}
      <div className="post-wire mt-2" data-broken={broken ? '' : undefined}>
        {ticks.map((item, index) => (
          <span key={index} className="post-tick" data-kind={item.kind} title={`${item.kind === 'reply' ? 'A reply' : item.kind === 'unsubscribe' ? 'An unsubscribe' : 'Mail'} at ${clock(item.at)}`} style={{ left: `${item.left}%` }} />
        ))}
      </div>
    </div>
  );
}

function WireOut({ mailbox }: { mailbox: Mailbox }) {
  const broken = mailbox.health === 'needs_password' || mailbox.health === 'cannot_send';
  return (
    <div className="min-w-0">
      {broken ? (
        <div className="md:pl-6">
          <p className="text-heading text-faint">Not sending</p>
          <p className="text-secondary text-muted">Nothing went out, and nothing was lost</p>
        </div>
      ) : (
        <div className="md:pl-6">
          <p className="text-heading">{mailbox.sent_today} <span className="text-body text-muted">of {mailbox.daily_limit} today</span></p>
          <p className="text-secondary text-muted">{sendWords(mailbox)}</p>
        </div>
      )}
      <div className="mt-2 flex h-6 items-center">
        {broken ? (
          <div className="post-wire w-full" data-broken="" />
        ) : (
          <>
            <span aria-hidden="true" className="hidden h-px w-6 shrink-0 bg-(--board-wire-strong) md:block" />
            <DayGauge sent={mailbox.sent_today} planned={mailbox.planned_today ?? 0} limit={mailbox.daily_limit} className="min-w-0 flex-1" />
          </>
        )}
      </div>
    </div>
  );
}

function Week({ days }: { days: MailboxDay[] }) {
  const most = Math.max(1, ...days.map((day) => day.sent));
  const total = days.reduce((sum, day) => sum + day.sent, 0);
  return (
    <span className="flex shrink-0 items-end gap-3">
      <span aria-hidden="true" className="flex h-5 items-end gap-px">
        {days.map((day, index) => (
          <span
            key={day.at}
            title={`${new Date(day.at).toLocaleDateString('en', { weekday: 'long' })}: ${plural(day.sent, 'email')} sent`}
            className={cn('w-2 rounded-t-[2px]', index === days.length - 1 ? 'bg-fg' : 'bg-(--mark-cached)')}
            style={{ height: `${Math.max(1, (day.sent / most) * 20)}px` }}
          />
        ))}
      </span>
      <span className="tabular text-caption text-faint">{total} this week</span>
    </span>
  );
}

function Row({ org, mailbox, workspace }: { org: string; mailbox: Mailbox; workspace: Workspace | undefined }) {
  const good = mailbox.health === 'good';
  return (
    <li className={cn('px-4 pt-6 pb-4 md:px-6', 'border-t border-board-line first:border-t-0')}>
      <div className="grid items-center gap-4 md:grid-cols-[minmax(0,1fr)_20rem_minmax(0,1fr)] md:gap-0">
        <div className="max-md:order-2"><WireIn org={org} mailbox={mailbox} /></div>
        <Link
          to="/$org/email/new-mail"
          params={{ org }}
          search={{ mailbox: mailbox.id }}
          className={cn('group min-w-0 rounded-(--radius-card) border bg-card p-3 transition-[border-color,transform] duration-(--duration-base) ease-standard hover:border-line-strong active:translate-y-px max-md:order-1', good ? 'border-line' : 'border-(--stuck-line)')}
        >
          <span className="flex items-center gap-3">
            <SiteMark site={mailbox.site} size={32} tile label={false} className="bg-card" />
            <span className="min-w-0 flex-1">
              <span className="block truncate text-label">{mailbox.address}</span>
              <span className="flex min-w-0 items-center gap-1 text-caption text-faint">
                {workspace ? <BusinessMark name={workspace.name} color={workspace.color} size={14} /> : null}
                <span className="truncate">{workspace?.name} · {mailbox.provider ?? mailbox.site}</span>
              </span>
            </span>
          </span>
          <span className="mt-2 flex items-center justify-between gap-2 border-t border-line pt-2">
            <StateBadge state={good ? 'good' : 'stuck'} label={HEALTH_WORD[mailbox.health]} size={14} className="text-caption" />
            <span className="mono truncate text-caption text-faint">{mailbox.computer}</span>
          </span>
        </Link>
        <div className="max-md:order-3 md:pl-0"><WireOut mailbox={mailbox} /></div>
      </div>
      {mailbox.problem ? (
        <div className="mt-4 flex flex-wrap items-center justify-between gap-x-6 gap-y-2 rounded-lg bg-(--stuck-soft) px-4 py-3">
          <p className="min-w-0 flex-1 text-body text-pretty">
            <span className="font-medium">{mailbox.problem.title} {agoWords(mailbox.problem.since)}.</span> <span className="text-muted">{mailbox.problem.body}</span>
          </p>
          <Button variant="primary" asChild>
            <Link to="/$org/email/connect" params={{ org }} search={{ address: mailbox.address, stage: 'found' }}>{mailbox.problem.next}</Link>
          </Button>
        </div>
      ) : null}
      <div className="mt-4 flex flex-wrap items-center justify-between gap-x-6 gap-y-2">
        <ul className="flex min-w-0 flex-wrap items-center gap-x-4 gap-y-1 text-secondary">
          {(mailbox.routines ?? []).map((routine) => (
            <li key={routine.id} className="flex min-w-0 items-center gap-1 text-muted">
              <Repeat aria-hidden="true" className="size-3 shrink-0 text-faint" />
              {routine.id === 'follow-up' ? (
                <Link to="/$org/email/routines/$routine" params={{ org, routine: routine.id }} className="truncate hover:text-fg hover:underline hover:underline-offset-4">{routine.name}</Link>
              ) : (
                <span className="truncate">{routine.name}</span>
              )}
            </li>
          ))}
        </ul>
        {mailbox.week ? <Week days={mailbox.week} /> : null}
      </div>
    </li>
  );
}

function Loading() {
  return (
    <div className="grid gap-8">
      <Skeleton className="h-24 max-w-2xl rounded-lg" />
      {[0, 1, 2].map((item) => <Skeleton key={item} className="h-44 rounded-(--radius-card)" />)}
    </div>
  );
}

export function Mailboxes() {
  const { org } = useParams({ from: '/$org/email' });
  const mailboxes = useMailboxes(org);
  const batches = useLeadBatches(org).data;
  const workspaces = useWorkspaces(org).data;
  const connect = (
    <Button icon={<Plus aria-hidden="true" className="size-4" />} asChild>
      <Link to="/$org/email/connect" params={{ org }}>Connect a mailbox</Link>
    </Button>
  );

  if (isNotServed(mailboxes.error)) return <Card><NotLive /></Card>;
  if (mailboxes.error) return <ErrorState error={mailboxes.error} onRetry={() => void mailboxes.refetch()} />;
  if (!mailboxes.data) return <Loading />;

  const all = mailboxes.data;
  if (all.length === 0) {
    return (
      <div className="grid gap-8">
        <h1 className="text-page-title-compact">Email</h1>
        <Card>
          <EmptyState
            icon={<Mail />}
            title="Connect a mailbox and your computer can read and send for you"
            description="Type your address and the rest is worked out. Your password goes to your own computer and stays there."
            action={
              <Button variant="primary" asChild>
                <Link to="/$org/email/connect" params={{ org }}>Connect a mailbox</Link>
              </Button>
            }
          />
        </Card>
      </div>
    );
  }

  const order = [...all].sort((a, b) => Number(a.health === 'good') - Number(b.health === 'good'));
  const waiting = batches?.filter((item) => item.status === 'waiting') ?? [];
  const fresh = all.reduce((sum, item) => sum + (item.new_count ?? 0), 0);
  return (
    <div className="pb-16">
      <EmailTabs org={org} here="mailboxes" newCount={fresh} waiting={waiting.length} />
      <div className="flex items-start justify-between gap-6">
        <Hero mailboxes={all} />
        <div className="shrink-0">{connect}</div>
      </div>
      <ul className="picture-stage mt-8 overflow-hidden rounded-(--radius-card) border border-line">
        {order.map((mailbox) => <Row key={mailbox.id} org={org} mailbox={mailbox} workspace={workspaces?.find((item) => item.id === mailbox.workspace)} />)}
      </ul>
      {waiting[0] ? (
        <Link to="/$org/email/leads/$batch" params={{ org, batch: waiting[0].id }} className="group mt-4 flex items-center justify-between gap-4 rounded-(--radius-card) border border-dashed border-line-strong px-4 py-3 transition-colors duration-(--duration-fast) ease-standard hover:bg-hover">
          <span className="min-w-0 text-body text-pretty">
            <span className="font-medium">{waiting[0].by} found {waiting[0].found} people for {waiting[0].list_name}.</span> <span className="text-muted">Nobody gets an email until you have looked.</span>
          </span>
          <span className="flex shrink-0 items-center gap-1 text-secondary font-medium">
            Look and decide
            <ArrowRight aria-hidden="true" className="size-4 transition-transform duration-(--duration-fast) ease-standard group-hover:translate-x-1" />
          </span>
        </Link>
      ) : null}
    </div>
  );
}
