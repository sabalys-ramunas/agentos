/**
 * Proof: one account's day, replayed step by step, with whether each step is signed, the proof
 * screenshot where there is one, and read-only links to share it (docs/API.md, "Audit"). Whether
 * the whole log is intact is asked of the owner alone, as the node refuses anyone else.
 */
import { Link, useNavigate, useParams, useSearch } from '@tanstack/react-router';
import { ChevronLeft, ChevronRight, Link2 } from 'lucide-react';
import { useMemo, useState } from 'react';
import { useAccess, useProofDay, useProofReplay, useWorkspaces } from '@/api/hooks';
import { proof } from '@/api/client';
import type { ProofStep } from '@/api/types';
import { usePageCrumb } from '@/components/shell/crumb';
import { Button, IconButton } from '@/components/ui/button';
import { Dialog } from '@/components/ui/dialog';
import { Card } from '@/components/ui/card';
import { PageHeader } from '@/components/ui/misc';
import { Select } from '@/components/ui/select';
import { EmptyState, ErrorState, QueryView, Skeleton } from '@/components/ui/states';
import { Status } from '@/components/ui/status';
import { Table, Td, Th, Tr, mainCell } from '@/components/ui/table';
import { dateTime, plural, utcClock } from '@/lib/format';
import { laneName, runStatus } from '@/lib/labels';
import { PLACES } from '@/lib/vocabulary';
import { usePrincipalName } from '@/lib/use-principal';
import { placeNames, proofSummary } from '@/lib/plain-node';
import { LogCheck } from './log-check';
import { accountOf, ShareDialog, type ShareTarget } from './share-dialog';
import { SharesList } from './shares';
import { CHAIN_WORDS, chainSentence, dayTitle, shiftDay } from './words';


/** A step's own result, in the words of runs, or the plain word the node gave. */
function resultOf(status: string) {
  return status in runStatus
    ? runStatus[status as keyof typeof runStatus]
    : { label: status === 'unfinished' ? 'Still running' : status, tone: 'neutral' as const };
}

function ProofImage({ src, title, onClose }: { src: string; title: string; onClose: () => void }) {
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()} title="Proof screenshot" description={title} className="sm:max-w-[720px]">
      <img src={src} alt={`Proof screenshot of ${title}`} className="w-full rounded-lg border border-line bg-inset" />
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
  usePageCrumb('Proof', { mono: false });
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
        eyebrow={
          <Link to="/$org/activity" params={{ org }} className="inline-flex min-h-8 items-center gap-1 text-secondary text-muted hover:text-fg md:min-h-0">
            <ChevronLeft aria-hidden="true" className="size-4" />
            Back to {PLACES.history.word}
          </Link>
        }
        title="Proof"
        description={`What one account did on one day, checked against the signed log. ${PLACES.history.word} tells the story, and this shows it can be trusted.`}
        actions={
          owner && lane && day ? (
            <Button icon={<Link2 className="size-4" />} onClick={() => setTarget({ kind: 'day', lane, day })}>
              Share this day
            </Button>
          ) : null
        }
      />

      <div className="grid gap-6">
        {owner ? <LogCheck org={org} /> : null}

        <Card className="overflow-hidden">
          <div className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3 border-b border-line px-5 py-4">
            <div className="min-w-0">
              <h2 className="text-section-title">
                {day ? dayTitle(day) : 'A day'}
                {lane ? <span className="text-muted"> · {laneName(accountOf(lane))}</span> : null}
              </h2>
              {lane && replay.data ? (
                <p className="text-secondary text-pretty text-muted">
                  {plural(replay.data.steps.length, 'step')}. {chainSentence(replay.data.chain)}
                </p>
              ) : null}
            </div>
          <div className="flex w-full items-center gap-2 sm:w-auto">
            {accounts.length > 0 ? (
              <div className="min-w-0 flex-1 sm:w-56 sm:flex-none">
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
          </div>

          {index.isPending ? (
            <div role="status" aria-label="Loading" className="grid gap-3 p-5">
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
            </div>
          ) : index.isError ? (
            <ErrorState error={index.error} onRetry={() => void index.refetch()} />
          ) : accounts.length === 0 ? (
            <div>
              <EmptyState
                title="Nothing was done on any account this day"
                description="Pick another day with the arrows. Days with work on them show their accounts here."
              />
            </div>
          ) : (
            <QueryView
              query={replay}
              skeleton={
                <div role="status" aria-label="Loading" className="grid gap-3 p-5">
                  <Skeleton className="h-10 w-full" />
                  <Skeleton className="h-10 w-full" />
                </div>
              }
            >
              {(record) =>
                record.steps.length === 0 ? (
                  <div>
                    <EmptyState title="Nothing on this account this day" description="Nothing it did on this day is kept." />
                  </div>
                ) : (
                  <div>
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
                              <Td className="tabular hidden text-secondary whitespace-nowrap text-faint md:table-cell">{utcClock(step.at)}</Td>
                              <Td className={`${mainCell} whitespace-normal`}>
                                <p className="text-body break-words">{step.summary ? proofSummary(step.summary, names) : 'A step with no words'}</p>
                                <p className="text-caption break-words text-faint">
                                  <span className="tabular md:hidden">{utcClock(step.at)} · </span>
                                  {step.grant?.label ?? who(step.principal ?? null, { inSentence: false })}
                                </p>
                                {step.error ? <p className="mt-1 text-caption text-bad">{step.error}</p> : null}
                              </Td>
                              <Td className="hidden md:table-cell">
                                <Status chip status={result} />
                              </Td>
                              <Td className="hidden md:table-cell">
                                <span title={chain.sentence}>
                                  <Status chip status={chain} />
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
                                  <Button
                                    variant="ghost"
                                    size="sm"
                                    icon={<Link2 className="size-4" />}
                                    aria-label={`Share this step: ${step.summary}`}
                                    onClick={() => setTarget({ kind: 'step', trace: step.id, summary: step.summary })}
                                  >
                                    <span className="max-md:sr-only">Share</span>
                                  </Button>
                                ) : null}
                              </Td>
                            </Tr>
                          );
                        })}
                      </tbody>
                    </Table>
                    <p className="border-t border-line px-4 py-3 text-caption text-faint">
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
        </Card>

        {owner ? <SharesList org={org} /> : null}
      </div>

      {target ? <ShareDialog key={JSON.stringify(target)} org={org} target={target} onClose={() => setTarget(null)} /> : null}
      {shot ? <ProofImage src={shot.src} title={shot.title} onClose={() => setShot(null)} /> : null}
    </>
  );
}
