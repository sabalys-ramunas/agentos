/**
 * The mailboxes, as drawn: one card each with who it is, how it is, today's sending against its daily
 * limit, what came in, the routines that use it and the computer it runs on. A mailbox in trouble comes
 * first, edged in red, with what happened in plain words and its one next step.
 * Concept stage: answered by the mock only.
 */
import { Link, useParams } from '@tanstack/react-router';
import { CircleAlert, Mail, Plus, Repeat, Server, Users } from 'lucide-react';
import { useLeadBatches, useMailboxes, useWorkspaces } from '@/api/hooks';
import { isNotServed } from '@/api/http';
import type { MailArrival, Mailbox, Workspace } from '@/api/types';
import { BusinessMark } from '@/components/brand/identity';
import { SiteMark } from '@/components/brand/site-mark';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Meter, PageHeader } from '@/components/ui/misc';
import { Status } from '@/components/ui/status';
import { EmptyState, ErrorState, NotLive, Skeleton } from '@/components/ui/states';
import { cn } from '@/lib/cn';
import { plural } from '@/lib/format';
import { EmailTabs } from './shared';
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

/** The page's sentence: how many are fine, what needs you, what came and what went out today. */
function heroWords(mailboxes: Mailbox[]): string {
  const fine = mailboxes.filter((item) => item.health === 'good').length;
  const needs = mailboxes.length - fine;
  const freshKnown = mailboxes.some((item) => item.new_count !== null);
  const fresh = mailboxes.reduce((sum, item) => sum + (item.new_count ?? 0), 0);
  const replies = mailboxes.reduce((sum, item) => sum + count(item.arrivals, 'reply'), 0);
  const sent = mailboxes.reduce((sum, item) => sum + item.sent_today, 0);
  return [
    `${plural(fine, 'mailbox is', 'mailboxes are')} fine.`,
    needs ? `${plural(needs, 'needs', 'need')} you.` : null,
    freshKnown ? `${fresh} new today, ${plural(replies, 'of them a reply', 'of them replies')}.` : null,
    `${sent} sent, inside every daily limit.`,
  ]
    .filter(Boolean)
    .join(' ');
}

/** One mailbox, as drawn: who it is, how it is, today's sending against its limit, and where it runs. */
function MailboxCard({ org, mailbox, workspace }: { org: string; mailbox: Mailbox; workspace: Workspace | undefined }) {
  const good = mailbox.health === 'good';
  const sends = mailbox.health !== 'needs_password' && mailbox.health !== 'cannot_send';
  const reads = mailbox.health !== 'needs_password' && mailbox.health !== 'cannot_read';
  const replies = count(mailbox.arrivals, 'reply');
  const out = count(mailbox.arrivals, 'unsubscribe');
  const week = mailbox.week?.reduce((sum, day) => sum + day.sent, 0);
  return (
    <li className="flex min-w-0">
      <Card className={cn('flex min-w-0 flex-1 flex-col gap-4 p-5', !good && 'border-bad-line')}>
        <div className="flex min-w-0 items-start gap-3">
          {workspace ? <BusinessMark name={workspace.name} color={workspace.color} size={40} /> : <SiteMark site={mailbox.site} size={40} tile label={false} />}
          <div className="grid min-w-0 flex-1 justify-items-start gap-1">
            <span title={mailbox.address} className="max-w-full truncate text-card-title">
              {mailbox.address}
            </span>
            <span className="max-w-full truncate text-secondary text-muted">{[workspace?.name, mailbox.provider ?? mailbox.site].filter(Boolean).join(' · ')}</span>
            <Status chip status={{ label: HEALTH_WORD[mailbox.health], tone: good ? 'ok' : mailbox.health === 'unchecked' ? 'idle' : 'bad' }} className="mt-1" />
          </div>
        </div>

        {mailbox.problem ? (
          <div className="flex items-start gap-3 rounded-lg border border-bad-line bg-bad-fill px-4 py-3 text-body text-pretty text-bad">
            <CircleAlert aria-hidden="true" className="mt-1 size-4 shrink-0" />
            <span>
              {mailbox.problem.title} {agoWords(mailbox.problem.since)}. {mailbox.problem.body}
            </span>
          </div>
        ) : null}

        {sends ? (
          <div className="grid gap-2">
            <div className="flex items-baseline justify-between gap-4 text-body">
              <span className="text-muted">Sent today</span>
              <span className="text-fg tabular">
                {mailbox.sent_today.toLocaleString('en')} of {mailbox.daily_limit.toLocaleString('en')} today
              </span>
            </div>
            <Meter value={mailbox.sent_today} max={mailbox.daily_limit} label="Sent today" className="w-full" />
            <span className="text-secondary text-muted">{sendWords(mailbox)}</span>
          </div>
        ) : mailbox.problem ? null : (
          <p className="text-body text-muted">Nothing went out, and nothing was lost.</p>
        )}

        {reads ? (
          <Link to="/$org/email/new-mail" params={{ org }} search={{ mailbox: mailbox.id }} className="link w-fit text-body">
            {mailbox.new_count === null
              ? 'Not read yet'
              : [`${mailbox.new_count} new today`, replies ? plural(replies, 'reply', 'replies') : null, out ? plural(out, 'unsubscribe') : null].filter(Boolean).join(', ')}
          </Link>
        ) : mailbox.problem ? null : (
          <p className="text-body text-muted">New mail is safe on the server.</p>
        )}

        {mailbox.routines?.length ? (
          <ul aria-label="Routines that use it" className="flex min-w-0 flex-wrap gap-x-4 gap-y-1 text-secondary text-muted">
            {mailbox.routines.map((routine) => (
              <li key={routine.id} className="flex min-w-0 items-center gap-1">
                <Repeat aria-hidden="true" className="size-3.5 shrink-0 text-faint" />
                {routine.id === 'follow-up' ? (
                  <Link to="/$org/email/routines/$routine" params={{ org, routine: routine.id }} className="truncate hover:text-fg hover:underline hover:underline-offset-4">
                    {routine.name}
                  </Link>
                ) : (
                  <span className="truncate">{routine.name}</span>
                )}
              </li>
            ))}
          </ul>
        ) : null}

        <div className="mt-auto flex flex-wrap items-center gap-x-4 gap-y-2 border-t border-line pt-4 text-secondary text-muted">
          <span className="inline-flex min-w-0 items-center gap-2">
            <Server aria-hidden="true" className="size-4 shrink-0 text-faint" />
            <span className="truncate">{mailbox.computer}</span>
          </span>
          {week !== undefined && sends ? <span className="tabular">{week.toLocaleString('en')} sent this week</span> : null}
          {mailbox.problem ? (
            <Button variant="primary" size="sm" className="ml-auto" asChild>
              <Link to="/$org/email/connect" params={{ org }} search={{ address: mailbox.address, stage: 'found' }}>
                {mailbox.problem.next}
              </Link>
            </Button>
          ) : null}
        </div>
      </Card>
    </li>
  );
}

function Loading() {
  return (
    <div className="grid gap-6">
      <Skeleton className="h-10 w-48" />
      <Skeleton className="h-6 w-full max-w-2xl" />
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
        {[0, 1, 2].map((item) => <Skeleton key={item} className="h-64 rounded-xl" />)}
      </div>
    </div>
  );
}

export function Mailboxes() {
  const { org } = useParams({ from: '/$org/email' });
  const mailboxes = useMailboxes(org);
  const batches = useLeadBatches(org).data;
  const workspaces = useWorkspaces(org).data;

  if (isNotServed(mailboxes.error)) return <Card><NotLive /></Card>;
  if (mailboxes.error) return <ErrorState error={mailboxes.error} onRetry={() => void mailboxes.refetch()} />;
  if (!mailboxes.data) return <Loading />;

  const all = mailboxes.data;
  if (all.length === 0) {
    return (
      <div>
        <PageHeader title="Email" />
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
  // One primary per view: a mailbox to connect again is the point of the page while there is one.
  const trouble = all.some((item) => item.problem);
  return (
    <div className="pb-16">
      <PageHeader
        title="Email"
        description={heroWords(all)}
        actions={
          <Button variant={trouble ? 'secondary' : 'primary'} asChild>
            <Link to="/$org/email/connect" params={{ org }}>
              <Plus aria-hidden="true" className="size-4" />
              Connect a mailbox
            </Link>
          </Button>
        }
      />
      <EmailTabs org={org} here="mailboxes" newCount={fresh} waiting={waiting.length} />
      <ul aria-label="Mailboxes" className="grid items-stretch gap-4 md:grid-cols-2 xl:grid-cols-3">
        {order.map((mailbox) => (
          <MailboxCard key={mailbox.id} org={org} mailbox={mailbox} workspace={workspaces?.find((item) => item.id === mailbox.workspace)} />
        ))}
      </ul>
      {waiting[0] ? (
        <Card className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-3 px-5 py-4">
          <Users aria-hidden="true" className="size-5 shrink-0 text-muted" />
          <p className="min-w-0 flex-1 basis-80 text-body text-pretty">
            <span className="font-medium">
              {waiting[0].by} found {waiting[0].found} people for {waiting[0].list_name}.
            </span>{' '}
            <span className="text-muted">Nobody gets an email until you have looked.</span>
          </p>
          <Button size="sm" asChild>
            <Link to="/$org/email/leads/$batch" params={{ org, batch: waiting[0].id }}>
              Look and decide
            </Link>
          </Button>
        </Card>
      ) : null}
    </div>
  );
}
