/**
 * One entry in full, in a drawer. Its title and text are the node's words, so they are shown as text and
 * never as markup or a link. The owner confirms, marks as looked at, or forgets it. Forgetting erases the
 * text for good, so it is typed and confirmed, and it asks for the reason the record keeps.
 */
import { useState } from 'react';
import { toast } from 'sonner';
import { useConfirmMemory, useForgetMemory, useMemoryEntry, useReviewMemory } from '@/api/hooks';
import type { MemoryDetail } from '@/api/types';
import { Button } from '@/components/ui/button';
import { ConfirmDialog, Drawer } from '@/components/ui/dialog';
import { Field, Textarea } from '@/components/ui/field';
import { Detail } from '@/components/ui/misc';
import { InlineError, QueryView, Skeleton } from '@/components/ui/states';
import { Status, StatusDot } from '@/components/ui/status';
import { describeError } from '@/lib/errors';
import { dateTime, timeAgo } from '@/lib/format';
import { memoryState, memoryTrust, memoryType } from '@/lib/labels';
import { useTechnical } from '@/lib/technical';
import { FORGET_HINT, SOURCE_SENTENCE, waitsForLook } from './memory-words';

export interface EntryTarget {
  id: string;
  workspace: string;
}

export function EntrySheet({ org, target, workspaceName, onClose }: { org: string; target: EntryTarget | null; workspaceName: (id: string) => string; onClose: () => void }) {
  return (
    <Drawer open={target !== null} onOpenChange={(open) => !open && onClose()} title="Memory entry" description={target ? workspaceName(target.workspace) : undefined}>
      {target ? <EntryBody org={org} target={target} workspaceName={workspaceName} /> : null}
    </Drawer>
  );
}

function EntryBody({ org, target, workspaceName }: { org: string; target: EntryTarget; workspaceName: (id: string) => string }) {
  const query = useMemoryEntry(org, target.id, target.workspace);
  return (
    <QueryView query={query} skeleton={<EntrySkeleton />}>
      {(entry) => <EntryView org={org} target={target} entry={entry} workspaceName={workspaceName} />}
    </QueryView>
  );
}

function EntrySkeleton() {
  return (
    <div className="grid gap-4">
      <Skeleton className="h-6 w-2/3" />
      <Skeleton className="h-4 w-1/2" />
      <Skeleton className="h-24 w-full" />
    </div>
  );
}

function EntryView({ org, target, entry, workspaceName }: { org: string; target: EntryTarget; entry: MemoryDetail; workspaceName: (id: string) => string }) {
  const [technical] = useTechnical();
  const confirm = useConfirmMemory(org);
  const review = useReviewMemory(org);
  const forget = useForgetMemory(org);
  const [forgetting, setForgetting] = useState(false);
  const [reason, setReason] = useState('');
  const live = entry.state === 'live';
  // The detail carries the time it was reviewed (null until then), which is what waiting turns on.
  const waiting = waitsForLook({ trust: entry.trust, state: entry.state, reviewed: entry.reviewed_at !== null });
  const hidden = live && entry.body === null;
  const failure = confirm.error ?? review.error ?? forget.error;

  const doConfirm = () =>
    confirm.mutate(
      { id: entry.id, workspace: target.workspace, version: entry.version },
      { onSuccess: () => toast('Confirmed. It is kept as yours now.') },
    );
  const doReview = () =>
    review.mutate({ id: entry.id, workspace: target.workspace }, { onSuccess: () => toast('Marked as looked at. It is not confirmed.') });
  const doForget = () =>
    forget.mutate(
      { id: entry.id, workspace: target.workspace, reason: reason.trim() },
      {
        onSuccess: () => {
          setForgetting(false);
          setReason('');
          toast('Forgotten. Its title and text are gone.');
        },
      },
    );

  return (
    <div className="grid gap-6">
      <header className="grid gap-2">
        <p className="text-13 text-faint">
          {memoryType[entry.type]} in {workspaceName(entry.workspace)}
        </p>
        <h3 className="text-16 font-semibold break-words">{entry.title ?? 'Forgotten entry'}</h3>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-13 text-muted">
          <span className="inline-flex items-center gap-2">
            <StatusDot tone={entry.trust === 'untrusted' ? 'warn' : 'neutral'} />
            {memoryTrust[entry.trust]}
          </span>
          {waiting ? <Status status={{ label: 'Waiting for a look', tone: 'warn' }} /> : null}
          {!live ? <Status status={memoryState[entry.state]} /> : null}
        </div>
        <p className="text-13 text-muted">{SOURCE_SENTENCE[entry.trust]}</p>
      </header>

      {entry.suspicious && live ? (
        <p className="rounded-xl border border-line px-4 py-3 text-13 text-muted">
          This entry reads like an instruction aimed at agents. Its text stays hidden here, so only its title is shown.
        </p>
      ) : null}

      <section aria-labelledby="memory-text" className="grid gap-2">
        <h4 id="memory-text" className="text-13 text-faint">
          Text
        </h4>
        {entry.state === 'forgotten' && entry.forgotten ? (
          <p className="text-14 text-muted">
            Forgotten {timeAgo(entry.forgotten.at)} by {entry.forgotten.by}. Reason: {entry.forgotten.reason}
          </p>
        ) : entry.body === null ? (
          <p className="text-14 text-muted">Nothing to show. Its text is hidden.</p>
        ) : (
          <p className="text-14 break-words whitespace-pre-wrap">{entry.body}</p>
        )}
      </section>

      {entry.evidence.length ? (
        <section aria-labelledby="memory-read" className="grid gap-2">
          <h4 id="memory-read" className="text-13 text-faint">
            What the agent read first
          </h4>
          <ul className="grid gap-2">
            {entry.evidence.map((item, index) => (
              <li key={`${item.at ?? index}-${index}`} className="text-13 break-words">
                {item.summary ?? 'Read a page'}
                {item.at ? <span className="text-faint"> · {timeAgo(item.at)}</span> : null}
                {technical && item.tool ? <span className="mono ml-2 text-faint">{item.tool}</span> : null}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {entry.history.length > 1 ? (
        <section aria-labelledby="memory-history" className="grid gap-2">
          <h4 id="memory-history" className="text-13 text-faint">
            Versions
          </h4>
          <ol className="grid gap-1">
            {entry.history.map((version) => (
              <li key={version.version} className="flex items-baseline justify-between gap-4 text-13">
                <span className="min-w-0 truncate">
                  {version.forgotten ? 'Forgotten' : (version.title ?? 'Untitled')}
                </span>
                <span className="shrink-0 text-faint">
                  {timeAgo(version.at)} · {memoryTrust[version.trust]}
                </span>
              </li>
            ))}
          </ol>
        </section>
      ) : null}

      {technical ? (
        <div className="grid gap-1 rounded-xl border border-line px-4 py-2">
          <Detail label="Entry id">
            <span className="mono">{entry.id}</span>
          </Detail>
          <Detail label="Version">
            <span className="mono tabular">
              {entry.version} of {entry.current}
            </span>
          </Detail>
          <Detail label="Written by">
            <span className="mono">{entry.writer}</span>
          </Detail>
          {entry.lane ? (
            <Detail label="Account">
              <span className="mono">{entry.lane}</span>
            </Detail>
          ) : null}
          <Detail label="Last changed">{dateTime(entry.updated_at)}</Detail>
        </div>
      ) : null}

      {failure ? (
        <InlineError>
          {describeError(failure)}
        </InlineError>
      ) : null}

      {live ? (
        <div className="flex flex-wrap items-center gap-2 border-t border-line pt-4">
          {entry.trust !== 'owner' ? (
            <Button variant="primary" loading={confirm.isPending} disabled={hidden} onClick={doConfirm}>
              Confirm it
            </Button>
          ) : null}
          {waiting ? (
            <Button loading={review.isPending} onClick={doReview}>
              Mark as looked at
            </Button>
          ) : null}
          <Button variant="danger" className="ml-auto" onClick={() => setForgetting(true)}>
            Forget it
          </Button>
          {hidden ? <p className="w-full text-13 text-faint">Its text is hidden, so there is nothing here to confirm.</p> : null}
        </div>
      ) : null}

      <ConfirmDialog
        open={forgetting}
        onOpenChange={setForgetting}
        title="Forget this entry?"
        description={FORGET_HINT}
        confirmLabel="Forget it"
        danger
        typeToConfirm="forget"
        confirmDisabled={reason.trim() === ''}
        loading={forget.isPending}
        onConfirm={doForget}
      >
        <Field label="Why it is forgotten" hint="Kept on record with the entry, so the history says why it went.">
          <Textarea value={reason} onChange={(event) => setReason(event.target.value)} maxLength={500} />
        </Field>
      </ConfirmDialog>
    </div>
  );
}
