/**
 * Proof: one account's day, replayed step by step, with whether each step is signed, the proof
 * screenshot where there is one, and read-only links to share it (docs/API.md, "Audit"). Whether
 * the whole log is intact is asked of the owner alone, as the node refuses anyone else.
 */
import { Link, useNavigate, useParams, useSearch } from '@tanstack/react-router';
import { ChevronLeft, ChevronRight, Share2 } from 'lucide-react';
import { useMemo, useState } from 'react';
import { useAccess, useProofDay, useProofReplay, useWorkspaces } from '@/api/hooks';
import { proof } from '@/api/client';
import type { ProofStep } from '@/api/types';
import { Button, IconButton } from '@/components/ui/button';
import { Dialog } from '@/components/ui/dialog';
import { Section } from '@/components/ui/card';
import { PageHeader } from '@/components/ui/misc';
import { Select } from '@/components/ui/select';
import { EmptyState, ErrorState, QueryView, Skeleton } from '@/components/ui/states';
import { Status } from '@/components/ui/status';
import { Table, Td, Th, Tr, mainCell } from '@/components/ui/table';
import { dateTime, plural } from '@/lib/format';
import { laneName, runStatus } from '@/lib/labels';
import { PLACES } from '@/lib/vocabulary';
import { usePrincipalName } from '@/lib/use-principal';
import { placeNames, proofSummary } from '@/lib/plain-node';
import { LogCheck } from './log-check';
import { accountOf, ShareDialog, type ShareTarget } from './share-dialog';
import { SharesList } from './shares';
import { CHAIN_WORDS, chainSentence, dayTitle, shiftDay } from './words';

const clock = (iso: string) => new Date(iso).toLocaleTimeString('en', { hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'UTC' });

/** A step's own result, in the words of runs, or the plain word the node gave. */
function resultOf(status: string) {
  return status in runStatus
    ? runStatus[status as keyof typeof runStatus]
    : { label: status === 'unfinished' ? 'Still running' : status, tone: 'neutral' as const };
}

function ProofImage({ src, title, onClose }: { src: string; title: string; onClose: () => void }) {
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()} title="Proof screenshot" description={title} className="sm:max-w-[720px]">
      <img src={src} alt={`Proof screenshot of ${title}`} className="w-full rounded-lg border border-line bg-subtle" />
    </Dialog>
  );
}

export function ProofPage() {
  const { org } = useParams({ from: '/$org/activity/proof' });
  const search = useSearch({ from: '/$org/activity/proof' });
  const navigate = useNavigate({ from: '/$org/activity/proof' });
  const access = useAccess(org);
  const owner = access.can('audit.share');
  const who = usePrincipalName(org);
  const workspaces = useWorkspaces(org);
  const names = useMemo(() => placeNames(workspaces.data), [workspaces.data]);
  const [target, setTarget] = useState<ShareTarget | null>(null);
  const [shot, setShot] = useState<{ src: string; title: string } | null>(null);

  const index = useProofDay(org, search.day);
  const day = index.data?.day ?? search.day ?? '';
  const accounts = index.data?.lanes ?? [];
  const lane = accounts.find((item) => item.lane === search.lane)?.lane ?? accounts[0]?.lane ?? '';
  const replay = useProofReplay(org, lane, day);

  const move = (patch: { lane?: string; day?: string }) =>
    void navigate({ to: '.', replace: true, search: (current: { lane?: string; day?: string }) => ({ ...current, ...patch }) as never });

  return (
    <>
      <PageHeader
        title="Proof"
        description={`What one account did on one day, checked against the signed log. ${PLACES.history.word} shows the story, and this shows the record behind it.`}
        actions={
          <Button asChild>
            <Link to="/$org/activity" params={{ org }}>
              Back to {PLACES.history.word}
            </Link>
          </Button>
        }
      />

      <div className="grid gap-12">
        {owner ? <LogCheck org={org} /> : null}

        <Section
          title={day ? dayTitle(day) : 'A day'}
          description={
            lane && replay.data ? `${plural(replay.data.steps.length, 'step')} on ${laneName(accountOf(lane))}. ${chainSentence(replay.data.chain)}` : undefined
          }
          action={
            owner && lane && day ? (
              <Button icon={<Share2 className="size-4" />} onClick={() => setTarget({ kind: 'day', lane, day })}>
                Share this day
              </Button>
            ) : undefined
          }
        >
          <div className="flex items-center gap-2">
            {accounts.length > 0 ? (
              <div className="min-w-0 flex-1 sm:max-w-72">
                <Select
                  aria-label="Account"
                  value={lane}
                  onValueChange={(next) => move({ lane: next })}
                  options={accounts.map((item) => ({ value: item.lane, label: laneName(accountOf(item.lane)), hint: plural(item.rows, 'thing done', 'things done') }))}
                  className="w-full"
                />
              </div>
            ) : null}
            <IconButton label="The day before" onClick={() => move({ day: shiftDay(day, -1), lane: undefined })} disabled={!day}>
              <ChevronLeft className="size-4" />
            </IconButton>
            <IconButton label="The day after" onClick={() => move({ day: shiftDay(day, 1), lane: undefined })} disabled={!day}>
              <ChevronRight className="size-4" />
            </IconButton>
          </div>

          {index.isPending ? (
            <div role="status" aria-label="Loading" className="grid gap-3">
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
            </div>
          ) : index.isError ? (
            <ErrorState error={index.error} onRetry={() => void index.refetch()} />
          ) : accounts.length === 0 ? (
            <div className="rounded-xl border border-line">
              <EmptyState
                title="Nothing was done on any account this day"
                description="Pick another day with the arrows. Days with work on them show their accounts here."
              />
            </div>
          ) : (
            <QueryView
              query={replay}
              skeleton={
                <div role="status" aria-label="Loading" className="grid gap-3">
                  <Skeleton className="h-10 w-full" />
                  <Skeleton className="h-10 w-full" />
                </div>
              }
            >
              {(record) =>
                record.steps.length === 0 ? (
                  <div className="rounded-xl border border-line">
                    <EmptyState title="Nothing on this account this day" description="Nothing it did on this day is kept." />
                  </div>
                ) : (
                  <div className="overflow-hidden rounded-xl border border-line">
                    <Table>
                      <thead className="hidden md:table-header-group">
                        <tr>
                          <Th className="w-20">Time</Th>
                          <Th>What happened</Th>
                          <Th className="w-28">Result</Th>
                          <Th className="w-44">Record</Th>
                          <Th className="w-24">
                            <span className="sr-only">Actions</span>
                          </Th>
                        </tr>
                      </thead>
                      <tbody>
                        {record.steps.map((step: ProofStep) => {
                          const chain = CHAIN_WORDS[step.chain.status];
                          const result = resultOf(step.status);
                          return (
                            <Tr key={step.id}>
                              <Td className="tabular hidden text-13 whitespace-nowrap text-faint md:table-cell">{clock(step.at)}</Td>
                              <Td className={`${mainCell} whitespace-normal`}>
                                <p className="text-14 break-words">{step.summary ? proofSummary(step.summary, names) : 'A step with no words'}</p>
                                <p className="text-12 break-words text-faint">
                                  <span className="tabular md:hidden">{clock(step.at)} · </span>
                                  {step.grant?.label ?? who(step.principal ?? null, { inSentence: false })}
                                </p>
                                {step.error ? <p className="mt-1 text-12 text-red">{step.error}</p> : null}
                              </Td>
                              <Td className="hidden md:table-cell">
                                <Status status={result} />
                              </Td>
                              <Td className="hidden md:table-cell">
                                <span title={chain.sentence}>
                                  <Status status={chain} />
                                </span>
                              </Td>
                              <Td className="shrink-0 text-right whitespace-nowrap">
                                {step.screenshot && step.run ? (
                                  <Button
                                    variant="ghost"
                                    size="sm"
                                    onClick={() => setShot({ src: proof.runScreenshot(org, step.run!.id), title: step.summary })}
                                  >
                                    Proof
                                  </Button>
                                ) : null}
                                {owner ? (
                                  <IconButton
                                    label={`Share this step: ${step.summary}`}
                                    onClick={() => setTarget({ kind: 'step', trace: step.id, summary: step.summary })}
                                  >
                                    <Share2 className="size-4" />
                                  </IconButton>
                                ) : null}
                              </Td>
                            </Tr>
                          );
                        })}
                      </tbody>
                    </Table>
                    <p className="border-t border-line px-4 py-3 text-12 text-faint">
                      {record.truncated ? 'Only the first steps of the day are shown. ' : ''}
                      {record.chain.proven_through
                        ? `Signed through ${dateTime(record.chain.proven_through.at)}.`
                        : 'No checkpoint covers these steps yet.'}
                    </p>
                  </div>
                )
              }
            </QueryView>
          )}
        </Section>

        {owner ? <SharesList org={org} /> : null}
      </div>

      {target ? <ShareDialog key={JSON.stringify(target)} org={org} target={target} onClose={() => setTarget(null)} /> : null}
      {shot ? <ProofImage src={shot.src} title={shot.title} onClose={() => setShot(null)} /> : null}
    </>
  );
}
