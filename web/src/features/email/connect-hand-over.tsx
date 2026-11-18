import { Link } from '@tanstack/react-router';
import { ArrowUpRight, Terminal } from 'lucide-react';
import { useEffect, useState } from 'react';
import { useEndMailForm, useMailboxes, useMailFormLink, useMailFormState, useWorkspaces } from '@/api/hooks';
import type { Mailbox, MailComputer, MailDiscovery, MailFormLinkBody } from '@/api/types';
import { describeError } from '@/lib/errors';
import { Button } from '@/components/ui/button';
import { CodeBlock, CopyButton, useCopy } from '@/components/ui/misc';
import { Spinner } from '@/components/ui/spinner';
import { StateGlyph } from '@/components/ui/state-badge';
import { apiMode } from '@/config';
import { adminNote, endingWords, GOOGLE_SECURITY, mailCompanyNote } from './words';
import type { Check, Ending } from './words';
import type { Go, OpenedForm, TypedServers } from './connect-steps';
import { ComputerChoice, Disclosure } from './connect-steps';
import { InlineError } from '@/components/ui/states';

/** The mailbox id a new one gets: its business, then the part of the address before the @. */
function mailboxIdFor(address: string, workspace: string) {
  const local = address.split('@')[0]?.toLowerCase().replace(/[^a-z0-9-]+/g, '-').replace(/^-+|-+$/g, '') || 'mail';
  return `${workspace}/${local}`;
}

/** What a person typed, as the node's form takes it. A correction keeps the ports found with it; with nothing found, the usual ports are asked. */
function typedBody(found: MailDiscovery, typed: TypedServers | null): Pick<MailFormLinkBody, 'imap' | 'smtp'> {
  if (!typed) return {};
  if (found.result !== 'not_found') return { imap: { host: typed.incoming }, smtp: { host: typed.outgoing } };
  return { imap: { host: typed.incoming, port: 993, security: 'tls' }, smtp: { host: typed.outgoing, port: 465, security: 'tls' } };
}

/**
 * Opens the node's form for this mailbox in a new tab. The tab opens on the press, so a browser does not
 * block it, and points at the form once the node has made the link.
 */
function useFormOpener(org: string, found: MailDiscovery, typed: TypedServers | null, onOpened: (form: OpenedForm | null) => void) {
  const workspace = useWorkspaces(org).data?.[0]?.id ?? 'home';
  const link = useMailFormLink(org);
  const open = (after?: () => void) => {
    const tab = window.open('', '_blank');
    link.mutate(
      { address: found.address, id: mailboxIdFor(found.address, workspace), ...typedBody(found, typed) },
      {
        onSuccess: (answer) => {
          if (tab) {
            tab.opener = null;
            tab.location.href = answer.url;
          } else {
            window.open(answer.url, '_blank', 'noopener');
          }
          onOpened({ token: answer.token, expires: answer.expires_at });
          after?.();
        },
        onError: () => tab?.close(),
      },
    );
  };
  return { open, busy: link.isPending, error: link.error };
}

export function HandOverStep({ org, found, computer, typed, go, onOpened }: { org: string; found: MailDiscovery; computer: MailComputer; typed: TypedServers | null; go: Go; onOpened: (form: OpenedForm | null) => void }) {
  const line = `agentos mail connect ${found.address}`;
  const [terminal, setTerminal] = useState(!computer.has_screen);
  useEffect(() => setTerminal(!computer.has_screen), [computer.has_screen]);
  const opener = useFormOpener(org, found, typed, onOpened);
  return (
    <div className="max-w-xl">
      <p className="text-16 text-pretty text-muted">
        Your password goes straight to <span className="mono text-14 text-fg">{computer.id}</span>, on a small page it makes for this one job. It never passes through agentos Cloud.
      </p>
      <div className="mt-4"><ComputerChoice computers={found.computers} value={computer.id} onChange={(id) => go('hand-over', { computer: id, replace: true })} /></div>
      {computer.has_screen ? (
        <div className="mt-4 flex flex-wrap items-center gap-3">
          <Button variant="primary" size="lg" loading={opener.busy} onClick={() => opener.open(() => go('waiting'))}>
            Open the form on {computer.id}
            <ArrowUpRight aria-hidden="true" className="size-4" />
          </Button>
          <button type="button" aria-expanded={terminal} className="inline-flex items-center gap-1 text-13 text-muted hover:text-fg" onClick={() => setTerminal(!terminal)}>
            <Terminal aria-hidden="true" className="size-3" />
            Or use its terminal
          </button>
        </div>
      ) : (
        <p className="mt-4 text-14 text-pretty">This computer has no screen, so its terminal is the way in.</p>
      )}
      {terminal ? (
        <div className="spine-rise mt-3">
          <CodeBlock code={line} label="One line for its terminal" />
          <p className="mt-2 text-13 text-pretty text-muted">Type this on {computer.id} itself. It asks for the password with the letters hidden.</p>
          <Button className="mt-3" onClick={() => go('waiting')}>I ran it</Button>
        </div>
      ) : null}
      {opener.error ? <InlineError className="mt-3">{describeError(opener.error)}</InlineError> : null}
      <p className="mt-4 text-13 text-faint">{computer.has_screen ? 'The form works once, and only for a short time. ' : ''}Nothing is saved until both checks pass.</p>
      {computer.has_screen ? (
        <div className="mt-2">
          <Disclosure label={`Not sitting at ${computer.id} right now?`}>
            The form only opens on {computer.id} itself, so the password never crosses the internet. Open this same page there and you carry on from this step.
            <span className="mt-2 block"><CopyButton text={location.href} label="Copy this page's link" /></span>
          </Disclosure>
        </div>
      ) : null}
    </div>
  );
}

/** The seconds left until a moment, never below zero. */
const secondsTo = (at: number) => Math.max(0, Math.round((at - Date.now()) / 1_000));

function Countdown({ until }: { until: number }) {
  const [left, setLeft] = useState(() => secondsTo(until));
  useEffect(() => {
    const timer = setInterval(() => setLeft(secondsTo(until)), 1_000);
    return () => clearInterval(timer);
  }, [until]);
  return <span className="tabular">{Math.floor(left / 60)}:{String(left % 60).padStart(2, '0')}</span>;
}

/** Whether a mailbox for this address is on the node now, however it was connected. */
function hasMailbox(rows: Mailbox[] | undefined, address: string) {
  return (rows ?? []).some((box) => box.address.toLowerCase() === address.toLowerCase());
}

/**
 * The panel waits for the form on the computer. It learns the form was used when the mailbox shows up, and
 * learns the link ended when the node says so. An ended link with no mailbox says so, and offers a new one.
 */
export function WaitingStep({ org, found, computer, typed, form, go, onOpened }: { org: string; found: MailDiscovery; computer: MailComputer; typed: TypedServers | null; form: OpenedForm | null; go: Go; onOpened: (form: OpenedForm | null) => void }) {
  const mailboxes = useMailboxes(org);
  const end = useEndMailForm(org);
  const opener = useFormOpener(org, found, typed, onOpened);
  const token = form?.token ?? null;
  const state = useMailFormState(org, token);
  // The token whose link ended with no mailbox behind it. A new link has a new token, so it starts clear.
  const [gone, setGone] = useState<string | null>(null);
  const connected = hasMailbox(mailboxes.data, found.address);
  const refetch = mailboxes.refetch;
  useEffect(() => {
    const timer = setInterval(() => void refetch(), 3_000);
    return () => clearInterval(timer);
  }, [refetch]);
  useEffect(() => {
    if (connected) go('checks', { replace: true });
  }, [connected, go]);
  const ended = state.data?.state === 'ended';
  // An ended link may have been used: the mailbox is looked for once more before the link is said to be over.
  useEffect(() => {
    if (!ended) return;
    void refetch().then((answer) => {
      if (!hasMailbox(answer.data, found.address)) setGone(token);
    });
  }, [ended, refetch, found.address, token]);
  const back = () => {
    onOpened(null);
    go('hand-over', { replace: true });
  };
  const cancel = () => (token ? end.mutate(token, { onSettled: back }) : back());
  const endsAt = form?.expires ? Date.parse(form.expires) : Number.NaN;

  if (token !== null && gone === token) {
    return (
      <div className="max-w-xl">
        <h3 className="text-16 font-semibold">This link has ended</h3>
        <p className="mt-1 text-14 text-pretty text-muted">It was used without connecting, ran out of time, or was cancelled. Nothing was saved. A new link works the same way.</p>
        <div className="mt-4 flex flex-wrap items-center gap-3">
          <Button variant="primary" loading={opener.busy} onClick={() => opener.open()}>
            Start again
            <ArrowUpRight aria-hidden="true" className="size-3" />
          </Button>
          <Button variant="ghost" onClick={back}>
            Back
          </Button>
        </div>
        {opener.error ? <InlineError className="mt-3">{describeError(opener.error)}</InlineError> : null}
      </div>
    );
  }

  return (
    <div className="max-w-xl">
      <p className="flex items-center gap-3 text-16 text-pretty">
        <Spinner className="size-4" />
        Waiting for the form on {computer.id}. This page moves on by itself.
      </p>
      {Number.isFinite(endsAt) ? <p className="mt-2 text-14 text-muted">The form closes in <Countdown until={endsAt} />.</p> : null}
      <div className="mt-4 flex flex-wrap items-center gap-3">
        <Button loading={opener.busy} onClick={() => opener.open()}>
          Open the form again
          <ArrowUpRight aria-hidden="true" className="size-3" />
        </Button>
        <button type="button" className="link text-13" disabled={end.isPending} onClick={cancel}>Cancel</button>
        {apiMode === 'mock' ? <button type="button" className="link text-13" onClick={() => go('checks', { replace: true })}>Concept: pretend it was filled in</button> : null}
      </div>
      {opener.error ? <InlineError className="mt-3">{describeError(opener.error)}</InlineError> : null}
    </div>
  );
}

function CheckRow({ label, state }: { label: string; state: Check }) {
  const word = { waiting: 'Not tried yet', running: 'Checking', yes: 'Yes', no: 'No', stopped: 'Stopped first' }[state];
  return (
    <li className="flex items-center gap-3 border-t border-line py-3 first:border-t-0">
      <span className="grid size-5 place-items-center">
        {state === 'running' ? <Spinner className="size-4" /> : state === 'yes' ? <StateGlyph state="good" size={20} /> : state === 'no' ? <StateGlyph state="stuck" size={20} /> : state === 'stopped' ? <StateGlyph state="waiting" size={20} /> : <span className="size-2 rounded-full bg-fill-3" />}
      </span>
      <span className="min-w-0 flex-1 text-14">{label}</span>
      <span className="text-13 text-muted">{word}</span>
    </li>
  );
}

export function ChecksStep({ provider, read, send }: { provider: string; read: Check; send: Check }) {
  return (
    <div className="max-w-xl">
      <ul className="rounded-lg border border-line px-4">
        <CheckRow label="Can it read new mail?" state={read} />
        <CheckRow label="Can it send mail?" state={send} />
      </ul>
      <p className="mt-3 text-13 text-faint">This only signs in to {provider}. Nothing is read and nothing is sent.</p>
    </div>
  );
}

function CopyNote({ text, label, after }: { text: string; label: string; after: string }) {
  const { copied, copy } = useCopy();
  return (
    <div className="mt-4">
      <p className="stranger rounded-sm bg-subtle py-3 pr-3 text-13 whitespace-pre-line text-muted">{text}</p>
      <div className="mt-3 flex items-center gap-3">
        <Button variant="primary" onClick={() => void copy(text)}>{copied ? 'Copied' : label}</Button>
        {copied ? <span className="spine-rise text-13 text-muted">{after}</span> : null}
      </div>
    </div>
  );
}

export function FailedStep({ ending, found, computer, go }: { ending: Ending; found: MailDiscovery; computer: string; go: Go }) {
  const provider = found.provider ?? 'Your mail company';
  const words = endingWords(ending, provider, found.domain, computer);
  const [opened, setOpened] = useState(false);
  return (
    <div className="max-w-xl">
      <h3 className="text-20 font-semibold text-balance">{words.title}</h3>
      <p className="mt-2 text-14 text-pretty text-muted">{words.body}</p>
      {ending === 'two-step-off' ? null : (
        <ul className="mt-4 rounded-lg border border-line px-4">
          <CheckRow label="Can it read new mail?" state={words.read} />
          <CheckRow label="Can it send mail?" state={words.send} />
        </ul>
      )}
      {ending === 'two-step-off' ? null : <p className="mt-3 text-13 text-faint">Nothing was saved.</p>}
      {ending === 'wrong-password' ? <Button variant="primary" className="mt-4" onClick={() => go('password')}>Make a new app password</Button> : null}
      {ending === 'two-step-off' ? (
        opened ? (
          <Button variant="primary" className="spine-rise mt-4" onClick={() => go('password')}>It is on now, carry on</Button>
        ) : (
          <Button variant="primary" className="mt-4" asChild>
            <a href={GOOGLE_SECURITY} target="_blank" rel="noreferrer" onClick={() => setOpened(true)}>
              Turn it on at Google
              <ArrowUpRight aria-hidden="true" className="size-4" />
            </a>
          </Button>
        )
      ) : null}
      {ending === 'imap-blocked' ? <CopyNote text={adminNote(found.address, found.domain)} label="Copy a note for your admin" after="Send it to whoever runs your company's Google account." /> : null}
      {ending === 'certificate' ? <CopyNote text={mailCompanyNote(found.domain, (found.incoming ?? '').split(',')[0] ?? '')} label="Copy a note for your mail company" after="Send it to them, and try again once it is fixed." /> : null}
    </div>
  );
}

export function DoneStep({ org, address, provider, computer }: { org: string; address: string; provider: string; computer: string }) {
  return (
    <div className="spine-rise max-w-xl">
      <p className="flex items-center gap-3"><StateGlyph state="good" size={24} /><span className="text-20 font-semibold">Connected</span></p>
      <h2 className="mt-2 text-24 font-semibold break-all">{address}</h2>
      <p className="mt-2 text-14 text-pretty text-muted">It can read and it can send. The password is in {computer}'s vault, tied to {provider}'s two servers, and nobody can read it back, agents included.</p>
      <div className="mt-6 flex flex-wrap items-center gap-2">
        <Button variant="primary" size="lg" asChild><Link to="/$org/email/new-mail" params={{ org }}>See new mail</Link></Button>
        <Button size="lg" asChild><Link to="/$org/email/leads" params={{ org }}>Build a list</Link></Button>
        <Button size="lg" variant="ghost" asChild><Link to="/$org/email" params={{ org }}>All mailboxes</Link></Button>
      </div>
    </div>
  );
}
