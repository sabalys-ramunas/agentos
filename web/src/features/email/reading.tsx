/**
 * What is new, and what became of it. Mail is shown as text with its own edge, so words a stranger
 * wrote can never pass for ours. No picture is fetched and no link is live. Beside each message sits
 * what a routine did with it, because that is why a person comes here and not to their mail app.
 * Concept stage: answered by the mock only.
 */
import { Link, useNavigate, useParams, useSearch } from '@tanstack/react-router';
import { Bell, FileText, ImageOff, PenLine, Repeat } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { toast } from 'sonner';
import { useMailboxes, useMailMark, useMailMessage, useMailMessages, useMailSend } from '@/api/hooks';
import { isNotServed } from '@/api/http';
import type { Mailbox, MailBlock, MailMessage } from '@/api/types';
import { PersonMark } from '@/components/brand/identity';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Dialog } from '@/components/ui/dialog';
import { Field, Input, Textarea } from '@/components/ui/field';
import { PageHeader } from '@/components/ui/misc';
import { Select } from '@/components/ui/select';
import { StateGlyph } from '@/components/ui/state-badge';
import { Banner, EmptyState, ErrorState, NotLive, Skeleton } from '@/components/ui/states';
import { Status } from '@/components/ui/status';
import { cn } from '@/lib/cn';
import { clock, dayAndTime, plural } from '@/lib/format';
import { EmailTabs, FromComputer } from './shared';
import { firstName } from './words';

const TAG: Partial<Record<MailMessage['kind'], string>> = { reply: 'Reply', unsubscribe: 'Unsubscribed', automatic: 'Automatic' };
const MANY = 10;

function Body({ blocks }: { blocks: MailBlock[] }) {
  return (
    <div className="grid gap-3">
      {blocks.map((block, index) => {
        if (block.kind === 'text') return <p key={index} className="text-body leading-6 whitespace-pre-wrap">{block.text}</p>;
        if (block.kind === 'link') {
          return (
            <p key={index} className="text-body">
              {block.text} <span className="mono text-caption break-all text-faint">({block.url})</span>
            </p>
          );
        }
        return (
          <p key={index} className="left-out flex items-center gap-2 rounded-md border border-dashed border-line-strong px-3 py-3 text-caption text-faint">
            <ImageOff aria-hidden="true" className="size-3 shrink-0" />
            <span className="min-w-0">A picture from <span className="mono">{block.host}</span> was not loaded</span>
          </p>
        );
      })}
    </div>
  );
}

function WhatHappened({ org, message }: { org: string; message: MailMessage }) {
  if (!message.did) return <p className="text-secondary text-muted">What a routine did with it is not kept on this computer yet.</p>;
  if (!message.did) return <p className="text-secondary text-muted">What a routine did with it is not kept on this computer yet.</p>;
  if (!message.did.length) return <p className="text-secondary text-muted">No routine touched this. It is only here for you to read.</p>;
  return (
    <ol className="relative grid gap-4 before:absolute before:inset-y-2 before:left-[7.5px] before:w-px before:bg-line">
      {(message.did ?? []).map((item, index) => (
        <li key={index} className="relative flex gap-3 text-body text-pretty">
          <span className="mt-1 grid size-4 shrink-0 place-items-center rounded-full bg-card text-faint">
            {item.waits ? <StateGlyph state="waiting" size={16} /> : item.routine ? <Repeat aria-hidden="true" className="size-3" /> : <Bell aria-hidden="true" className="size-3" />}
          </span>
          <span className="min-w-0">
            {item.words}
            <span className="block text-caption text-faint">
              {clock(item.at)}
              {item.routine ? (
                <>
                  {' · '}
                  {item.routine.id === 'follow-up' ? (
                    <Link to="/$org/email/routines/$routine" params={{ org, routine: item.routine.id }} className="hover:text-fg hover:underline hover:underline-offset-4">{item.routine.name}</Link>
                  ) : (
                    item.routine.name
                  )}
                </>
              ) : null}
              {item.waits ? <> · <Link to="/$org/needs-you" params={{ org }} className="link">Look at it in Needs you</Link></> : null}
            </span>
          </span>
        </li>
      ))}
    </ol>
  );
}

function Reply({ org, message, mailbox }: { org: string; message: MailMessage; mailbox: Mailbox | undefined }) {
  const [text, setText] = useState('');
  const send = useMailSend(org);
  useEffect(() => setText(''), [message.id]);
  if (message.kind === 'unsubscribe') return <p className="text-secondary text-muted">{firstName(message.from_name)} asked not to hear from you, so there is nothing to reply to.</p>;
  if (!mailbox) return null;
  return (
    <form
      onSubmit={(event) => {
        event.preventDefault();
        if (!text.trim()) return;
        send.mutate(
          { mailbox: mailbox.id, to: message.from_address, subject: `Re: ${message.subject}`, body: text, reply_to: message.id },
          {
            onSuccess: () => {
              setText('');
              toast.success('Sent. It is in your Sent folder too.');
            },
          },
        );
      }}
    >
      <Textarea rows={3} value={text} onChange={(event) => setText(event.target.value)} aria-label={`Reply to ${message.from_name}`} placeholder="Write a reply" />
      <div className="mt-2 flex items-center justify-between gap-4">
        <p className="min-w-0 truncate text-caption text-faint">From {mailbox.address} · uses 1 of today's {mailbox.daily_limit - mailbox.sent_today} left</p>
        <Button type="submit" loading={send.isPending} aria-disabled={!text.trim()}>Send</Button>
      </div>
    </form>
  );
}

function Reader({ org, message, mailbox, onUnread }: { org: string; message: MailMessage; mailbox: Mailbox | undefined; onUnread: () => void }) {
  // The list carries only a clip on a node, so the body is read in full when the reader opens.
  const whole = useMailMessage(org, message.id);
  const blocks = whole.data?.body ?? message.body;
  return (
    <article aria-label={message.subject} className="min-w-0">
      <header>
        <div className="flex items-start justify-between gap-4">
          <h2 className="min-w-0 text-heading text-balance">{message.subject}</h2>
          <Button variant="ghost" size="sm" onClick={onUnread}>Mark unread</Button>
        </div>
        <p className="mt-2 flex min-w-0 items-center gap-2 text-secondary text-muted">
          <PersonMark id={message.from_address} name={message.from_name} size={20} />
          <span className="truncate">{message.from_name}</span>
          <span className="mono min-w-0 truncate text-caption text-faint">{message.from_address}</span>
          <span className="ml-auto shrink-0 text-caption text-faint">{dayAndTime(message.at)}</span>
        </p>
      </header>
      {message.kind === 'unsubscribe' ? (
        <p className="mt-6 text-body text-pretty text-muted">{firstName(message.from_name)} pressed the unsubscribe link your computer adds to every email. Nobody wrote anything.</p>
      ) : (
      <div className="stranger mt-6">
        <p className="mb-3 text-eyebrow text-muted">Written by someone else</p>
        {blocks.length || !whole.isPending ? (blocks.length || !whole.isError ? <Body blocks={blocks} /> : <p className="text-body text-muted">This message could not be opened from this computer just now.</p>) : <p className="text-body text-muted">Opening the message.</p>}
        {message.attachments.length ? (
          <ul className="mt-4 grid gap-1">
            {message.attachments.map((file) => (
              <li key={file.name} className="flex items-center gap-2 text-secondary text-muted">
                <FileText aria-hidden="true" className="size-3 shrink-0 text-faint" />
                <span className="mono min-w-0 truncate text-caption">{file.name}</span>
                <span className="shrink-0 text-caption text-faint">Not opened</span>
              </li>
            ))}
          </ul>
        ) : null}
        <p className="mt-4 text-caption text-faint">Shown as plain text. No picture was fetched and no link here can be pressed.</p>
      </div>
      )}
      <section aria-label="What happened to it" className="mt-8 border-t border-line pt-6">
        <h3 className="mb-3 text-body font-semibold">What happened to it</h3>
        <WhatHappened org={org} message={message} />
      </section>
      <section aria-label="Reply" className="mt-8 border-t border-line pt-6">
        <Reply org={org} message={message} mailbox={mailbox} />
      </section>
    </article>
  );
}

const TAG_TONE: Partial<Record<MailMessage['kind'], 'ok' | 'idle'>> = { reply: 'ok', unsubscribe: 'idle', automatic: 'idle' };

/** One message as drawn: who, what kind, the subject and its first line, and when. The open one is selected. */
function MessageRow({ message, open, onOpen }: { message: MailMessage; open: boolean; onOpen: () => void }) {
  const tag = TAG[message.kind];
  const tone = TAG_TONE[message.kind];
  return (
    <li className={cn('border-b border-l-2 border-b-line last:border-b-0', open ? 'border-l-accent bg-accent-soft' : 'border-l-transparent')}>
      <button
        type="button"
        aria-current={open ? 'true' : undefined}
        onClick={onOpen}
        className="focus-inset grid w-full grid-cols-[32px_minmax(0,1fr)_auto] items-start gap-x-3 py-3 pr-5 pl-4 text-left transition-colors duration-(--duration-fast) ease-standard hover:bg-hover"
      >
        <PersonMark id={message.from_address} name={message.from_name} size={32} />
        <span className="grid min-w-0 gap-1">
          <span className="flex min-w-0 items-center gap-2">
            <span className={cn('truncate text-label', message.unread ? 'font-semibold text-fg' : 'text-fg')}>{message.from_name}</span>
            {message.unread ? <span className="sr-only">, unread</span> : null}
            {tag && tone ? <Status chip status={{ label: tag, tone }} className="shrink-0" /> : null}
          </span>
          <span className="truncate text-secondary">
            <span className={cn(message.unread ? 'font-semibold text-fg' : 'font-medium text-fg')}>{message.subject}</span>
            {message.clip ? <span className="text-muted"> · {message.clip}</span> : null}
          </span>
        </span>
        <time dateTime={message.at} className="pt-px text-secondary text-muted tabular">
          {clock(message.at)}
        </time>
      </button>
    </li>
  );
}

function NewEmail({ org, mailboxes, open, onClose }: { org: string; mailboxes: Mailbox[]; open: boolean; onClose: () => void }) {
  const healthy = mailboxes.filter((item) => item.health === 'good');
  const [from, setFrom] = useState(healthy[0]?.id ?? '');
  const [to, setTo] = useState('');
  const [subject, setSubject] = useState('');
  const [body, setBody] = useState('');
  const send = useMailSend(org);
  const people = to.split(/[,;\s]+/).filter(Boolean).length;
  const tooMany = people > MANY;
  const ready = people > 0 && !tooMany && subject.trim() && body.trim();
  return (
    <Dialog
      open={open}
      onOpenChange={(value) => !value && onClose()}
      title="New email"
      footer={
        <Button
          variant="primary"
          loading={send.isPending}
          aria-disabled={!ready}
          onClick={() => {
            if (!ready) return;
            send.mutate({ mailbox: from, to, subject, body }, { onSuccess: () => { toast.success('Sent. It is in your Sent folder too.'); onClose(); } });
          }}
        >
          Send
        </Button>
      }
    >
      <div className="grid gap-4">
        <Field label="From">
          <Select value={from} onValueChange={setFrom} options={healthy.map((item) => ({ value: item.id, label: item.address }))} />
        </Field>
        <Field label="To" error={tooMany ? `That is ${people} people. Mail to more than ${MANY} goes through a list, so the unsubscribe and the daily limit always apply.` : undefined}>
          <Input value={to} onChange={(event) => setTo(event.target.value)} placeholder="name@company.com" />
        </Field>
        <Field label="Subject"><Input value={subject} onChange={(event) => setSubject(event.target.value)} /></Field>
        <Field label="Message"><Textarea rows={5} value={body} onChange={(event) => setBody(event.target.value)} /></Field>
      </div>
    </Dialog>
  );
}

export function Reading() {
  const { org } = useParams({ from: '/$org/email/new-mail' });
  const search = useSearch({ from: '/$org/email/new-mail' });
  const navigate = useNavigate({ from: '/$org/email/new-mail' });
  const messages = useMailMessages(org);
  const mailboxes = useMailboxes(org).data ?? [];
  const mark = useMailMark(org);
  const [writing, setWriting] = useState(false);

  const all = useMemo(() => [...(messages.data ?? [])].sort((a, b) => b.at.localeCompare(a.at)), [messages.data]);
  const shown = search.mailbox ? all.filter((item) => item.mailbox === search.mailbox) : all;
  const open = shown.find((item) => item.id === search.message) ?? shown[0] ?? null;
  const unreadOpen = open?.unread ? open.id : null;

  useEffect(() => {
    if (unreadOpen) mark.mutate({ id: unreadOpen, read: true });
    // Opening one message marks it once. `mark` changes every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [unreadOpen]);

  if (isNotServed(messages.error)) return <Card><NotLive /></Card>;
  if (messages.error) return <ErrorState error={messages.error} onRetry={() => void messages.refetch()} />;
  if (!messages.data) {
    return (
      <div className="grid gap-6">
        <Skeleton className="h-10 w-96 rounded-lg" />
        <div className="grid gap-6 lg:grid-cols-[24rem_minmax(0,1fr)]">
          <Skeleton className="h-96 rounded-lg" />
          <Skeleton className="h-96 rounded-lg" />
        </div>
      </div>
    );
  }

  const answers = shown.filter((item) => item.kind !== 'mail');
  const rest = shown.filter((item) => item.kind === 'mail');
  const fresh = all.filter((item) => item.unread).length;
  const replies = shown.filter((item) => item.kind === 'reply').length;
  const held = mailboxes.filter((item) => item.health !== 'good');
  const computers = [...new Set(mailboxes.filter((item) => item.health === 'good').map((item) => item.computer))];
  const openMessage = (id: string) => void navigate({ search: (old) => ({ ...old, message: id }), replace: true });
  const row = (message: MailMessage) => <MessageRow key={message.id} message={message} open={open?.id === message.id} onOpen={() => openMessage(message.id)} />;
  const readable = mailboxes.filter((item) => item.health === 'good');

  return (
    <div className="pb-16">
      <PageHeader
        title="Email"
        description={`${plural(shown.length, 'message')} today${replies ? `, ${plural(replies, 'reply', 'replies')}` : ''}.`}
        actions={
          <Button variant="primary" icon={<PenLine aria-hidden="true" className="size-4" />} onClick={() => setWriting(true)}>
            New email
          </Button>
        }
      />
      <div className="mb-6 flex flex-wrap items-center justify-between gap-4">
        <EmailTabs org={org} here="new-mail" newCount={fresh} className="mb-0" />
        {readable.length > 1 ? (
          <Select
            aria-label="Which mailbox"
            value={search.mailbox ?? 'all'}
            onValueChange={(next) => void navigate({ search: next === 'all' ? {} : { mailbox: next }, replace: true })}
            options={[{ value: 'all', label: 'Every mailbox' }, ...readable.map((item) => ({ value: item.id, label: item.address }))]}
            className="w-full sm:w-72"
          />
        ) : null}
      </div>

      {held.map((item) => (
        <Banner key={item.id} tone="warn" className="mb-6">
          {item.address} cannot be read until it is connected again. New mail is safe on the server.
        </Banner>
      ))}

      <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,5fr)_minmax(0,6fr)]">
        <div className="grid min-w-0 gap-3">
          <Card className="overflow-hidden">
            {answers.length ? (
              <section aria-labelledby="mail-answers">
                <h2 id="mail-answers" className="border-b border-line px-5 py-3 text-eyebrow text-muted">
                  Answers to your emails
                </h2>
                <ul>{answers.map(row)}</ul>
              </section>
            ) : null}
            {rest.length ? (
              <section aria-labelledby="mail-rest" className={cn(answers.length && 'border-t border-line')}>
                <h2 id="mail-rest" className="border-b border-line px-5 py-3 text-eyebrow text-muted">
                  Everything else
                </h2>
                <ul>{rest.map(row)}</ul>
              </section>
            ) : null}
            {!shown.length ? <EmptyState icon={<FileText />} title="Nothing new here" description="New mail shows up as soon as your computer sees it." /> : null}
          </Card>
          {computers.length ? <FromComputer computers={computers} /> : null}
        </div>
        <div className="min-w-0 lg:sticky lg:top-20 lg:self-start">
          {open ? (
            <Card className="p-5 md:p-6">
              <Reader org={org} message={open} mailbox={mailboxes.find((item) => item.id === open.mailbox)} onUnread={() => mark.mutate({ id: open.id, read: false })} />
            </Card>
          ) : null}
        </div>
      </div>
      <NewEmail org={org} mailboxes={mailboxes} open={writing} onClose={() => setWriting(false)} />
    </div>
  );
}
