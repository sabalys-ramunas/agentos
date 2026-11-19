import { useEffect, useState } from 'react';
import type { Approval, Principal, Question } from '@/api/types';
import { AgentMark } from '@/components/brand/identity';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/field';
import { Kbd } from '@/components/ui/misc';
import { StatusDot } from '@/components/ui/status';
import { useTechnical } from '@/lib/technical';
import { usePrincipalName } from '@/lib/use-principal';
import { WorkerStopped } from '@/features/workers/in-place';
import { untilWords } from './words';

/** Who asked, in words. The thread id is for the technical, so it shows only with that switch on. */
export function Requester({ org, principal }: { org: string; principal: Principal }) {
  const nameOf = usePrincipalName(org);
  const [technical] = useTechnical();
  return (
    <span>
      {nameOf(principal)}
      {technical && principal.thread ? <span className="mono text-faint"> · {principal.thread}</span> : null}
    </span>
  );
}

/**
 * A decision the node did not take. It is still waiting there, so this never reads as done.
 */
export function RefusedDelivery({ approval }: { approval: Approval }) {
  if (approval.delivery !== 'refused') return null;
  const answer = approval.state === 'denied' ? 'no' : 'yes';
  return (
    <p role="status" className="flex items-baseline gap-2 rounded-lg border border-line bg-inset px-4 py-3 text-body">
      <StatusDot tone="bad" className="translate-y-px" />
      <span className="min-w-0">
        <span className="font-medium">The computer did not take your {answer}.</span>{' '}
        <span className="text-muted">It still waits there, and nothing was done.</span>
      </span>
    </p>
  );
}

interface QuestionDetailProps {
  org: string;
  question: Question;
  canAnswer: boolean;
  onAnswer: (answer: string) => void;
}

/** The agent is waiting on a person. Node text stays plain text, like everything from a node. */
export function QuestionDetail({ org, question, canAnswer, onAnswer }: QuestionDetailProps) {
  const [text, setText] = useState('');
  useEffect(() => setText(''), [question.id]);
  const nameOf = usePrincipalName(org);
  // The signed-in person's own question reads "Asked by you", never "you asks you".
  const mine = nameOf(question.asked_by, { inSentence: true }) === 'you';
  const send = (event: React.FormEvent) => {
    event.preventDefault();
    if (text.trim()) onAnswer(text.trim());
  };
  return (
    <div className="grid gap-4">
      <div>
        <p className="flex items-center gap-2 text-secondary text-muted">
          <AgentMark principal={question.asked_by} size={20} />
          <span>{mine ? 'Asked by you' : <><Requester org={org} principal={question.asked_by} /> asks you</>}</span>
        </p>
        <h2 className="mt-3 text-page-title-compact break-words">{question.question}</h2>
      </div>
      {question.worker ? <WorkerStopped thing={question.worker} /> : null}
      {!canAnswer ? (
        <p className="text-secondary text-faint">Your role can see this but not answer it.</p>
      ) : question.options?.length ? (
        <ul className="grid gap-2">
          {question.options.map((option, index) => (
            <li key={option}>
              <button
                type="button"
                onClick={() => onAnswer(option)}
                className="flex min-h-12 w-full items-center gap-2 rounded-xl border border-line px-4 py-2 text-left text-lede transition-colors duration-(--duration-fast) ease-standard hover:border-line-strong hover:bg-hover active:bg-pressed"
              >
                <span className="min-w-0 flex-1 break-words">{option}</span>
                {index < 9 ? (
                  <Kbd aria-hidden="true" className="hidden lg:inline-flex">
                    {index + 1}
                  </Kbd>
                ) : null}
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <form onSubmit={send} className="grid gap-2">
          <Textarea
            aria-label="Your answer"
            value={text}
            onChange={(event) => setText(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) send(event);
            }}
          />
          <Button type="submit" variant="primary" disabled={!text.trim()} className="justify-self-end">
            Send answer
          </Button>
        </form>
      )}
      <p className="text-secondary text-faint">
        <Requester org={org} principal={question.asked_by} /> waits for your answer until {untilWords(question.expires_at)}.
      </p>
    </div>
  );
}

export function ChangedNotice() {
  return (
    <p role="status" className="flex items-baseline gap-2 rounded-lg border border-line bg-inset px-4 py-3 text-body">
      <StatusDot tone="warn" className="translate-y-px" />
      <span className="min-w-0">
        <span className="font-medium">This changed since you saw it.</span> <span className="text-muted">Nothing was decided. Read it again before you answer.</span>
      </span>
    </p>
  );
}
