/**
 * An agent asks to work on a private repository its computer cannot read. The owner gives a token for it once, or
 * an admin of the repository adds the computer's deploy key. The node checks either with GitHub before it keeps
 * anything, and the agent never sees the token.
 *
 * The app never holds the token (docs/WEB-NODE.md): "Paste a token" opens the computer's own page for it in a new
 * tab, and the card waits on that page's trip until it says done.
 */
import { ArrowUpRight, Check, ExternalLink, GitBranch } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { useCheckRepo, useDeclineRepo, useDropQuestion, useFormTrip, useRepoDeployKey, useRepoTokenForm } from '@/api/hooks';
import type { Question, RepoConnect, RepoConnected } from '@/api/types';
import { AgentMark } from '@/components/brand/identity';
import { Button } from '@/components/ui/button';
import { CodeBlock } from '@/components/ui/misc';
import { Spinner } from '@/components/ui/spinner';
import { describeError } from '@/lib/errors';
import { date } from '@/lib/format';
import { Requester } from './approval-detail';
import { untilWords } from './words';
import { InlineError } from '@/components/ui/states';

const hostName = (card: RepoConnect) => (card.host === 'github.com' ? 'GitHub' : card.host);


function Outside({ href, children }: { href: string; children: ReactNode }) {
  return (
    <a href={href} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 link hover:underline">
      {children}
      <ExternalLink aria-hidden="true" className="size-3" />
    </a>
  );
}

interface RepoConnectDetailProps {
  org: string;
  question: Question & { repo_connect: RepoConnect };
  owner: boolean;
  /** The card is connected or declined, and the person is done with it: the list moves on. */
  onLeave: () => void;
}

export function RepoConnectDetail({ org, question, owner, onLeave }: RepoConnectDetailProps) {
  const card = question.repo_connect;
  const [way, setWay] = useState<'token' | 'key'>('token');
  const [done, setDone] = useState<RepoConnected | null>(null);
  const drop = useDropQuestion(org);
  const leave = () => {
    drop(question.id);
    onLeave();
  };

  return (
    <div className="grid gap-4">
      <div>
        <p className="flex items-center gap-2 text-secondary text-muted">
          <AgentMark principal={question.asked_by} size={20} />
          <span>
            <Requester org={org} principal={question.asked_by} /> wants to work on a private repository
          </span>
        </p>
        <h2 className="mt-3 flex items-start gap-2 text-page-title-compact break-words">
          <GitBranch aria-hidden="true" className="mt-1 size-5 shrink-0 text-muted" />
          <span className="min-w-0 [overflow-wrap:anywhere]">{card.reason === 'refused' ? `Give a new token for ${card.repo}` : `Connect ${card.repo}`}</span>
        </h2>
      </div>
      {done ? (
        <Connected done={done} onLeave={leave} />
      ) : !owner ? (
        <p className="text-body text-muted">Only the owner of this computer can connect a repository.</p>
      ) : (
        <>
          <p className="text-body text-muted">
            {card.reason === 'refused' ? 'The token this computer had for it stopped working. ' : ''}
            Its computer cannot read this repository yet. Paste a token from your own {hostName(card)} account, on a page this computer serves: you do not need to be an admin of the repository. openajar checks the token, then keeps it on this computer only. The agent never sees it.
          </p>
          {way === 'token' ? (
            <TokenStep org={org} id={question.id} card={card} onDone={setDone} onDeclined={leave} onKey={() => setWay('key')} />
          ) : (
            <DeployKey org={org} id={question.id} card={card} onDone={setDone} onToken={() => setWay('token')} />
          )}
        </>
      )}
      {done ? null : (
        <p className="text-secondary text-faint">
          <Requester org={org} principal={question.asked_by} /> waits until {untilWords(question.expires_at)}. Nothing is rented while it waits.
        </p>
      )}
    </div>
  );
}

/** The card's token page opens in a new tab, on the computer. The app only learns how far it got. */
function TokenStep({ org, id, card, onDone, onDeclined, onKey }: { org: string; id: string; card: RepoConnect; onDone: (done: RepoConnected) => void; onDeclined: () => void; onKey: () => void }) {
  const link = useRepoTokenForm(org);
  const decline = useDeclineRepo(org);
  const [trip, setTrip] = useState<{ id: string; url: string } | null>(null);
  const status = useFormTrip(org, trip?.id);
  const stage = trip ? (status.data?.stage ?? 'waiting') : null;
  const [declineError, setDeclineError] = useState<string | null>(null);
  const finished = stage === 'done' ? status.data : null;

  useEffect(() => {
    if (!finished) return;
    onDone({ repo: finished.outcome?.repo ?? card.repo, connected: true, expires_at: finished.outcome?.token_expires_at ?? null });
  }, [finished, card.repo, onDone]);

  // The page opens once the computer has made its link. The press may first ask the owner to confirm it is them.
  const open = () =>
    link.mutate(id, {
      onSuccess: (made) => {
        if (!made.form_url) return;
        setTrip({ id: made.id, url: made.form_url });
        window.open(made.form_url, '_blank', 'noopener');
      },
    });

  const notNow = () => decline.mutate(id, { onSuccess: onDeclined, onError: (cause) => setDeclineError(describeError(cause)) });
  const waiting = stage === 'waiting' && trip !== null;

  return (
    <div className="grid gap-4">
      <p className="text-secondary text-muted">
        A classic token needs the repo scope. A fine-grained token needs this repository, with Contents: Read (Read and write to push).
      </p>
      {card.token_url ? (
        <p className="flex flex-wrap gap-x-3 gap-y-1 text-secondary">
          <Outside href={card.token_url}>Make a fine-grained token</Outside>
          {card.classic_token_url ? <Outside href={card.classic_token_url}>or a classic one</Outside> : null}
        </p>
      ) : null}
      {waiting ? (
        <div className="grid gap-2">
          <p role="status" className="flex items-center gap-2 text-body">
            <Spinner className="size-4" />
            Paste the token on this computer&apos;s own page, in a new tab. This card moves on by itself.
          </p>
          <p className="text-secondary">
            <Outside href={trip.url}>Open the token page</Outside>
          </p>
        </div>
      ) : null}
      {stage === 'ended' ? (
        <InlineError>
          That page closed before a token was connected. Nothing was kept. Open a new one.
        </InlineError>
      ) : null}
      {link.isError ? (
        <InlineError>
          {describeError(link.error)}
        </InlineError>
      ) : null}
      {declineError ? (
        <InlineError>
          {declineError}
        </InlineError>
      ) : null}
      <div className="flex flex-wrap items-center justify-end gap-2">
        <Button variant="ghost" className="mr-auto" disabled={decline.isPending} onClick={onKey}>
          Use a deploy key instead (repo admins)
        </Button>
        <Button variant="ghost" loading={decline.isPending} onClick={notNow}>
          Not now
        </Button>
        {waiting ? null : (
          <Button variant="primary" loading={link.isPending} disabled={decline.isPending} onClick={open}>
            Paste a token
            <ArrowUpRight aria-hidden="true" className="size-4" />
          </Button>
        )}
      </div>
    </div>
  );
}

function DeployKey({ org, id, card, onDone, onToken }: { org: string; id: string; card: RepoConnect; onDone: (done: RepoConnected) => void; onToken: () => void }) {
  const key = useRepoDeployKey(org);
  const check = useCheckRepo(org);
  const settings = key.data?.settings_url ?? card.deploy_keys_url;
  const { mutate } = key;
  const asked = useRef(false);
  // Choosing a deploy key is the press that makes one: the node keeps one per repository, so a second ask shows the same.
  useEffect(() => {
    if (asked.current) return;
    asked.current = true;
    mutate(id);
  }, [id, mutate]);

  return (
    <div className="grid gap-4">
      <p className="text-body text-muted">
        An admin of {card.repo} adds this key to the repository, with Allow write access ticked so agents can push. Then press check.
      </p>
      {key.isError ? (
        <InlineError>
          {describeError(key.error)}
        </InlineError>
      ) : key.data ? (
        <div className="grid gap-2">
          <CodeBlock code={key.data.public_key} label="Copy key" />
          <p className="font-mono text-code-caption text-faint [overflow-wrap:anywhere]">{key.data.fingerprint}</p>
        </div>
      ) : (
        <p role="status" className="text-secondary text-muted">
          Making the key on this computer.
        </p>
      )}
      {settings?.startsWith('https://') ? (
        <p className="text-secondary">
          <Outside href={settings}>Open the repository&apos;s deploy keys</Outside>
        </p>
      ) : null}
      {check.isError ? (
        <InlineError>
          {describeError(check.error)}
        </InlineError>
      ) : null}
      <div className="flex flex-wrap items-center justify-end gap-2">
        <Button variant="ghost" className="mr-auto" disabled={check.isPending} onClick={onToken}>
          Use a token instead
        </Button>
        <Button variant="primary" loading={check.isPending} disabled={!key.data} onClick={() => check.mutate(id, { onSuccess: onDone })}>
          I added it, check
        </Button>
      </div>
    </div>
  );
}

function Connected({ done, onLeave }: { done: RepoConnected; onLeave: () => void }) {
  return (
    <div className="grid gap-4">
      <p role="status" className="flex items-start gap-2 text-body">
        <Check aria-hidden="true" className="mt-1 size-4 shrink-0 text-ok" />
        <span>Connected. The agent can open {done.repo} now.</span>
      </p>
      {done.expires_at ? <p className="text-secondary text-muted">GitHub says this token expires on {date(done.expires_at)}. Make a new one before then.</p> : null}
      <div className="flex justify-end">
        <Button variant="primary" onClick={onLeave}>
          Done
        </Button>
      </div>
    </div>
  );
}
