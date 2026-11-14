import { useState } from 'react';
import { useProofShares, useRevokeShare } from '@/api/hooks';
import type { ProofShare } from '@/api/types';
import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/dialog';
import { Section } from '@/components/ui/card';
import { EmptyState, QueryView, Skeleton } from '@/components/ui/states';
import { Status } from '@/components/ui/status';
import { Table, Td, Th, Tr, mainCell } from '@/components/ui/table';
import { dateTime, plural, timeAgo } from '@/lib/format';
import { laneName } from '@/lib/labels';
import { accountOf } from './share-dialog';
import { dayTitle, SHARE_WORDS } from './words';

/** What a link shows, in words. A step is named by its place in the day, never by its id. */
function whatOf(share: ProofShare): string {
  if (share.kind === 'trace') return 'One step';
  return `${laneName(accountOf(share.target.lane ?? ''))}, ${dayTitle(share.target.day ?? '')}`;
}

/**
 * Every read-only link the owner has made, newest first, with what it shows, whether it still
 * works and how often it was opened. Stopping one is a confirmed step: the link stops at once.
 */
export function SharesList({ org }: { org: string }) {
  const shares = useProofShares(org);
  const revoke = useRevokeShare(org);
  const [stopping, setStopping] = useState<ProofShare | null>(null);

  return (
    <Section
      title="Read-only links"
      description="Each link opens one account's day, or one step, with no sign-in. Stop a link and it stops working at once."
    >
      <QueryView
        query={shares}
        card
        skeleton={
          <div role="status" aria-label="Loading" className="grid gap-3 rounded-xl border border-line p-4">
            <Skeleton className="h-4 w-2/3" />
            <Skeleton className="h-4 w-1/3" />
          </div>
        }
      >
        {(rows) =>
          rows.length === 0 ? (
            <div className="rounded-xl border border-line">
              <EmptyState title="No links yet" description="Share a day or one step from the list above, and its link is kept here." />
            </div>
          ) : (
            <div className="overflow-hidden rounded-xl border border-line">
              <Table>
                <thead className="hidden md:table-header-group">
                  <tr>
                    <Th>What</Th>
                    <Th className="w-28">State</Th>
                    <Th className="hidden w-24 text-right lg:table-cell">Opened</Th>
                    <Th className="w-40">Ends</Th>
                    <Th className="w-28">
                      <span className="sr-only">Actions</span>
                    </Th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((share) => {
                    const status = SHARE_WORDS[share.state];
                    return (
                      <Tr key={share.id}>
                        <Td className={mainCell}>
                          <p className="truncate text-14">{whatOf(share)}</p>
                          <p className="truncate text-12 text-faint md:hidden">
                            <Status status={status} />
                          </p>
                        </Td>
                        <Td className="hidden md:table-cell">
                          <Status status={status} />
                        </Td>
                        <Td className="tabular hidden text-right text-13 text-muted lg:table-cell">
                          {share.views === 0 ? 'Not yet' : plural(share.views, 'time')}
                          {share.last_view_at ? <span className="block text-12 text-faint">{timeAgo(share.last_view_at)}</span> : null}
                        </Td>
                        <Td className="tabular text-13 whitespace-nowrap text-muted">
                          {share.state === 'active' ? dateTime(share.expires_at) : share.revoked_at ? `Stopped ${timeAgo(share.revoked_at)}` : dateTime(share.expires_at)}
                        </Td>
                        <Td className="text-right">
                          {share.state === 'active' ? (
                            <Button variant="danger" size="sm" onClick={() => setStopping(share)}>
                              Stop link
                            </Button>
                          ) : null}
                        </Td>
                      </Tr>
                    );
                  })}
                </tbody>
              </Table>
            </div>
          )
        }
      </QueryView>
      <ConfirmDialog
        open={stopping !== null}
        onOpenChange={(open) => {
          if (!open) setStopping(null);
        }}
        title="Stop this link?"
        description="Anyone with it loses access at once. A new link can be made after this."
        confirmLabel="Stop link"
        danger
        loading={revoke.isPending}
        onConfirm={async () => {
          if (stopping) await revoke.mutateAsync(stopping.id);
          setStopping(null);
        }}
      />
    </Section>
  );
}
