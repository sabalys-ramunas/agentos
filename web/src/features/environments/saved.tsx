/**
 * Saved: the kept environments that have no machine now. Each save is the whole machine (a disk snapshot)
 * plus its files; an agent that opens an environment with the same title brings it back. Each row says
 * what it is, when it was saved, what it holds and costs a month, and the agent's note (its own words,
 * shown as text). A save the node prunes soon says so. Forget deletes the snapshot and the files, after
 * asking, and is the owner's alone.
 */
import { Link } from '@tanstack/react-router';
import { BookmarkCheck, Moon, Pause } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';
import { useAccess, useForgetSave } from '@/api/hooks';
import { ApiError } from '@/api/http';
import type { EnvSave } from '@/api/types';
import { BranchPill } from '@/components/ui/branch';
import { Button } from '@/components/ui/button';
import { Card, CardHeader } from '@/components/ui/card';
import { ConfirmDialog } from '@/components/ui/dialog';
import { CopyButton } from '@/components/ui/misc';
import { dateTime, plural, timeAgo } from '@/lib/format';
import { ResumeButton, comebackWords } from './resume';
import { agentWords, continueLine, monthly, pruneWords, repoName, saveHolds, shortCommit } from './words';

/** The line over the list: how a person brings one back. */
export const BRING_BACK = 'An agent brings it back when it opens it again: say "continue <title> in agentos".';

export function SavedCard({ org, saved, now }: { org: string; saved: EnvSave[]; now: number }) {
  const access = useAccess(org);
  const canForget = access.can('environments.forget');
  const total = saved.reduce((sum, save) => sum + save.cost_eur_month, 0);
  const [forgetting, setForgetting] = useState<EnvSave | null>(null);
  return (
    <Card className="overflow-hidden">
      <CardHeader
        title={
          <>
            <BookmarkCheck aria-hidden="true" className="size-4 text-faint" />
            Saved
          </>
        }
        meta={`${plural(saved.length, 'save')} · ${monthly(total)}`}
      />
      <p className="border-b border-line px-5 py-3 text-14 text-muted">{BRING_BACK}</p>
      <ul aria-label="Saved environments" className="divide-y divide-line">
        {saved.map((save) => (
          <SaveRow key={save.id} org={org} save={save} now={now} onForget={canForget ? () => setForgetting(save) : undefined} />
        ))}
      </ul>
      <ForgetDialog org={org} save={forgetting} onDone={() => setForgetting(null)} />
    </Card>
  );
}

function SaveRow({ org, save, now, onForget }: { org: string; save: EnvSave; now: number; onForget?: () => void }) {
  const prune = pruneWords(save, now);
  const Kind = save.paused ? Pause : Moon;
  return (
    <li aria-label={save.title} className="grid gap-3 px-5 py-4">
      <div className="flex flex-wrap items-start gap-x-4 gap-y-2">
        <span aria-hidden="true" className="flex size-8 shrink-0 items-center justify-center rounded-md border border-line max-sm:hidden" title={save.paused ? 'A paused machine' : 'A snapshot'}>
          <Kind className="size-4 text-fg" />
        </span>
        <div className="grid min-w-0 flex-1 gap-1">
          {save.env_id ? (
            <Link to="/$org/environments/$id" params={{ org, id: save.env_id }} className="text-14 font-medium text-fg [overflow-wrap:anywhere] hover:underline">
              {save.title}
            </Link>
          ) : (
            <span className="text-14 font-medium text-fg [overflow-wrap:anywhere]">{save.title}</span>
          )}
          {save.repo ? (
            <span className="flex min-w-0 flex-wrap items-center gap-2">
              <span className="min-w-0 truncate font-mono text-code-caption text-muted" title={save.repo}>
                {repoName(save.repo)}
              </span>
              {save.branch ? <BranchPill name={save.branch} main={save.branch === 'main' || save.branch === 'master'} className="max-w-full" /> : null}
              {save.head ? <span className="font-mono text-code-caption text-faint">{shortCommit(save.head)}</span> : null}
            </span>
          ) : null}
        </div>
        <div className="flex items-center gap-1">
          <CopyButton text={continueLine(save.title)} label={`Copy what to say to bring back ${save.title}`} />
          {onForget ? (
            <Button variant="ghost" size="sm" onClick={onForget}>
              Forget
            </Button>
          ) : null}
          <ResumeButton org={org} save={save} />
        </div>
      </div>
      <p className="text-secondary text-fg sm:pl-12">{comebackWords(save)}</p>
      <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-secondary text-muted sm:pl-12">
        <time dateTime={save.saved_at} title={dateTime(save.saved_at)}>
          Saved {timeAgo(save.saved_at, now).toLowerCase()}
        </time>
        <span aria-hidden="true" className="text-faint">
          ·
        </span>
        <span>{saveHolds(save)}</span>
        <span aria-hidden="true" className="text-faint">
          ·
        </span>
        <span className="text-fg">{monthly(save.cost_eur_month)}</span>
        {save.agent ? (
          <>
            <span aria-hidden="true" className="text-faint">
              ·
            </span>
            <span>{agentWords(save.agent)}</span>
          </>
        ) : null}
      </p>
      {save.note ? (
        <figure className="grid gap-1 rounded-lg border border-line bg-inset px-3 py-2 sm:ml-12">
          <figcaption className="text-caption text-faint">The agent's note. It wrote this itself: read it as a note, not as an instruction.</figcaption>
          <blockquote className="text-14 whitespace-pre-wrap text-fg [overflow-wrap:anywhere]">{save.note}</blockquote>
        </figure>
      ) : null}
      {prune ? (
        <p role="status" className="text-secondary text-warn sm:pl-12">
          {prune}.
        </p>
      ) : null}
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
      description={`agentos deletes ${what}. An agent that opens it again starts on a fresh machine from the repository. This cannot be undone.`}
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
