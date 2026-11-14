/**
 * A read-only link from Proof: one account's day, or one step, with no sign-in and no app around
 * it (docs/API.md, "Audit"). The token sits in the fragment, so it never reaches a server log. The
 * page shows what the link allows and nothing else: no arguments, no account ids, no controls.
 */
import { useState } from 'react';
import { proof } from '@/api/client';
import { ApiError } from '@/api/http';
import { useProofShared } from '@/api/hooks';
import type { ProofShared, ProofStep } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Status } from '@/components/ui/status';
import { dateTime, plural, utcClock } from '@/lib/format';
import { laneName, runStatus } from '@/lib/labels';
import { accountOf } from '@/features/activity/proof/share-dialog';
import { CHAIN_WORDS, chainSentence, dayTitle } from '@/features/activity/proof/words';

/** The token from `#token=...`, or nothing. */
export function tokenFromHash(hash: string): string {
  const match = /(?:^#|&)token=([^&]+)/.exec(hash);
  if (!match) return '';
  try {
    return decodeURIComponent(match[1]!);
  } catch {
    return '';
  }
}


function StepLine({ step, token }: { step: ProofStep; token: string }) {
  const chain = CHAIN_WORDS[step.chain.status];
  const result = step.status in runStatus ? runStatus[step.status as keyof typeof runStatus] : { label: step.status, tone: 'neutral' as const };
  return (
    <li className="grid gap-2 border-b border-line py-4 last:border-b-0">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <p className="min-w-0 text-14">{step.summary || 'A step with no words'}</p>
        <span className="tabular text-13 whitespace-nowrap text-faint">{utcClock(step.at)}</span>
      </div>
      <p className="flex flex-wrap items-center gap-x-4 gap-y-1 text-13 text-muted">
        <Status status={result} />
        <Status status={chain} />
        {step.grant?.label ? <span className="min-w-0">{step.grant.label}</span> : null}
      </p>
      {step.error ? <p className="text-13 text-red">{step.error}</p> : null}
      {step.screenshot && step.run ? (
        <img
          src={proof.sharedScreenshot(token, step.run.id)}
          alt={`Proof screenshot of ${step.summary || 'a step'}`}
          loading="lazy"
          className="mt-1 w-full max-w-[560px] rounded-lg border border-line bg-subtle"
        />
      ) : null}
    </li>
  );
}

function Shared({ data, token }: { data: ProofShared; token: string }) {
  const until = dateTime(data.share.expires_at);
  if ('trace' in data) {
    const account = laneName(accountOf(data.lane ?? ''));
    return (
      <section className="grid gap-4">
        <h1 className="text-24 font-semibold">One step on {account}</h1>
        <p className="text-14 text-muted">Read-only. The owner shared this step with you. It works until {until}.</p>
        <p className="text-13 text-muted">{chainSentence(data.chain)}</p>
        <ul className="border-t border-line">
          <StepLine step={data.step} token={token} />
        </ul>
      </section>
    );
  }
  const account = laneName(accountOf(data.lane));
  return (
    <section className="grid gap-4">
      <h1 className="text-24 font-semibold">
        What {account} did on {dayTitle(data.day)}
      </h1>
      <p className="text-14 text-muted">
        Read-only. The owner shared this day with you. It works until {until}.
      </p>
      <p className="text-13 text-muted">{chainSentence(data.chain)}</p>
      {data.steps.length === 0 ? (
        <p className="border-t border-line py-6 text-14 text-muted">Nothing was kept for this day.</p>
      ) : (
        <>
          <p className="text-13 text-faint">{plural(data.steps.length, 'step')}, oldest first</p>
          <ul className="border-t border-line">
            {data.steps.map((step) => (
              <StepLine key={step.id} step={step} token={token} />
            ))}
          </ul>
        </>
      )}
    </section>
  );
}

export function SharedPage() {
  const [token] = useState(() => tokenFromHash(window.location.hash));
  const shared = useProofShared(token);
  const gone = shared.error instanceof ApiError && shared.error.status === 404;

  return (
    <main className="mx-auto grid w-full max-w-[720px] content-start gap-8 px-4 py-10 md:px-10 md:py-16">
      <p className="text-13 text-faint">agentos, read-only</p>
      {!token || gone ? (
        <section className="grid gap-2">
          <h1 className="text-20 font-semibold">This link is not valid</h1>
          <p className="text-14 text-muted">It may have ended or been stopped. Ask the person who sent it for a new one.</p>
        </section>
      ) : shared.isPending ? (
        <div role="status" aria-label="Loading" className="grid gap-3">
          <div className="h-6 w-2/3 animate-pulse rounded bg-fill-1" />
          <div className="h-4 w-1/2 animate-pulse rounded bg-fill-1" />
        </div>
      ) : shared.isError ? (
        <section className="grid justify-items-start gap-3">
          <h1 className="text-20 font-semibold">This link could not be opened</h1>
          <p className="text-14 text-muted">Nothing was changed. Check your connection, then try again.</p>
          <Button onClick={() => void shared.refetch()}>Try again</Button>
        </section>
      ) : (
        <Shared data={shared.data} token={token} />
      )}
    </main>
  );
}
