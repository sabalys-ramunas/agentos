/**
 * Concept: how a yes looks in a chat. A stand-alone mock with its own words, in a generic chat window
 * that imitates no product. The point of the page is the line between two voices: the agent can say
 * what it likes, and only agentos writes the question. "Who wrote what" draws that line on the page.
 */
import { Link, useParams } from '@tanstack/react-router';
import { ArrowUpRight, Check, Fingerprint, Lock, Paperclip, X } from 'lucide-react';
import type { ReactNode } from 'react';
import { useState } from 'react';
import { useConfirmation } from '@/api/hooks';
import { Button } from '@/components/ui/button';
import { Logo } from '@/components/ui/misc';
import { Switch } from '@/components/ui/toggle';
import { cn } from '@/lib/cn';
import './confirmations.css';

type Voice = 'you' | 'agent' | 'agentos';

const WROTE: Record<Exclude<Voice, 'you'>, string> = {
  agent: 'The agent wrote this',
  agentos: 'agentos wrote this',
};

const ADDRESS = 'https://app.agentos.example/northwind-group/review/ap_pay';

/** A part of the chat with its author. With the outlines on, the author is named on its edge. */
function Spoken({ voice, shown, name, children, className }: { voice: Voice; shown: boolean; name?: string; children: ReactNode; className?: string }) {
  const named = shown && voice !== 'you';
  return (
    <div className={cn('yes-voice', className)} data-outline={named ? voice : undefined}>
      {children}
      {named ? <span className={cn('yes-voice-name', voice === 'agentos' ? 'font-medium text-fg' : 'text-faint')}>{name ?? WROTE[voice]}</span> : null}
    </div>
  );
}

function Bubble({ from, children }: { from: 'you' | 'agent'; children: ReactNode }) {
  return <div className={cn('rounded-xl px-3 py-2 text-body text-pretty', from === 'you' && 'ml-auto w-fit max-w-[85%] bg-pressed')}>{children}</div>;
}

function ChatWindow({ title, children, footer, className }: { title: string; children: ReactNode; footer?: ReactNode; className?: string }) {
  return (
    <div className={cn('flex flex-col overflow-hidden rounded-xl border border-line bg-card', className)}>
      <div className="flex h-10 shrink-0 items-center gap-2 border-b border-line px-4">
        <span className="size-2 rounded-full bg-fill-strong" />
        <span className="text-secondary text-muted">{title}</span>
      </div>
      <div className="flex flex-1 flex-col gap-4 p-4">{children}</div>
      <div className="grid gap-2 p-4 pt-0">
        {footer}
        <div className="flex h-10 items-center gap-2 rounded-lg border border-line px-3 text-secondary text-faint">
          <Paperclip aria-hidden="true" className="size-4" />
          Message your agent
        </div>
      </div>
    </div>
  );
}

/** Drawn by the chat app, outside the conversation, so it can't be mistaken for a message. */
function Prompt({ shown, answer, onAnswer }: { shown: boolean; answer: 'yes' | 'no' | null; onAnswer: (value: 'yes' | 'no') => void }) {
  if (answer) {
    return (
      <div role="status" className="flex animate-fade-in items-center gap-2 rounded-lg border border-line bg-inset px-3 py-2 text-secondary">
        {answer === 'yes' ? <Check aria-hidden="true" className="size-4 text-ok" /> : <X aria-hidden="true" className="size-4 text-muted" />}
        <span className="min-w-0">{answer === 'yes' ? 'You said yes in this chat. Just a tap. It is in History.' : 'You said no. Nothing is posted.'}</span>
      </div>
    );
  }
  return (
    <Spoken voice="agentos" shown={shown}>
      <section aria-label="A question from agentos" className="rounded-lg border border-line-strong bg-raised shadow-popover">
        <header className="flex items-center gap-2 border-b border-line px-3 py-2">
          <span className="text-secondary font-medium">agentos asks</span>
          <span className="ml-auto flex items-center gap-1 text-caption text-faint">
            <Lock aria-hidden="true" className="size-3" />
            Not from the agent
          </span>
        </header>
        <div className="grid gap-2 p-3">
          <p className="text-label">Post this photo on Aster Coffee&apos;s Instagram?</p>
          <blockquote className="border-l-2 border-line-strong pl-2 text-secondary text-pretty text-muted">Our autumn blend is back. Washed Ethiopian with a little dried plum on the finish. Roasted Tuesday, on shelves Thursday.</blockquote>
          <p className="text-caption text-faint">As @astercoffee, seen by 4,812 followers. You can delete it later.</p>
        </div>
        <div className="grid grid-cols-2 gap-2 p-3 pt-0">
          <Button onClick={() => onAnswer('no')}>Don&apos;t post</Button>
          <Button variant="primary" onClick={() => onAnswer('yes')}>
            Post it
          </Button>
        </div>
      </section>
    </Spoken>
  );
}

function PromptForm({ shown }: { shown: boolean }) {
  const [answer, setAnswer] = useState<'yes' | 'no' | null>(null);
  return (
    <ChatWindow title="Your chat app" className="h-full" footer={<Prompt shown={shown} answer={answer} onAnswer={setAnswer} />}>
      <Spoken voice="you" shown={shown}>
        <Bubble from="you">Post the autumn blend photo on Aster&apos;s Instagram.</Bubble>
      </Spoken>
      <Spoken voice="agent" shown={shown}>
        <Bubble from="agent">It&apos;s ready. agentos wants a person to say yes first, so it is asking you now.</Bubble>
      </Spoken>
      {answer === 'yes' ? (
        <Spoken voice="agent" shown={shown} className="animate-fade-in">
          <Bubble from="agent">Posted. It is live on @astercoffee.</Bubble>
        </Spoken>
      ) : null}
      {answer === 'no' ? (
        <Spoken voice="agent" shown={shown} className="animate-fade-in">
          <Bubble from="agent">Understood. I won&apos;t post it.</Bubble>
        </Spoken>
      ) : null}
    </ChatWindow>
  );
}

function ReviewForm({ shown, href }: { shown: boolean; href: string | null }) {
  return (
    <div className="grid h-full grid-rows-[auto_40px_minmax(0,1fr)]">
      <ChatWindow title="Your chat app">
        <Spoken voice="you" shown={shown}>
          <Bubble from="you">Pay the Nordic Ropeworks invoice.</Bubble>
        </Spoken>
        <Spoken voice="agent" shown={shown}>
          <Bubble from="agent">It&apos;s ready. Money needs you to prove it&apos;s you, and a chat can&apos;t do that. Open this to review it:</Bubble>
        </Spoken>
        <Spoken voice="agentos" shown={shown} name="agentos made this link" className="px-3 py-2">
          <a href={href ?? undefined} className="link mono block truncate text-secondary">
            {ADDRESS}
          </a>
        </Spoken>
      </ChatWindow>
      <div aria-hidden="true" className="relative flex items-center justify-center">
        <span className="absolute inset-y-0 left-1/2 w-px bg-line-strong" />
        <span className="relative bg-card px-2 text-caption text-faint">opens the panel, signed in</span>
      </div>
      <a href={href ?? undefined} className="group flex flex-col overflow-hidden rounded-xl border border-line bg-card transition-colors duration-(--duration-fast) ease-standard hover:border-control">
        <span className="flex h-10 shrink-0 items-center gap-2 border-b border-line px-4 text-secondary text-muted">
          <Logo className="size-4" />
          Review
          <ArrowUpRight aria-hidden="true" className="ml-auto size-4 text-faint transition-transform duration-(--duration-fast) ease-standard group-hover:translate-x-px group-hover:-translate-y-px" />
        </span>
        <span className="grid flex-1 content-center gap-3 p-4">
          <span className="block">
            <span className="block text-heading">Pay €412.50 to Nordic Ropeworks?</span>
            <span className="mt-1 block text-secondary text-muted">Harbor Supply, from the account ending 4471, reference INV-4471.</span>
          </span>
          <span className="flex items-center gap-3 rounded-lg border border-line-strong p-3">
            <Fingerprint aria-hidden="true" className="size-5 shrink-0" />
            <span className="min-w-0 text-secondary">
              <span className="block font-medium">Prove it&apos;s you to say yes</span>
              <span className="block text-muted">No Mac, iPhone or app needed.</span>
            </span>
          </span>
        </span>
      </a>
    </div>
  );
}

const RULES: [string, string][] = [
  ['The agent never holds the pen.', 'The question is written from the request itself, the same way a card in Needs you is. Nothing the agent writes reaches it.'],
  ['The agent has no button.', 'The answer goes from your chat app to agentos. The agent only hears how it came out.'],
  ['A link never says yes.', 'It only opens the page. You decide there, signed in, with as much proof as it needs. Check that it is your own agentos address.'],
  ['No is always welcome.', 'You can say no from any chat, whatever the setting says.'],
];

export function YesInChat() {
  const { org } = useParams({ from: '/$org/concept/yes-in-chat' });
  const [shown, setShown] = useState(true);
  const href = useConfirmation(org).data?.sample_review ?? null;
  return (
    <main className="mx-auto grid max-w-6xl gap-8 p-4 md:p-10">
      <header className="grid gap-4">
        <p className="flex items-center gap-2 text-secondary text-muted">
          <Logo className="size-5" />
          Concept, on mock data
        </p>
        <div className="flex flex-wrap items-end justify-between gap-x-8 gap-y-4">
          <div className="min-w-0">
            <h1 className="text-headline">A yes in chat</h1>
            <p className="mt-2 max-w-[60ch] text-body text-pretty text-muted">For anyone with no Mac or iPhone, and anyone already talking to an agent. The agent asks agentos, and agentos asks you.</p>
          </div>
          <span className="flex shrink-0 items-center gap-2 text-secondary text-muted">
            <span aria-hidden="true">Show who wrote what</span>
            <Switch aria-label="Show who wrote what" checked={shown} onCheckedChange={setShown} />
          </span>
        </div>
      </header>
      <div className="grid gap-6 lg:grid-cols-2">
        <section aria-label="When the chat app can show a prompt" className="grid min-w-0 grid-rows-[auto_minmax(0,1fr)] gap-3">
          <div>
            <h2 className="text-section-title">When the chat app can ask you</h2>
            <p className="mt-1 text-secondary text-muted">A post, on a day it asks. How it looks is up to each chat app. The words are always agentos&apos;s own, outside the conversation.</p>
          </div>
          <PromptForm shown={shown} />
        </section>
        <section aria-label="When it needs more than a chat" className="grid min-w-0 grid-rows-[auto_minmax(0,1fr)] gap-3">
          <div>
            <h2 className="text-section-title">When it needs more than a chat</h2>
            <p className="mt-1 text-secondary text-muted">A payment. The same goes for a chat app that can&apos;t show a prompt: agentos hands over a link instead.</p>
          </div>
          <ReviewForm shown={shown} href={href} />
        </section>
      </div>
      <ul className="grid gap-x-8 gap-y-4 border-t border-line pt-6 sm:grid-cols-2 lg:grid-cols-4">
        {RULES.map(([title, detail]) => (
          <li key={title}>
            <p className="text-label">{title}</p>
            <p className="mt-1 text-secondary text-pretty text-muted">{detail}</p>
          </li>
        ))}
      </ul>
      <p className="text-secondary text-muted">
        <Link to="/$org/settings/yes" params={{ org }} className="link">
          Back to What needs a yes
        </Link>
      </p>
    </main>
  );
}
