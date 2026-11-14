/**
 * Connect a mailbox, for someone who has never heard of a mail server. A person types an address and
 * watches it get found, in plain words. Each stage is in the URL, so Back works and every state has an
 * address. The panel holds no password field: at the point a password is needed it hands over to the
 * person's own computer, and the route picture beside the steps shows why that is the short way.
 * Concept stage: answered by the mock only.
 */
import { Link, useNavigate, useParams, useSearch } from '@tanstack/react-router';
import { ArrowLeft, ArrowUpRight, Check as CheckIcon, ChevronDown, Copy, Terminal } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { useEndMailForm, useMailboxes, useMailDiscover, useMailFormLink, useMailFormState, useProviders, useWorkspaces } from '@/api/hooks';
import { Card } from '@/components/ui/card';
import { NotLive } from '@/components/ui/states';
import type { Mailbox, MailComputer, MailDiscovery, MailFormLinkBody } from '@/api/types';
import { describeError } from '@/lib/errors';
import { SiteMark } from '@/components/brand/site-mark';
import { Button } from '@/components/ui/button';
import { Field, Input } from '@/components/ui/field';
import { CodeBlock, CopyButton, useCopy } from '@/components/ui/misc';
import { Spinner } from '@/components/ui/spinner';
import { StateGlyph } from '@/components/ui/state-badge';
import { apiMode } from '@/config';
import { cn } from '@/lib/cn';
import { RoutePicture } from './route-picture';
import type { KeyAt } from './route-picture';
import './email.css';
import { adminNote, endingWords, GOOGLE_APP_PASSWORDS, GOOGLE_SECURITY, hintFor, looksLikeAddress, mailCompanyNote } from './words';
import type { Check, ConnectStage, Ending } from './words';

const ENDINGS: readonly string[] = ['wrong-password', 'two-step-off', 'imap-blocked', 'certificate'];
const asEnding = (value: string | undefined) => (value && ENDINGS.includes(value) ? (value as Ending) : null);

/** The two server names a person typed. The node's form shows them again to check, with their ports. */
export interface TypedServers {
  incoming: string;
  outgoing: string;
}

/** The node's form for this mailbox, as the panel holds it while it waits: its token and when it closes. */
interface OpenedForm {
  token: string | null;
  expires: string | null;
}

type Go = (stage: ConnectStage, extra?: { ending?: string; computer?: string; replace?: boolean }) => void;

/** One step down the spine. Done steps fold to a line, so the whole way stays in view. */
function Step({ number, title, state, summary, children }: { number: number; title: string; state: 'done' | 'now' | 'later'; summary?: ReactNode; children?: ReactNode }) {
  return (
    <li className={cn('spine-step', state === 'now' ? 'pb-8' : 'pb-6')} data-now={state === 'now' ? '' : undefined} data-done={state === 'done' ? '' : undefined}>
      <span className="spine-mark">{state === 'done' ? <StateGlyph state="good" size={20} /> : number}</span>
      <h2 className={cn('min-h-6 text-14', state === 'now' ? 'text-16 font-semibold text-fg' : state === 'done' ? 'font-medium text-fg' : 'text-faint')}>
        {title}
      </h2>
      {state === 'done' && summary ? <div className="mt-1 text-13 text-muted">{summary}</div> : null}
      {state === 'now' ? <div className="spine-rise mt-3">{children}</div> : null}
    </li>
  );
}

function Disclosure({ label, children }: { label: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button type="button" aria-expanded={open} onClick={() => setOpen(!open)} className="group inline-flex items-center gap-1 text-13 text-muted hover:text-fg">
        {label}
        <ChevronDown aria-hidden="true" className={cn('size-3 transition-transform duration-(--duration-fast) ease-standard', open && 'rotate-180')} />
      </button>
      {open ? <div className="spine-rise mt-2 max-w-xl text-13 text-pretty text-muted">{children}</div> : null}
    </div>
  );
}

function ServerNames({ found }: { found: MailDiscovery }) {
  return (
    <dl className="grid gap-1">
      <div className="flex gap-2"><dt className="w-24 shrink-0 text-faint">Mail in</dt><dd className="mono min-w-0 break-all text-12">{found.incoming}</dd></div>
      <div className="flex gap-2"><dt className="w-24 shrink-0 text-faint">Mail out</dt><dd className="mono min-w-0 break-all text-12">{found.outgoing}</dd></div>
      <div className="flex gap-2"><dt className="w-24 shrink-0 text-faint">Found in</dt><dd className="min-w-0">{found.source}</dd></div>
    </dl>
  );
}

/** The address, and nothing else on the page asks for attention. */
function AddressStep({ initial, onFind }: { initial: string; onFind: (address: string) => void }) {
  const [value, setValue] = useState(initial);
  const [tried, setTried] = useState(false);
  const hint = hintFor(value);
  const ready = looksLikeAddress(value);
  return (
    <form
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        setTried(true);
        if (ready) onFind(value.trim());
      }}
    >
      <label htmlFor="mail-address" className="block text-32 font-semibold text-balance">What is the email address?</label>
      <p className="mt-2 text-16 text-pretty text-muted">Type it here and the rest gets worked out for you.</p>
      <input
        id="mail-address"
        className="address-field mt-8"
        type="email"
        inputMode="email"
        autoComplete="off"
        autoCapitalize="none"
        spellCheck={false}
        placeholder="you@yourbusiness.com"
        autoFocus
        value={value}
        aria-invalid={tried && !ready ? true : undefined}
        aria-describedby="mail-address-note"
        onChange={(event) => setValue(event.target.value)}
      />
      <p id="mail-address-note" className="mt-2 flex h-5 items-center gap-2 text-13 text-muted">
        {hint ? <><SiteMark site={hint.site} size={14} label={false} />{hint.word}</> : tried && !ready ? 'That does not look like a whole address yet. It needs an @ and a name after it.' : null}
      </p>
      <Button type="submit" variant="primary" size="lg" className="mt-6">Find my mail</Button>
    </form>
  );
}

/** The lookup, one calm line at a time. */
function Finding({ domain, found, failed }: { domain: string; found: MailDiscovery | undefined; failed: boolean }) {
  const [shown, setShown] = useState(1);
  useEffect(() => {
    if (shown >= 3) return;
    const timer = setTimeout(() => setShown(shown + 1), 520);
    return () => clearTimeout(timer);
  }, [shown]);
  const done = Boolean(found) && shown >= 3;
  const lines: { id: string; going: string; gone: ReactNode }[] = [
    { id: 'read', going: 'Reading the address', gone: `Read the address: ${domain}` },
    {
      id: 'who',
      going: 'Finding out who runs this mail',
      gone: found?.provider ? <span className="inline-flex items-center gap-2">Found {found.provider}{found.site ? <SiteMark site={found.site} size={14} label={false} /> : null}</span> : found?.result === 'not_found' ? 'Nobody answered for this name' : 'Found something, not from a source we trust',
    },
    { id: 'how', going: 'Working out how to reach it', gone: found?.result === 'later' ? `${found.provider} needs its own kind of password` : found?.incoming ? 'Found the way in and the way out, both secure' : 'No way in or out to be found' },
  ];
  return (
    <div>
      <ul aria-live="polite" className="grid gap-2 text-14">
        {lines.slice(0, shown).map((line, index) => {
          const settled = index < shown - 1 || done;
          return (
            <li key={line.id} className="spine-rise flex min-h-6 items-center gap-2">
              {settled ? <CheckIcon aria-hidden="true" className="size-4 text-faint" /> : <Spinner className="size-4" />}
              <span className={settled ? 'text-fg' : 'text-muted'}>{settled ? line.gone : line.going}</span>
            </li>
          );
        })}
      </ul>
      {failed ? <p className="mt-2 text-13 text-muted">Your computer did not answer. Check that it is on, and try again.</p> : null}
      <p className="mt-3 text-12 text-faint">Only the name after the @ was looked up. Your address and password stay out of it.</p>
    </div>
  );
}

/** Two names a person has to look at, because a stranger could have written them. */
function HostLine({ label, host, odd }: { label: string; host: string; odd: string | null | undefined }) {
  const [name] = host.split(',');
  const at = odd ? (name ?? '').indexOf(odd) : -1;
  return (
    <div className="grid gap-1 py-3 sm:grid-cols-[6rem_minmax(0,1fr)] sm:items-baseline">
      <dt className="text-13 text-faint">{label}</dt>
      <dd className="mono min-w-0 text-16 break-all text-muted">
        {at >= 0 ? (
          <>
            {name?.slice(0, at)}
            <span className="rounded-sm bg-(--wait-soft) px-1 font-medium text-fg shadow-[inset_0_0_0_1px_var(--wait-line)]">{odd}</span>
          </>
        ) : (
          name
        )}
      </dd>
    </div>
  );
}

function ByHand({ onDone }: { onDone: (typed: TypedServers) => void }) {
  const [incoming, setIncoming] = useState('');
  const [outgoing, setOutgoing] = useState('');
  const ready = incoming.includes('.') && outgoing.includes('.');
  return (
    <form
      className="spine-rise mt-4 grid max-w-md gap-4"
      onSubmit={(event) => {
        event.preventDefault();
        if (ready) onDone({ incoming: incoming.trim(), outgoing: outgoing.trim() });
      }}
    >
      <Field label="Incoming server" hint="Your mail company lists this on its help pages.">
        <Input mono value={incoming} onChange={(event) => setIncoming(event.target.value)} placeholder="imap.yourmailcompany.com" />
      </Field>
      <Field label="Outgoing server">
        <Input mono value={outgoing} onChange={(event) => setOutgoing(event.target.value)} placeholder="smtp.yourmailcompany.com" />
      </Field>
      <div><Button type="submit" variant="primary" aria-disabled={!ready}>Use these</Button></div>
    </form>
  );
}

function FoundStep({ found, onHandOver, onAgain }: { found: MailDiscovery; onHandOver: (typed?: TypedServers) => void; onAgain: () => void }) {
  const [byHand, setByHand] = useState(false);
  if (found.result === 'not_found') {
    return (
      <div className="mt-4 max-w-xl">
        <h3 className="text-20 font-semibold text-balance">We could not find how this mail works</h3>
        <p className="mt-2 text-14 text-pretty text-muted">We asked every public place, and nothing answered for {found.domain}. That happens with smaller mail companies. They will know the two server names, and nothing is lost.</p>
        {byHand ? <ByHand onDone={onHandOver} /> : <Button variant="primary" className="mt-4" onClick={() => setByHand(true)}>Type the server names</Button>}
      </div>
    );
  }
  if (found.result === 'later') {
    return (
      <div className="mt-4 max-w-xl">
        <h3 className="text-20 font-semibold text-balance">{found.provider} comes a little later</h3>
        <p className="mt-2 text-14 text-pretty text-muted">Gmail and most business mail work today. {found.provider} needs its own kind of password, and that part is still being built. Nothing was saved.</p>
        <Button className="mt-4" onClick={onAgain}>Use another address</Button>
      </div>
    );
  }
  if (found.result === 'needs_a_look') {
    return (
      <div className="mt-4 max-w-xl">
        <h3 className="text-20 font-semibold text-balance">Please look at these two names</h3>
        <p className="mt-2 text-14 text-pretty text-muted">We found them in public records that can be changed by anyone who gets at this domain, so we will not send a password there until a person says they look right.</p>
        <dl className="mt-4 divide-y divide-line border-y border-line">
          <HostLine label="Mail in" host={found.incoming ?? ''} odd={found.odd_part} />
          <HostLine label="Mail out" host={found.outgoing ?? ''} odd={found.odd_part} />
        </dl>
        {found.odd_part ? <p className="mt-3 flex items-start gap-2 text-13 text-pretty text-muted"><StateGlyph state="waiting" size={16} />This is not the same name as your address ({found.domain}). That can be fine, if this is the company that runs your mail. If you are not sure, ask whoever set up your email.</p> : null}
        {byHand ? <ByHand onDone={onHandOver} /> : (
          <div className="mt-4 flex items-center gap-2">
            <Button onClick={() => setByHand(true)}>These look wrong</Button>
            <Button variant="primary" onClick={() => onHandOver()}>Yes, these are right</Button>
          </div>
        )}
      </div>
    );
  }
  return null;
}

function Numbered({ number, children }: { number: number; children: ReactNode }) {
  return (
    <li className="flex gap-3 border-t border-line py-3 text-14 text-pretty first:border-t-0">
      <span className="tabular mt-px w-4 shrink-0 text-faint">{number}</span>
      <span className="min-w-0 flex-1">{children}</span>
    </li>
  );
}

function PasswordStep({ provider, go }: { provider: string; go: Go }) {
  const { copied, copy } = useCopy();
  return (
    <div className="max-w-xl">
      <p className="text-16 text-pretty text-muted">
        It is a separate password, 16 letters long, that you make for agentos. <span className="text-fg">Your normal {provider} password stays yours.</span> You can delete this one any time.
      </p>
      <ol className="mt-4 rounded-lg border border-line px-4">
        <Numbered number={1}>
          <span className="flex flex-wrap items-center justify-between gap-2">
            Open Google's app passwords page.
            <Button size="sm" asChild>
              <a href={GOOGLE_APP_PASSWORDS} target="_blank" rel="noreferrer">
                Open Google's page
                <ArrowUpRight aria-hidden="true" className="size-3" />
              </a>
            </Button>
          </span>
          <span className="mt-1 block text-13 text-muted">It opens in its own tab, on Google's own site. Google may ask you to sign in there.</span>
        </Numbered>
        <Numbered number={2}>
          <span className="flex flex-wrap items-center gap-2">
            Give it a name, such as
            <button type="button" onClick={() => void copy('agentos')} className="mono inline-flex h-6 items-center gap-1 rounded-sm bg-fill-2 px-2 text-12 hover:bg-fill-3">
              agentos
              {copied ? <CheckIcon aria-hidden="true" className="size-3" /> : <Copy aria-hidden="true" className="size-3 text-faint" />}
            </button>
            and press Create.
          </span>
        </Numbered>
        <Numbered number={3}>Google shows you 16 letters. Leave them on the screen. You will paste them in the next step.</Numbered>
      </ol>
      <div className="mt-4 flex flex-wrap items-center gap-3">
        <Button variant="primary" size="lg" onClick={() => go('hand-over')}>I have my 16 letters</Button>
        <button type="button" className="link text-13" onClick={() => go('failed', { ending: 'two-step-off' })}>Google says this is not available</button>
      </div>
      <div className="mt-6 grid gap-2">
        <Disclosure label="Why not my normal password?">
          Google does not let outside apps sign in with a normal password. An app password is a separate one you give to one app, and you can delete it on its own without changing anything else.
        </Disclosure>
        <Disclosure label="What is 2-Step Verification?">
          It means Google checks a second thing when someone signs in, usually a tap on your phone, so a stolen password alone is not enough. Google only offers app passwords once it is on. If your account uses Advanced Protection, Google blocks app passwords altogether.
        </Disclosure>
      </div>
    </div>
  );
}

function ComputerChoice({ computers, value, onChange }: { computers: MailComputer[]; value: string; onChange: (id: string) => void }) {
  return (
    <fieldset className="grid gap-2 sm:grid-cols-2">
      <legend className="sr-only">Which computer keeps this mailbox</legend>
      {computers.map((computer) => (
        <label key={computer.id} aria-label={`${computer.id}, ${computer.word}`} className={cn('flex cursor-pointer items-center gap-3 rounded-lg border p-3 transition-colors duration-(--duration-fast) ease-standard', value === computer.id ? 'border-fg' : 'border-line hover:border-line-strong')}>
          <input type="radio" name="computer" className="sr-only" checked={value === computer.id} onChange={() => onChange(computer.id)} />
          <span className="min-w-0">
            <span className="mono block truncate text-13">{computer.id}</span>
            <span className="block truncate text-12 text-faint">{computer.word}</span>
          </span>
        </label>
      ))}
    </fieldset>
  );
}

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

function HandOverStep({ org, found, computer, typed, go, onOpened }: { org: string; found: MailDiscovery; computer: MailComputer; typed: TypedServers | null; go: Go; onOpened: (form: OpenedForm | null) => void }) {
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
      {opener.error ? <p role="alert" className="mt-3 text-13 text-red">{describeError(opener.error)}</p> : null}
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
function WaitingStep({ org, found, computer, typed, form, go, onOpened }: { org: string; found: MailDiscovery; computer: MailComputer; typed: TypedServers | null; form: OpenedForm | null; go: Go; onOpened: (form: OpenedForm | null) => void }) {
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
        {opener.error ? <p role="alert" className="mt-3 text-13 text-red">{describeError(opener.error)}</p> : null}
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
      {opener.error ? <p role="alert" className="mt-3 text-13 text-red">{describeError(opener.error)}</p> : null}
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

function ChecksStep({ provider, read, send }: { provider: string; read: Check; send: Check }) {
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

function FailedStep({ ending, found, computer, go }: { ending: Ending; found: MailDiscovery; computer: string; go: Go }) {
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

function DoneStep({ org, address, provider, computer }: { org: string; address: string; provider: string; computer: string }) {
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

/** Concept only: walk to any state without having to fail for real. */
const WALK: { word: string; search: { address: string; stage?: ConnectStage; ending?: string } }[] = [
  { word: 'Gmail, from the start', search: { address: 'astercoffee.orders@gmail.example', stage: 'found' } },
  { word: 'Wrong password', search: { address: 'astercoffee.orders@gmail.example', stage: 'failed', ending: 'wrong-password' } },
  { word: '2-Step Verification off', search: { address: 'astercoffee.orders@gmail.example', stage: 'failed', ending: 'two-step-off' } },
  { word: 'Blocked by an admin', search: { address: 'sales@harbor-supply.example', stage: 'failed', ending: 'imap-blocked' } },
  { word: 'Servers not found', search: { address: 'hello@lindqvist-yachts.example', stage: 'found' } },
  { word: 'Certificate', search: { address: 'post@old-mill-post.example', stage: 'failed', ending: 'certificate' } },
  { word: 'Needs a person to look', search: { address: 'ola@nordlys-post.example', stage: 'found' } },
  { word: 'iCloud, later', search: { address: 'mia@icloud.example', stage: 'found' } },
];

function ConceptWalk({ org }: { org: string }) {
  return (
    <aside aria-label="Concept shortcuts" className="mt-12 border-t border-line pt-4">
      <p className="silk mb-2">Concept only: see another outcome</p>
      <div className="flex flex-wrap gap-1">
        {WALK.map((item) => (
          <Link key={item.word} to="/$org/email/connect" params={{ org }} search={item.search} className="rounded-md px-2 py-1 text-12 text-muted transition-colors duration-(--duration-fast) ease-standard hover:bg-fill-1 hover:text-fg">
            {item.word}
          </Link>
        ))}
      </div>
    </aside>
  );
}

const CAPTION: Partial<Record<ConnectStage, (computer: string, provider: string) => string>> = {
  found: (computer) => `${computer} is looking this up. Only the name after the @ leaves your computer.`,
  password: (computer, provider) => `You make the password at ${provider}. Next it goes to ${computer}, and from there to ${provider}.`,
  'hand-over': (computer, provider) => `It goes from you to ${computer}, then to ${provider}. That is the whole trip.`,
  waiting: (computer) => `Your password goes from you to ${computer} only when you press Connect on the form there.`,
  checks: (computer, provider) => `${computer} signs in to ${provider} twice: once to read, once to send.`,
  done: (computer) => `The password stays on ${computer}. agentos Cloud knows the mailbox is healthy, and that is all it knows.`,
};

/** A mailbox is connected from the computer that holds its password, so the cloud says so instead of a form. */
export function Connect() {
  const onComputer = useProviders().data?.code === true;
  if (!onComputer) return <Card className="p-4 sm:p-6"><NotLive /></Card>;
  return <ConnectHere />;
}

function ConnectHere() {
  const { org } = useParams({ from: '/$org/email/connect' });
  const search = useSearch({ from: '/$org/email/connect' });
  const navigate = useNavigate({ from: '/$org/email/connect' });
  const discover = useMailDiscover(org);
  const address = search.address ?? '';
  const stage: ConnectStage = address ? (search.stage ?? 'found') : 'address';
  const ending = asEnding(search.ending);
  const found = discover.data?.address === address ? discover.data : undefined;
  const asked = useRef('');
  const [read, setRead] = useState<Check>('waiting');
  const [send, setSend] = useState<Check>('waiting');
  // The server names a person typed, for the node's form to show and check; the form the panel opened, while it waits.
  const [typed, setTyped] = useState<TypedServers | null>(null);
  const [form, setForm] = useState<OpenedForm | null>(null);
  useEffect(() => {
    setTyped(null);
    setForm(null);
  }, [address]);

  const go = useMemo<Go>(
    () => (next, extra) => void navigate({ search: (old) => ({ ...old, stage: next, ending: extra?.ending, computer: extra?.computer ?? old.computer }), replace: extra?.replace }),
    [navigate],
  );

  useEffect(() => {
    if (!address || asked.current === address) return;
    asked.current = address;
    discover.mutate(address);
  }, [address, discover]);

  // A good answer moves on by itself, after a moment to read "Found Gmail".
  useEffect(() => {
    if (stage !== 'found' || found?.result !== 'found') return;
    const timer = setTimeout(() => go(found.password === 'app_password' ? 'password' : 'hand-over', { replace: true }), 2_100);
    return () => clearTimeout(timer);
  }, [stage, found, go]);

  // The two checks, one after the other. A failed check is its own stage, reached from the concept's list.
  useEffect(() => {
    if (stage !== 'checks') return;
    setRead('running');
    setSend('waiting');
    const first = setTimeout(() => {
      setRead('yes');
      setSend('running');
    }, 1_100);
    const second = setTimeout(() => setSend('yes'), 2_100);
    const last = setTimeout(() => go('done', { replace: true }), 2_900);
    return () => [first, second, last].forEach(clearTimeout);
  }, [stage, go]);

  const provider = found?.provider ?? null;
  const computer = found?.computers.find((item) => item.id === search.computer) ?? found?.computers[0] ?? { id: 'home-mini', word: '', has_screen: true };
  const failure = stage === 'failed' && ending && found ? endingWords(ending, provider ?? 'Your mail company', found.domain, computer.id) : null;
  const needsAppPassword = found?.password === 'app_password';
  const order: ConnectStage[] = ['found', 'password', 'hand-over', 'waiting', 'checks', 'done'];
  // A failure belongs to the step it happened in: 2-Step Verification comes up while making the password.
  const position = order.indexOf(stage === 'failed' ? (ending === 'two-step-off' ? 'password' : 'checks') : stage);
  const stateOf = (from: ConnectStage, to = from): 'done' | 'now' | 'later' => (position > order.indexOf(to) ? 'done' : position >= order.indexOf(from) ? 'now' : 'later');
  const keyAt: KeyAt = stage === 'done' ? 'kept' : stage === 'checks' || (stage === 'failed' && ending !== 'two-step-off') ? 'computer' : stage === 'hand-over' || stage === 'waiting' || stage === 'password' ? 'you' : 'none';
  const pictureRead = stage === 'done' ? 'yes' : stage === 'checks' ? read : (failure?.read ?? 'waiting');
  const pictureSend = stage === 'done' ? 'yes' : stage === 'checks' ? send : (failure?.send ?? 'waiting');
  const caption = failure?.caption ?? (found?.result === 'later' ? 'Nothing was sent anywhere, and nothing was saved.' : stage === 'found' && found && found.result !== 'found' ? 'Your password has not gone anywhere. It waits until the names are right.' : (CAPTION[stage]?.(computer.id, provider ?? 'your mail') ?? 'Your password will go from you to your own computer, and from there to your mail. Never through agentos Cloud.'));
  let number = 1;

  return (
    <div className="pb-16">
      <Link to="/$org/email" params={{ org }} className="mb-6 inline-flex items-center gap-1 text-13 text-muted hover:text-fg">
        <ArrowLeft className="size-3" /> Email
      </Link>
      <div className="grid gap-10 lg:grid-cols-[minmax(0,1fr)_26rem] lg:gap-16">
        <div className="min-w-0">
          {stage === 'address' ? (
            <AddressStep initial={address} onFind={(value) => void navigate({ search: { address: value, stage: 'found' } })} />
          ) : stage === 'done' ? (
            <DoneStep org={org} address={address} provider={provider ?? 'your mail company'} computer={computer.id} />
          ) : (
            <>
              <h1 className="mb-6 text-24 font-semibold break-all">Connecting {address}</h1>
              <ol className="spine">
                <Step
                  number={number++}
                  title={found?.result === 'found' && stage !== 'found' ? `Found ${provider}` : 'Finding your mail'}
                  state={stateOf('found')}
                  summary={found?.incoming ? <Disclosure label="Show the server names"><ServerNames found={found} /></Disclosure> : undefined}
                >
                  <Finding domain={address.split('@')[1] ?? ''} found={found} failed={discover.isError} />
                  {found ? (
                    <FoundStep
                      found={found}
                      onHandOver={(names) => {
                        setTyped(names ?? null);
                        go('hand-over');
                      }}
                      onAgain={() => void navigate({ search: {} })}
                    />
                  ) : null}
                </Step>
                {found?.result === 'later' ? null : needsAppPassword || !found ? (
                  <Step number={number++} title={`${provider ?? 'Your mail'} needs an app password`} state={stateOf('password')}>
                    {stage === 'failed' && ending === 'two-step-off' && found ? <FailedStep ending={ending} found={found} computer={computer.id} go={go} /> : <PasswordStep provider={provider ?? 'Gmail'} go={go} />}
                  </Step>
                ) : null}
                {found?.result === 'later' ? null : (
                  <>
                    <Step number={number++} title="Give it to your computer" state={stateOf('hand-over', 'waiting')} summary={`on ${computer.id}`}>
                      {found && stage === 'hand-over' ? <HandOverStep org={org} found={found} computer={computer} typed={typed} go={go} onOpened={setForm} /> : null}
                      {found && stage === 'waiting' ? <WaitingStep org={org} found={found} computer={computer} typed={typed} form={form} go={go} onOpened={setForm} /> : null}
                    </Step>
                    <Step number={number++} title="Two quick checks" state={stateOf('checks')}>
                      {stage === 'failed' && ending && found ? <FailedStep ending={ending} found={found} computer={computer.id} go={go} /> : <ChecksStep provider={provider ?? 'your mail company'} read={read} send={send} />}
                    </Step>
                  </>
                )}
              </ol>
            </>
          )}
          {apiMode === 'mock' ? <ConceptWalk org={org} /> : null}
        </div>
        <aside aria-label="Where your password goes" className="lg:sticky lg:top-6 lg:self-start">
          <div className="picture-stage registered rounded-(--radius-card) border border-line p-4">
            <p className="silk mb-3">Where your password goes</p>
            <RoutePicture computer={computer.id} provider={provider} site={found?.site ?? null} looking={stage === 'found' && !(found && found.result !== 'found')} keyAt={keyAt} read={pictureRead} send={pictureSend} />
          </div>
          <p aria-live="polite" className="mt-3 text-14 text-pretty text-muted">{caption}</p>
        </aside>
      </div>
    </div>
  );
}
