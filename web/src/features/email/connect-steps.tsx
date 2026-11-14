import { ArrowUpRight, Check as CheckIcon, ChevronDown, Copy } from 'lucide-react';
import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import type { MailComputer, MailDiscovery } from '@/api/types';
import { SiteMark } from '@/components/brand/site-mark';
import { Button } from '@/components/ui/button';
import { Field, Input } from '@/components/ui/field';
import { useCopy } from '@/components/ui/misc';
import { Spinner } from '@/components/ui/spinner';
import { StateGlyph } from '@/components/ui/state-badge';
import { cn } from '@/lib/cn';
import { GOOGLE_APP_PASSWORDS, hintFor, looksLikeAddress } from './words';
import type { ConnectStage } from './words';

/** The two server names a person typed. The node's form shows them again to check, with their ports. */
export interface TypedServers {
  incoming: string;
  outgoing: string;
}

/** The node's form for this mailbox, as the panel holds it while it waits: its token and when it closes. */
export interface OpenedForm {
  token: string | null;
  expires: string | null;
}

export type Go = (stage: ConnectStage, extra?: { ending?: string; computer?: string; replace?: boolean }) => void;

/** One step down the spine. Done steps fold to a line, so the whole way stays in view. */
export function Step({ number, title, state, summary, children }: { number: number; title: string; state: 'done' | 'now' | 'later'; summary?: ReactNode; children?: ReactNode }) {
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

export function Disclosure({ label, children }: { label: string; children: ReactNode }) {
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

export function ServerNames({ found }: { found: MailDiscovery }) {
  return (
    <dl className="grid gap-1">
      <div className="flex gap-2"><dt className="w-24 shrink-0 text-faint">Mail in</dt><dd className="mono min-w-0 break-all text-12">{found.incoming}</dd></div>
      <div className="flex gap-2"><dt className="w-24 shrink-0 text-faint">Mail out</dt><dd className="mono min-w-0 break-all text-12">{found.outgoing}</dd></div>
      <div className="flex gap-2"><dt className="w-24 shrink-0 text-faint">Found in</dt><dd className="min-w-0">{found.source}</dd></div>
    </dl>
  );
}

/** The address, and nothing else on the page asks for attention. */
export function AddressStep({ initial, onFind }: { initial: string; onFind: (address: string) => void }) {
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
export function Finding({ domain, found, failed }: { domain: string; found: MailDiscovery | undefined; failed: boolean }) {
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

export function FoundStep({ found, onHandOver, onAgain }: { found: MailDiscovery; onHandOver: (typed?: TypedServers) => void; onAgain: () => void }) {
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

export function PasswordStep({ provider, go }: { provider: string; go: Go }) {
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

export function ComputerChoice({ computers, value, onChange }: { computers: MailComputer[]; value: string; onChange: (id: string) => void }) {
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
