/**
 * Resume, from the app (concept): the owner wakes a saved environment without a chat. A paused machine
 * comes back in seconds as it was, its dev server still running; a snapshot comes back on a new machine in
 * about a minute. It then waits for an agent: the next one that opens its title takes it over. The dialog
 * says what it bills first. While it wakes, the page shows the bring-up growing; once awake, who watches it,
 * because watching keeps it open.
 */
import { useNavigate } from '@tanstack/react-router';
import { Eye, Moon, Play } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';
import { useAccess, useEnvironments, useResumeSave } from '@/api/hooks';
import { ApiError } from '@/api/http';
import type { EnvDetail, EnvSave, EnvWatcher } from '@/api/types';
import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/dialog';
import { CopyButton } from '@/components/ui/misc';
import { StatusDot } from '@/components/ui/status';
import { dateTime, timeAgo } from '@/lib/format';
import { continueLine, hourly, monthly, saveHolds } from './words';

/** How fast each kind of save comes back, and what comes back with it. */
export function comebackWords(save: Pick<EnvSave, 'paused' | 'snapshot_gb' | 'services'>): string {
  if (save.paused) {
    const running = save.services?.map((service) => service.cmd) ?? [];
    return running.length ? `Comes back in seconds, as it was: ${running.join(' and ')} still running.` : 'Comes back in seconds, as it was, its programs still running.';
  }
  if (save.snapshot_gb) return 'Comes back in about a minute on a new machine. Its programs start again.';
  return 'Comes back on a fresh machine with its files, in a few minutes.';
}

export function ResumeButton({ org, save, primary = false }: { org: string; save: EnvSave; primary?: boolean }) {
  const access = useAccess(org);
  const resume = useResumeSave(org);
  const navigate = useNavigate();
  const [asking, setAsking] = useState(false);
  if (!access.can('environments.forget')) return null;
  const what = save.paused ? 'its paused machine' : save.snapshot_gb ? 'its snapshot on a new machine' : 'its files on a fresh machine';
  const price = save.price_hourly_eur ? `It bills ${hourly(save.price_hourly_eur)} while it is awake, instead of ${monthly(save.cost_eur_month)} saved.` : 'It bills by the hour while it is awake.';
  return (
    <>
      <Button variant={primary ? 'primary' : 'secondary'} size={primary ? 'md' : 'sm'} icon={<Play className="size-4" />} loading={resume.isPending} onClick={() => setAsking(true)} aria-label={primary ? undefined : `Resume ${save.title}`}>
        Resume
      </Button>
      <ConfirmDialog
        open={asking}
        onOpenChange={setAsking}
        title={`Resume ${save.title}?`}
        description={`agentos wakes ${what} now. ${price} It waits for an agent: the next one that opens "${save.title}" takes it over. If none comes and no one watches, it is saved again when it idles.`}
        confirmLabel="Resume"
        loading={resume.isPending}
        onConfirm={() =>
          resume.mutate(
            { id: save.id },
            {
              onSuccess: (env) => {
                setAsking(false);
                void navigate({ to: '/$org/environments/$id', params: { org, id: env.id } });
              },
              onError: (error) => {
                setAsking(false);
                toast.error(error instanceof ApiError ? error.message : 'It could not be resumed. Nothing was changed.');
              },
            },
          )
        }
      />
    </>
  );
}

/** On a saved environment's own page: it sleeps, what that costs, how it comes back, and Resume. */
export function SavedNotice({ org, env }: { org: string; env: EnvDetail }) {
  const list = useEnvironments(org);
  const save = list.data?.saved?.find((entry) => entry.env_id === env.id);
  if (!save) return null;
  return (
    <section aria-label="Saved" className="flex flex-wrap items-center gap-x-6 gap-y-4 rounded-xl border border-line bg-card px-5 py-4 shadow-lift">
      <span aria-hidden="true" className="flex size-10 shrink-0 items-center justify-center rounded-lg border border-line">
        <Moon className="size-4 text-fg" />
      </span>
      <div className="grid min-w-0 flex-1 gap-1">
        <p className="text-14">
          <span className="font-medium text-fg">Saved {timeAgo(save.saved_at).toLowerCase()}</span>
          <span className="text-muted">
            {' '}
            · {saveHolds(save)} · {monthly(save.cost_eur_month)}
          </span>
        </p>
        <p className="text-secondary text-muted">{comebackWords(save)} Resume it here, or say it to an agent:</p>
        <p className="flex min-w-0 items-center gap-1">
          <code className="truncate font-mono text-code-caption text-fg">{continueLine(save.title)}</code>
          <CopyButton text={continueLine(save.title)} label="Copy what to say" />
        </p>
      </div>
      <ResumeButton org={org} save={save} primary />
    </section>
  );
}

/** An environment the owner resumed: waking, or awake and waiting for an agent. */
export function ResumedNotice({ env }: { env: EnvDetail }) {
  if (!env.resumed) return null;
  const waking = env.state === 'starting';
  return (
    <section role="status" aria-label={waking ? 'Waking' : 'Waiting for an agent'} className="flex items-start gap-3 rounded-xl border border-line bg-card px-5 py-4 shadow-lift">
      <StatusDot tone={waking ? 'live' : 'ok'} pulse={waking} className="mt-2" />
      <div className="grid min-w-0 gap-1 text-14">
        <p>
          <span className="font-medium text-fg">{waking ? `Waking from its ${env.resumed.from === 'paused' ? 'paused machine' : 'snapshot'}.` : 'Awake, and waiting for an agent.'}</span>{' '}
          <span className="text-muted">
            {waking
              ? env.resumed.from === 'paused'
                ? 'Usually under 10 seconds.'
                : 'Usually about a minute.'
              : `The next agent that opens "${env.title}" takes it over, with its work as it was.`}
          </span>
        </p>
        <p className="text-secondary text-faint">
          You resumed it {timeAgo(env.resumed.at).toLowerCase()}. {waking ? 'You can leave this page: it carries on.' : `Say "${continueLine(env.title)}" to an agent.`}
        </p>
      </div>
    </section>
  );
}

/** "You are watching it, so it stays open": who has the live view open, in the header's line. */
export function WatchLine({ watching }: { watching: EnvWatcher[] | undefined }) {
  if (!watching?.length) return null;
  const you = watching.find((watcher) => watcher.you);
  const others = watching.filter((watcher) => !watcher.you).map((watcher) => watcher.name);
  const who = you ? (others.length ? `You and ${others.join(', ')} are watching` : 'You are watching') : `${others.join(', ')} ${others.length === 1 ? 'is' : 'are'} watching`;
  const since = (you ?? watching[0])?.since;
  return (
    <span className="inline-flex items-center gap-1" title={since ? `Since ${dateTime(since)}. While anyone watches, it does not close or save for being idle.` : undefined}>
      <Eye aria-hidden="true" className="size-4 text-faint" />
      {who}, so it stays open
    </span>
  );
}
