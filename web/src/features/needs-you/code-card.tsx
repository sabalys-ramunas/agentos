/**
 * A login waits at its code step. The site sent the owner a one-time code, and only the owner can type
 * it: the node types it into the site, and the code goes nowhere else. It is read from a plain field
 * once, at submit, and the field is cleared then. It is never state, a query, a toast or a log.
 */
import { KeyRound } from 'lucide-react';
import { useRef, useState } from 'react';
import type { FormEvent } from 'react';
import { toast } from 'sonner';
import { useDeclineCode, useEnterCode } from '@/api/hooks';
import type { CodeRequest } from '@/api/types';
import { AgentMark } from '@/components/brand/identity';
import { Button } from '@/components/ui/button';
import { Field, Input } from '@/components/ui/field';
import { describeError } from '@/lib/errors';
import { timeAgo } from '@/lib/format';
import { siteName } from '@/lib/sites';
import { Requester } from './approval-detail';
import { untilWords } from './words';
import { queueRow } from './queue-words';

/** The site's name, as a person knows it ("Instagram"), for a card's title. */
export const codeSite = (card: Pick<CodeRequest, 'site'>) => siteName(card.site);

/** A card in the list, weighted like a question: an icon, who asked, and when. */
export function CodeRow({ card, business, focused, onFocus }: { card: CodeRequest; business: string; focused: boolean; onFocus: () => void }) {
  return (
    <button
      type="button"
      onClick={onFocus}
      aria-current={focused || undefined}
      className={queueRow(focused)}
    >
      <span className="flex size-6 shrink-0 items-center justify-center rounded-sm border border-line bg-card text-muted shadow-lift">
        <KeyRound aria-hidden="true" className="size-3.5" />
      </span>
      <span className="grid min-w-0 flex-1 gap-1">
        <span className="truncate text-label">Login code for {codeSite(card)}</span>
        <span className="flex min-w-0 items-center gap-2 text-caption text-faint">
          <AgentMark principal={card.requested_by} size={16} />
          <span className="truncate">{[business, timeAgo(card.created_at)].filter(Boolean).join(' · ')}</span>
        </span>
      </span>
    </button>
  );
}

interface CodeDetailProps {
  org: string;
  card: CodeRequest;
  business: string;
  /** The card is answered or declined: the list moves on from it. */
  onLeave: () => void;
}

/** The code field, and the two answers: type the code, or say none came. Neither is a free text answer. */
export function CodeDetail({ org, card, business, onLeave }: CodeDetailProps) {
  const site = codeSite(card);
  const field = useRef<HTMLInputElement>(null);
  const enter = useEnterCode(org);
  const decline = useDeclineCode(org);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const send = async (event: FormEvent) => {
    event.preventDefault();
    const code = field.current?.value ?? '';
    // Cleared before anything is awaited, so the code never sits in the field once it has been sent.
    if (field.current) field.current.value = '';
    if (!code.trim()) {
      setError('Type the code the site sent you.');
      field.current?.focus();
      return;
    }
    setSending(true);
    setError(null);
    try {
      await enter(card.id, code);
      toast(`Code sent. agentos is typing it into ${site}.`);
      onLeave();
    } catch (cause) {
      // Said here, on the card, because the card is the place the person is looking.
      setError(describeError(cause));
      setSending(false);
      field.current?.focus();
    }
  };

  const noCode = () => {
    if (field.current) field.current.value = '';
    decline.mutate(card.id, {
      onSuccess: () => {
        toast('No code came. The login stops and waits for you.');
        onLeave();
      },
      onError: (cause) => setError(describeError(cause)),
    });
  };

  return (
    <div className="grid gap-4">
      <div>
        <p className="flex items-center gap-2 text-13 text-muted">
          <AgentMark principal={card.requested_by} size={20} />
          <span>
            <Requester org={org} principal={card.requested_by} /> is signing in to {site}
          </span>
        </p>
        <h2 className="mt-3 text-24 font-semibold break-words">Type the code {site} sent you</h2>
      </div>
      <p className="text-14 text-muted">
        {site} sent a code to your phone or app. Type it here once. agentos types it into the site, and the code is never stored or shown to the agent.
      </p>
      <form className="grid gap-4" autoComplete="off" onSubmit={(event) => void send(event)}>
        <Field label="Code" hint="4 to 12 letters or digits. Spaces and dashes are fine." error={error}>
          <Input
            ref={field}
            name="one-time-code"
            type="text"
            mono
            autoComplete="one-time-code"
            autoCapitalize="off"
            autoCorrect="off"
            spellCheck={false}
            maxLength={20}
          />
        </Field>
        <div className="flex flex-wrap justify-end gap-2">
          <Button variant="ghost" disabled={sending} loading={decline.isPending} onClick={noCode}>
            No code came
          </Button>
          <Button type="submit" variant="primary" loading={sending} disabled={decline.isPending}>
            Send code
          </Button>
        </div>
      </form>
      <p className="text-13 text-faint">
        {business ? `For ${business}. ` : ''}The login waits until {untilWords(card.expires_at)}, then it stops.
      </p>
    </div>
  );
}
