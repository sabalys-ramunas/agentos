/**
 * Resume, from the app: the owner wakes a saved environment without a chat (the same right as Forget). A
 * paused machine comes back in seconds as it was, its dev server still running; a snapshot comes back on a
 * new machine in about a minute. It then waits for an agent: the next one that opens its title takes it
 * over. The dialog says what it bills first, and a refusal (the node's limits) stays in it with the fix.
 * While it wakes, the page shows the bring-up growing; once awake, that it is watched, because watching
 * keeps it open.
 */
import { useNavigate } from '@tanstack/react-router';
import { Eye, Moon, Play } from 'lucide-react';
import { useState } from 'react';
import { toast } from 'sonner';
import { useAccess, useEnvironments, useResumeSave } from '@/api/hooks';
import { ApiError } from '@/api/http';
import type { EnvDetail, EnvSave, EnvWatched } from '@/api/types';
import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/dialog';
import { CopyButton } from '@/components/ui/misc';
import { StatusDot } from '@/components/ui/status';
import { dateTime, plural, timeAgo } from '@/lib/format';
import { wakesFrom } from './opened-words';
import { continueLine, hourly, monthly, saveHolds } from './words';

/** How fast each kind of save comes back, and what comes back with it. */
export function comebackWords(save: Pick<EnvSave, 'paused' | 'snapshot_gb' | 'services' | 'provider'>): string {
  const running = save.services?.map((service) => service.cmd) ?? [];
  if (save.paused) return running.length ? `Comes back in seconds, as it was: ${running.join(' and ')} still running.` : 'Comes back in seconds, as it was, its programs still running.';
  const again = running.length ? `It starts ${running.join(' and ')} again.` : 'Its programs start again.';
  // a local image on this computer: no machine to rent, so it is back in seconds
  if (save.snapshot_gb && save.provider === 'docker') return `Comes back in seconds from its image on this computer. ${again}`;
  if (save.snapshot_gb) return `Comes back in about a minute on a new machine. ${again}`;
  return 'Comes back on a fresh machine with its files, in a few minutes.';
}

/** A refusal that names its fix: the node's limits, which the owner changes on the node. */
const LIMITS = new Set(['environment_limit', 'environment_full']);

export function ResumeButton({ org, save, primary = false }: { org: string; save: EnvSave; primary?: boolean }) {
  const access = useAccess(org);
  const resume = useResumeSave(org);
  const navigate = useNavigate();
  const [asking, setAsking] = useState(false);
  const [refused, setRefused] = useState<ApiError | null>(null);
  if (!access.can('environments.forget')) return null;
  const what = save.paused ? 'its paused machine' : save.snapshot_gb ? (save.provider === 'docker' ? 'its snapshot' : 'its snapshot on a new machine') : 'its files on a fresh machine';
  const price =
    save.price_hourly_eur === 0
      ? 'Its machine costs nothing while it is awake.'
      : save.price_hourly_eur
        ? `It bills ${hourly(save.price_hourly_eur)} while it is awake, instead of ${monthly(save.cost_eur_month)} saved.`
        : 'It bills by the hour while it is awake.';
  const close = (next: boolean) => {
    setAsking(next);
    if (!next) setRefused(null);
  };
  return (
    <>
      <Button variant={primary ? 'primary' : 'secondary'} size={primary ? 'md' : 'sm'} icon={<Play className="size-4" />} loading={resume.isPending} onClick={() => setAsking(true)} aria-label={primary ? undefined : `Resume ${save.title}`}>
        Resume
      </Button>
      <ConfirmDialog
        open={asking}
        onOpenChange={close}
        title={`Resume ${save.title}?`}
        description={`openajar wakes ${what} now. ${price} It waits for an agent: the next one that opens "${save.title}" takes it over. If none comes and no one watches, it is saved again when it idles.`}
        confirmLabel={refused ? 'Try again' : 'Resume'}
        loading={resume.isPending}
        onConfirm={() =>
          resume.mutate(
            { id: save.id },
            {
              onSuccess: (env) => {
                close(false);
                if (env.opened === 'already_open') toast.success(`${save.title} is open already, so it was left as it is.`);
                void navigate({ to: '/$org/environments/$id', params: { org, id: env.id } });
              },
              onError: (error) => {
                if (error instanceof ApiError && LIMITS.has(error.code)) {
                  setRefused(error);
                  return;
                }
                close(false);
                toast.error(error instanceof ApiError ? error.message : 'It could not be resumed. Nothing was changed.');
              },
            },
          )
        }
      >
        {refused ? <Refusal error={refused} /> : null}
      </ConfirmDialog>
    </>
  );
}

/** The node's refusal as it gave it, with the command it names ready to copy. Nothing was rented. */
function Refusal({ error }: { error: ApiError }) {
  const command = error.message.match(/`(openajar [^`]+)`/)?.[1] ?? error.message.match(/(openajar environments [\w -]+?(?:--\S+ \S+)+)/)?.[1] ?? null;
  return (
    <div role="alert" className="grid gap-2 rounded-lg border border-warn-line bg-warn-fill px-3 py-3">
      <p className="text-secondary text-fg">
        <span className="font-medium">{error.code === 'environment_full' ? 'No room for another machine now.' : 'That would pass a limit of this node.'}</span> Nothing was rented.
      </p>
      <p className="text-secondary text-muted [overflow-wrap:anywhere]">{error.message.replace(/`/g, '')}</p>
      {command ? (
        <p className="flex min-w-0 items-center gap-1">
          <code className="truncate font-mono text-code-caption text-fg">{command}</code>
          <CopyButton text={command} label="Copy the command" />
        </p>
      ) : null}
    </div>
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
        <p className="text-body">
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
  const from = wakesFrom(env);
  const waking = env.state === 'starting';
  if (env.resumed_by !== 'owner' || (!waking && !env.owner_woken)) return null;
  const kind = from === 'paused' ? 'paused machine' : from === 'files' ? 'saved files' : 'snapshot';
  return (
    <section role="status" aria-label={waking ? 'Waking' : 'Waiting for an agent'} className="flex items-start gap-3 rounded-xl border border-line bg-card px-5 py-4 shadow-lift">
      <StatusDot tone={waking ? 'live' : 'ok'} pulse={waking} className="mt-2" />
      <div className="grid min-w-0 gap-1 text-body">
        <p>
          <span className="font-medium text-fg">{waking ? `Waking from its ${kind}.` : 'Awake, and waiting for an agent.'}</span>{' '}
          <span className="text-muted">
            {waking
              ? from === 'paused'
                ? 'Usually under 10 seconds.'
                : from === 'files'
                  ? 'A few minutes.'
                  : env.machine.provider === 'docker'
                    ? 'Usually a few seconds.'
                    : 'Usually about a minute.'
              : `The next agent that opens "${env.title}" takes it over, with its work as it was.`}
          </span>
        </p>
        <p className="text-secondary text-faint">
          You resumed it {timeAgo(env.created_at).toLowerCase()}. {waking ? 'You can leave this page: it carries on.' : `Say "${continueLine(env.title)}" to an agent.`}
        </p>
      </div>
    </section>
  );
}

/** "Watched now, so it stays open": live views of its browser and connections to its app, in the header's line. */
export function WatchLine({ watched }: { watched: EnvWatched | null | undefined }) {
  if (!watched?.viewers) return null;
  const parts = [watched.live_views ? plural(watched.live_views, 'live view') : null, watched.port_connections ? plural(watched.port_connections, 'connection') + ' to its app' : null].filter(Boolean);
  return (
    <span className="inline-flex items-center gap-1" title={watched.since ? `Since ${dateTime(watched.since)}. While anyone watches, it does not close or save for being idle.` : 'While anyone watches, it does not close or save for being idle.'}>
      <Eye aria-hidden="true" className="size-4 text-faint" />
      Watched now ({parts.join(' and ')}), so it stays open
    </span>
  );
}
