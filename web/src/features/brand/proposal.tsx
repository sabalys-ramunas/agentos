/**
 * A waiting brand proposal, leading its profile's page: the agent's reason, what changes as a readable
 * difference (each token before and after as swatches and values, guideline text changes), and Approve
 * and Reject for the owner in person. The hook handles step-up and refusal; an error is said here.
 */
import { ArrowRight, Check, X } from 'lucide-react';
import { useState } from 'react';
import { useDecideBrand } from '@/api/hooks';
import type { BrandDraft, BrandProfile } from '@/api/types';
import { AgentMark } from '@/components/brand/identity';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Textarea } from '@/components/ui/field';
import { KindEyebrow } from '@/components/ui/kind';
import { Status } from '@/components/ui/status';
import { Table, TableScroll, Td, Th } from '@/components/ui/table';
import { timeAgo } from '@/lib/format';
import { principalName } from '@/lib/principal';
import type { BrandDecision } from '@/api/types';
import { drawnButton, guidelineChanges, guidelineRule, paintable, tokenChanges } from './model';
import { DrawnButtonView } from './sections';
import type { TokenChange } from './model';
import { InlineError } from '@/components/ui/states';

function Value({ value, colour }: { value: string | null; colour: boolean }) {
  if (value === null) return <span className="text-faint">none</span>;
  const paint = colour ? paintable(value) : null;
  return (
    <span className="inline-flex min-w-0 items-center gap-2">
      {paint ? (
        <span data-studio="ignore" role="img" aria-label={`The colour ${value}`} className="size-5 shrink-0 rounded-sm border border-line" style={{ background: paint }} />
      ) : null}
      <span className="truncate font-mono text-code text-fg" title={value}>
        {value}
      </span>
    </span>
  );
}

function TokenTable({ changes }: { changes: TokenChange[] }) {
  return (
    <TableScroll>
      <Table density="compact" aria-label="Tokens the proposal changes">
        <thead>
          <tr>
            <Th>Token</Th>
            <Th>Now</Th>
            <Th>Proposed</Th>
          </tr>
        </thead>
        <tbody>
          {changes.map((change) => (
            <tr key={change.name}>
              <Td main>
                <span className="block truncate font-mono text-code" title={change.name}>
                  {change.name}
                </span>
              </Td>
              <Td>
                <Value value={change.before} colour={change.colour} />
              </Td>
              <Td>
                <Value value={change.after} colour={change.colour} />
              </Td>
            </tr>
          ))}
        </tbody>
      </Table>
    </TableScroll>
  );
}

export function Proposal({ org, profile, draft, onDecided }: { org: string; profile: BrandProfile; draft: BrandDraft; onDecided: (decision: BrandDecision) => void }) {
  const decide = useDecideBrand(org);
  const [rejecting, setRejecting] = useState(false);
  const [note, setNote] = useState('');
  const tokens = tokenChanges(profile, draft);
  const drawnNow = drawnButton(profile);
  const drawnNext = draft.tokens && profile.brand ? drawnButton({ ...profile, brand: { ...profile.brand, tokens: draft.tokens } }) : null;
  const buttonChanges = drawnNow && drawnNext && JSON.stringify(drawnNow) !== JSON.stringify(drawnNext);
  const guidelines = guidelineChanges(profile, draft);
  // Not mutate(..., { onSuccess }): the refetch after a decision drops the draft, which unmounts this
  // card, and a call's own callbacks never run once its component is gone, so "Version N is live"
  // only showed when the callback won that race. The promise always settles; an error shows below.
  const send = (approve: boolean) =>
    decide
      .mutateAsync({ ref: profile.ref, version: draft.version, approve, note: approve ? undefined : note.trim() || undefined })
      .then(onDecided, () => undefined);
  const error = decide.error instanceof Error ? decide.error.message : decide.error ? 'The decision did not go through. Nothing was changed.' : null;

  return (
    <Card kind="design" aria-labelledby="proposal-title" className="mb-8">
      <div className="grid gap-3 px-5 pt-5 pb-4">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <KindEyebrow kind="design" label="Brand change" />
          <Status status={{ label: 'Waiting for you', tone: 'warn' }} />
          <span className="text-caption text-faint">{timeAgo(draft.created_at)}</span>
        </div>
        <h2 id="proposal-title" className="flex min-w-0 flex-wrap items-center gap-2 text-section-title">
          {draft.created_by ? <AgentMark principal={draft.created_by} size={20} /> : null}
          <span className="min-w-0">
            {draft.created_by ? principalName(draft.created_by) : 'An agent'} proposes version {draft.version} of {profile.display_name}
          </span>
        </h2>
        {draft.reason ? <p className="max-w-[60em] text-14 break-words text-muted">“{draft.reason}”</p> : null}
      </div>
      <div className="grid gap-5 border-t border-line px-5 py-4 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <div className="grid content-start gap-4">
          {buttonChanges ? (
            <div className="grid gap-2">
              <h3 className="text-secondary text-faint">The Button, drawn from the tokens</h3>
              <div className="grid grid-cols-2 gap-3">
                <figure className="grid gap-1">
                  <DrawnButtonView button={drawnNow} label={`The Button with version ${profile.version}'s tokens`} text="Button" />
                  <figcaption className="text-caption text-faint">Now, version {profile.version}</figcaption>
                </figure>
                <figure className="grid gap-1">
                  <DrawnButtonView button={drawnNext} label={`The Button with version ${draft.version}'s tokens`} text="Button" />
                  <figcaption className="text-caption text-faint">Proposed, version {draft.version}</figcaption>
                </figure>
              </div>
            </div>
          ) : null}
          {tokens && tokens.length ? (
            <div className="grid gap-2">
              <h3 className="text-secondary text-faint">Tokens</h3>
              <TokenTable changes={tokens} />
            </div>
          ) : null}
          {guidelines && guidelines.length ? (
            <div className="grid gap-2">
              <h3 className="text-secondary text-faint">Guidelines</h3>
              <ul className="grid gap-2">
                {guidelines.map((change) => (
                  <li key={change.id} className="grid gap-1 rounded-lg border border-line p-3">
                    <span className="flex items-center gap-2 text-caption text-faint">
                      <span className="font-mono text-code-caption text-muted">{change.id}</span>
                      {change.kind === 'added' ? 'added' : change.kind === 'removed' ? 'removed' : 'changed'}
                    </span>
                    {change.before && change.kind !== 'added' ? (
                      <span className="text-14 text-muted line-through">{change.before.text}</span>
                    ) : null}
                    {change.after ? (
                      <span className="text-14 text-fg">
                        {change.after.text} <span className="text-secondary text-faint">{guidelineRule(change.after)}</span>
                      </span>
                    ) : null}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
          {(tokens === null || guidelines === null) && draft.diff.length ? (
            <ul className="grid gap-1">
              {draft.diff.map((line, index) => (
                <li key={index} className="font-mono text-code break-words text-fg">
                  {line}
                </li>
              ))}
            </ul>
          ) : null}
          {tokens !== null && guidelines !== null && draft.diff.length ? (
            <details className="text-secondary text-muted">
              <summary className="cursor-pointer">Every change as the computer lists it ({draft.diff.length})</summary>
              <ul className="mt-2 grid gap-1">
                {draft.diff.map((line, index) => (
                  <li key={index} className="font-mono text-code-caption break-words">
                    {line}
                  </li>
                ))}
              </ul>
            </details>
          ) : null}
        </div>
        <div className="grid content-start gap-3">
          <p className="text-14 text-muted">
            Only you can decide. Agents keep using version {profile.version} until you approve, and every check of {profile.display_name} measures against
            the version that is live.
          </p>
          {rejecting ? (
            <div className="grid gap-1">
              <label htmlFor="reject-note" className="text-secondary text-muted">
                A note for the agent (optional)
              </label>
              <Textarea id="reject-note" value={note} onChange={(event) => setNote(event.target.value)} rows={2} maxLength={500} />
            </div>
          ) : null}
          {error ? (
            <InlineError>
              {error}
            </InlineError>
          ) : null}
          <div className="flex flex-wrap justify-end gap-2">
            {rejecting ? (
              <>
                <Button variant="ghost" onClick={() => setRejecting(false)} disabled={decide.isPending}>
                  Keep it waiting
                </Button>
                <Button variant="danger" icon={<X className="size-4" />} loading={decide.isPending} onClick={() => send(false)}>
                  Reject version {draft.version}
                </Button>
              </>
            ) : (
              <>
                <Button onClick={() => setRejecting(true)} disabled={decide.isPending}>
                  Reject
                </Button>
                <Button variant="primary" icon={<Check className="size-4" />} loading={decide.isPending} onClick={() => send(true)}>
                  Approve version {draft.version}
                </Button>
              </>
            )}
          </div>
        </div>
      </div>
    </Card>
  );
}

/** After a decision: what is live now. */
export function Decided({ version, approved, onDone }: { version: number; approved: boolean; onDone: () => void }) {
  return (
    <Card role="status" className="mb-8 flex flex-wrap items-center gap-3 px-5 py-4">
      <Status status={{ label: approved ? `Version ${version} is live` : `Version ${version} was rejected`, tone: approved ? 'ok' : 'neutral' }} />
      <span className="min-w-0 flex-1 text-14 text-muted">
        {approved ? 'Every check now measures against it, and agents get it on their next task.' : 'Agents keep using the version that was live.'}
      </span>
      <Button variant="ghost" size="sm" icon={<ArrowRight className="size-4" />} onClick={onDone}>
        Done
      </Button>
    </Card>
  );
}
