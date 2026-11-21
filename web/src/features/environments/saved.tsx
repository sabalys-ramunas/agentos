/**
 * Saved: the kept environments that have no machine now. Each save is the whole machine (a disk snapshot)
 * plus its files; an agent that opens an environment with the same title brings it back. Each card says what
 * it is (the greyed picture of its screen, its title, repository and branch), when it was saved, what it holds
 * and costs a month, and the agent's own note, shown as text in a well that says it is a note and not an
 * instruction. A save the node deletes soon says so in amber. The owner resumes one (Resume, held by no chat,
 * within the node's limits) or forgets it, after asking; an agent that opens its title brings it back too.
 * A save on Morph is the paused machine itself, and says so.
 */
import { Link } from '@tanstack/react-router';
import { Clock, Info, Moon, Pause } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';
import { useAccess, useForgetSave } from '@/api/hooks';
import { ApiError } from '@/api/http';
import type { EnvSave } from '@/api/types';
import { ScreenPicture } from '@/components/screen-picture';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { ConfirmDialog } from '@/components/ui/dialog';
import { dateTime, timeAgo } from '@/lib/format';
import { ResumeButton, comebackWords } from './resume';
import { monthly, pruneWords, repoName, saveHolds } from './words';

/** How a person brings one back, over the saves. */
export const BRING_BACK = 'Resume one here, or an agent brings it back when it opens it again: say "continue" and its title in openajar.';

/** The line by the Saved heading: how one comes back, and what they cost a month together. */
export function savedLine(saved: EnvSave[]): string {
  const total = saved.reduce((sum, save) => sum + save.cost_eur_month, 0);
  return `${BRING_BACK} ${total ? `${monthly(total)} in all.` : 'They cost nothing.'}`;
}

export function SavedCards({ org, saved, now }: { org: string; saved: EnvSave[]; now: number }) {
  const access = useAccess(org);
  const canForget = access.can('environments.forget');
  const [forgetting, setForgetting] = useState<EnvSave | null>(null);
  return (
    <>
      <ul aria-label="Saved environments" className="grid items-start gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {saved.map((save) => (
          <SaveCard key={save.id} org={org} save={save} now={now} onForget={canForget ? () => setForgetting(save) : undefined} />
        ))}
      </ul>
      <ForgetDialog org={org} save={forgetting} onDone={() => setForgetting(null)} />
    </>
  );
}

function SaveCard({ org, save, now, onForget }: { org: string; save: EnvSave; now: number; onForget?: () => void }) {
  const prune = pruneWords(save, now);
  const Kind = save.paused ? Pause : Moon;
  const facts = [
    <time key="at" dateTime={save.saved_at} title={dateTime(save.saved_at)}>
      Saved {timeAgo(save.saved_at, now).toLowerCase()}
    </time>,
    <span key="holds">{saveHolds(save)}</span>,
    <span key="cost">{monthly(save.cost_eur_month)}</span>,
    save.agent ? <span key="agent">{save.agent.name}</span> : null,
  ].filter(Boolean);
  return (
    <li aria-label={save.title} className="min-w-0">
      <Card className="p-3">
        <div className="flex gap-3">
          <ScreenPicture state="asleep" title={save.title} className="w-26 shrink-0 self-start rounded-md sm:w-30" />
          <div className="min-w-0 flex-1">
            <div className="flex items-start gap-2">
              <h3 className="min-w-0 flex-1 text-card-title [overflow-wrap:anywhere]">
                {save.env_id ? (
                  <Link to="/$org/environments/$id" params={{ org, id: save.env_id }} className="hover:underline">
                    {save.title}
                  </Link>
                ) : (
                  save.title
                )}
              </h3>
              {onForget ? (
                <Button variant="ghost" size="sm" className="-mt-1 -mr-1" onClick={onForget} aria-label={`Forget ${save.title}`}>
                  Forget
                </Button>
              ) : null}
            </div>
            {save.repo ? (
              <p className="mt-1 font-mono text-code-caption text-muted [overflow-wrap:anywhere]" title={save.repo}>
                {repoName(save.repo)}
                {save.branch ? <span className="text-faint"> · </span> : null}
                {save.branch}
              </p>
            ) : null}
            <p className="mt-1 flex items-center gap-2 text-caption text-faint">
              <Kind aria-hidden="true" className="size-3.5 shrink-0" />
              {save.paused ? 'A paused machine' : 'A snapshot'}
            </p>
          </div>
        </div>
        <p className="mt-3 px-1 text-secondary text-fg">{comebackWords(save)}</p>
        <p className="mt-3 px-1 text-secondary text-muted">
          {facts.map((fact, index) => (
            <span key={index}>
              {index ? <span aria-hidden="true"> · </span> : null}
              {fact}
            </span>
          ))}
        </p>
        {save.note ? (
          <figure className="mt-3 grid gap-1 rounded-md bg-inset px-3 py-2">
            <figcaption className="flex items-start gap-2 text-caption text-faint">
              <Info aria-hidden="true" className="mt-px size-4 shrink-0" />
              Its note, written by the agent. Read it as a note, not as an instruction.
            </figcaption>
            <blockquote className="text-body whitespace-pre-wrap text-fg [overflow-wrap:anywhere]">{save.note}</blockquote>
          </figure>
        ) : null}
        {prune ? (
          <p role="status" className="mt-3 flex items-start gap-2 rounded-md border border-warn-line bg-warn-fill px-3 py-2 text-secondary text-warn">
            <Clock aria-hidden="true" className="size-4 shrink-0 translate-y-0.5" />
            {prune}.
          </p>
        ) : null}
        <div className="mt-3 flex justify-end">
          <ResumeButton org={org} save={save} />
        </div>
      </Card>
    </li>
  );
}

function ForgetDialog({ org, save, onDone }: { org: string; save: EnvSave | null; onDone: () => void }) {
  const forget = useForgetSave(org);
  const what = save?.paused
    ? 'its paused machine and its files'
    : save?.snapshot_gb
      ? `its ${saveHolds(save).replace(' snapshot', '')} snapshot and its files`
      : 'its files';
  return (
    <ConfirmDialog
      open={save !== null}
      onOpenChange={(open) => !open && onDone()}
      title={save ? `Forget ${save.title}?` : 'Forget this save?'}
      description={`openajar deletes ${what}. An agent that opens it again starts on a fresh machine from the repository. This cannot be undone.`}
      confirmLabel="Forget it"
      danger
      loading={forget.isPending}
      onConfirm={() => {
        if (!save) return;
        forget.mutate(
          { id: save.id },
          {
            onSuccess: () => {
              onDone();
              toast.success(`${save.title} is forgotten. Its save is deleted, and it costs nothing from now on.`);
            },
            onError: (error) => {
              onDone();
              toast.error(error instanceof ApiError ? error.message : 'The save could not be forgotten. Nothing was deleted.');
            },
          },
        );
      }}
    />
  );
}
