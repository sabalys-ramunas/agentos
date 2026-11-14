/**
 * A brand change an agent proposed, in Needs you's queue for the owner (Mono): the row, and the open
 * request with the agent's reason, the colours it swaps, every line it changes, and Decline and Approve.
 * It is decided on the profile through the brand hooks (`useDecideBrand`), never the approval routes, and
 * always for the version the owner read.
 *
 * The node keeps no picture of a draft, so there is no before and after here: the words and the colours
 * are what it knows. The whole proposal is on the profile's page.
 */
import { Link } from '@tanstack/react-router';
import { ArrowRight, Check } from 'lucide-react';
import type { BrandProposal } from '@/api/types';
import { AgentMark } from '@/components/brand/identity';
import { Button } from '@/components/ui/button';
import { IfYes } from '@/components/ui/if-yes';
import { KindEyebrow, KindTile } from '@/components/ui/kind';
import { Status } from '@/components/ui/status';
import { Swatch } from '@/components/ui/swatch';
import { principalName } from '@/lib/principal';
import { brandColours, brandIfYes } from './if-yes';
import { decideBar, queueRow, waitedShort } from './queue-words';

export function brandTitle(proposal: BrandProposal): string {
  return `Brand change: ${proposal.display_name}`;
}

/** The row in the queue. */
export function BrandRow({ proposal, focused, onFocus }: { proposal: BrandProposal; focused: boolean; onFocus: () => void }) {
  const line = brandIfYes(proposal);
  return (
    <button
      type="button"
      onClick={onFocus}
      aria-current={focused || undefined}
      aria-label={`${brandTitle(proposal)}, ${line}`}
      className={queueRow(focused)}
    >
      <KindTile kind="design" size="sm" />
      <span className="grid min-w-0 flex-1 gap-1">
        <span title={brandTitle(proposal)} className="truncate text-label">
          {brandTitle(proposal)}
        </span>
        <IfYes title={line}>
          <span title={line} className="truncate">
            {line}
          </span>
        </IfYes>
      </span>
      <span className="shrink-0 font-mono text-code-caption text-faint tabular">{waitedShort(proposal.created_at)}</span>
    </button>
  );
}

interface DetailProps {
  org: string;
  proposal: BrandProposal;
  armed: boolean;
  pending: boolean;
  done: 'approve' | 'deny' | null;
  canDecide: boolean;
  onYes: () => void;
  onNo: () => void;
}

/** The open request. */
export function BrandDetail({ org, proposal, armed, pending, done, canDecide, onYes, onNo }: DetailProps) {
  const colours = brandColours(proposal.diff);
  const who = proposal.created_by ? principalName(proposal.created_by) : 'An agent';
  const question = `Approve version ${proposal.version} of ${proposal.display_name}?`;
  return (
    <article aria-label={question} className="grid">
      <header className="flex min-h-13 flex-wrap items-center gap-3 border-b border-line px-5 py-2">
        <KindEyebrow kind="design" label="Brand change" />
        <span className="ml-auto flex items-center gap-3">
          <Status status={{ label: 'Waiting', tone: 'warn' }} />
          <span className="text-caption text-faint">{waitedShort(proposal.created_at)} ago</span>
        </span>
      </header>
      <div className="grid gap-5 p-5 lg:p-6">
        <h2 className="text-page-title-compact break-words">{question}</h2>
        {proposal.reason ? (
          <p className="max-w-[68ch] text-body break-words text-muted">
            <span className="text-fg">{who} says: </span>
            {proposal.reason}
          </p>
        ) : null}
        <section aria-label="If you say yes" className="grid gap-3">
          <p className="text-eyebrow text-faint">If you say yes</p>
          <div className="grid gap-4 rounded-lg border border-line bg-inset p-4">
            {colours.length ? (
              <ul className="grid gap-3 sm:grid-cols-2">
                {colours.map((item) => (
                  <li key={item.name} className="grid grid-cols-2 gap-2">
                    <Swatch color={item.from} name={item.name} value={item.from} tag="Now" />
                    <Swatch color={item.to} name={item.name} value={item.to} tag={`v${proposal.version}`} />
                  </li>
                ))}
              </ul>
            ) : null}
            {proposal.diff.length ? (
              <ul aria-label="What it changes" className="grid gap-1">
                {proposal.diff.map((line, index) => (
                  <li key={`${line}-${index}`} className="font-mono text-code break-words">
                    {line}
                  </li>
                ))}
              </ul>
            ) : (
              <p className="text-secondary text-muted">The node lists no changes for this version.</p>
            )}
            <div className="flex flex-wrap items-center gap-3 border-t border-line pt-3">
              <span className="text-caption text-faint">No picture of the page as it would be is kept for a draft.</span>
              <Link
                to="/$org/brand/$profile"
                params={{ org, profile: proposal.ref }}
                search={{}}
                className="link ml-auto inline-flex min-h-8 items-center gap-1 text-secondary"
              >
                The whole proposal
                <ArrowRight aria-hidden="true" className="size-3.5" />
              </Link>
            </div>
          </div>
        </section>
      </div>
      <footer className={decideBar}>
        <span className="flex min-w-0 items-center gap-2 text-secondary text-muted">
          {proposal.created_by ? <AgentMark principal={proposal.created_by} size={20} /> : null}
          <span className="truncate">{who} asked</span>
        </span>
        <span className="ml-auto flex items-center gap-2">
          {done ? (
            <Status status={done === 'approve' ? { label: `Version ${proposal.version} approved`, tone: 'ok' } : { label: 'Declined', tone: 'idle' }} />
          ) : canDecide ? (
            <>
              <Button disabled={!armed || pending} onClick={onNo} shortcut="D">
                Decline
              </Button>
              <Button variant="primary" icon={<Check className="size-4" />} disabled={!armed || pending} loading={pending} onClick={onYes} shortcut="A">
                Approve version {proposal.version}
              </Button>
            </>
          ) : (
            <span className="text-secondary text-faint">Only the owner decides a brand change.</span>
          )}
        </span>
      </footer>
    </article>
  );
}
