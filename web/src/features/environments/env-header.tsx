/**
 * The top of an environment's page, on the night band: its name and state (and "You have control" while it is
 * yours), who works in it, since when, how it stays and its repository, and more time and Close on glass.
 * On a phone the two buttons move under the panels, where a thumb reaches them.
 */
import { Clock } from 'lucide-react';
import { toast } from 'sonner';
import { useAccess, useExtendEnvironment } from '@/api/hooks';
import { ApiError } from '@/api/http';
import type { EnvDetail } from '@/api/types';
import { PictureChip } from '@/components/screen-picture';
import { Button } from '@/components/ui/button';
import { Menu, MenuContent, MenuItem, MenuTrigger } from '@/components/ui/menu';
import { StatusDot } from '@/components/ui/status';
import { cn } from '@/lib/cn';
import { clock, dateTime, timeAgo } from '@/lib/format';
import { CloseEnvironment } from './close-flow';
import { WatchLine } from './resume';
import { ENV_STATE_SENTENCE, ENV_STATUS, KEEP, agentWords, isOpen, keepOf, leftOf, leftWords, repoName } from './words';

export const EXTEND = [
  { minutes: 30, label: '30 minutes' },
  { minutes: 60, label: '1 hour' },
  { minutes: 120, label: '2 hours' },
];

/** May this person give it more time or close it, now? */
export function useActionable(org: string, env: EnvDetail): boolean {
  const access = useAccess(org);
  return isOpen(env) && env.state !== 'closing' && access.can('lanes.operate');
}

/** One fact after the first, with its dot, kept together on one line. */
function Part({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <span className={cn('whitespace-nowrap', className)}>
      <span aria-hidden="true" className="px-2 text-faint">
        ·
      </span>
      {children}
    </span>
  );
}

/** `actions`: Extend and Close sit here (from 768 px); on a phone they follow the panels (PhoneActions). */
export function EnvHeader({ org, env, now, yours, actions }: { org: string; env: EnvDetail; now: number; yours: boolean; actions: boolean }) {
  const actionable = useActionable(org, env);
  const status = ENV_STATUS[env.state];
  const left = leftOf(env, now);
  return (
    <header className="grid gap-x-6 gap-y-3 md:grid-cols-[minmax(0,1fr)_auto] md:items-start">
      <div className="grid min-w-0 gap-2">
        <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-2">
          <h1 tabIndex={-1} className="min-w-0 text-page-title-compact text-fg [overflow-wrap:anywhere] md:text-page-title">
            {env.title}
          </h1>
          <span title={yours ? 'Your keyboard and mouse go to its screen' : ENV_STATE_SENTENCE[env.state]}>
            <PictureChip className="h-8 text-label">
              {yours ? <span aria-hidden="true" className="size-2 rounded-full bg-[var(--sidebar-text)]" /> : <StatusDot tone={status.tone} starting={status.starting} />}
              {yours ? 'You have control' : status.label}
            </PictureChip>
          </span>
        </div>
        {/* Each fact is one flex item with its dot, so a line breaks between facts (JSX leaves no space there)
            and no dot is left at the end of a line. */}
        <p className="flex min-w-0 flex-wrap items-baseline text-body text-muted">
          <span className="whitespace-nowrap">{agentWords(env.agent)}</span>
          <Part className={cn(left !== null && 'max-md:hidden')}>
            <span title={dateTime(env.created_at)}>opened {timeAgo(env.created_at, now).toLowerCase()}</span>
          </Part>
          {left !== null ? <Part className="md:hidden">{leftWords(left)}</Part> : null}
          {env.keep ? (
            <Part>
              <span title={KEEP[keepOf(env)].sentence}>{KEEP[keepOf(env)].label}</span>
            </Part>
          ) : null}
          {env.repo ? (
            <Part className="max-md:hidden">
              <span className="inline-block max-w-80 truncate align-bottom font-mono text-code text-faint" title={env.repo}>
                {repoName(env.repo)}
              </span>
            </Part>
          ) : null}
        </p>
        {isOpen(env) && env.state !== 'gone' && env.watched?.viewers ? (
          <p className="text-secondary text-muted">
            <WatchLine watched={env.watched} />
          </p>
        ) : null}
      </div>
      {actionable && actions ? (
        <div className="flex items-center gap-2">
          {env.state === 'gone' ? null : <ExtendMenu org={org} env={env} glass />}
          <CloseEnvironment org={org} env={env} glass />
        </div>
      ) : null}
    </header>
  );
}

/** Extend and Close side by side, full width, for a phone (under the panels). */
export function PhoneActions({ org, env, className }: { org: string; env: EnvDetail; className?: string }) {
  if (!useActionable(org, env)) return null;
  return (
    <div className={cn('grid gap-3', env.state === 'gone' ? 'grid-cols-1' : 'grid-cols-2', className)}>
      {env.state === 'gone' ? null : <ExtendMenu org={org} env={env} className="h-11 w-full" />}
      <CloseEnvironment org={org} env={env} className="h-11 w-full" />
    </div>
  );
}

function ExtendMenu({ org, env, glass = false, className }: { org: string; env: EnvDetail; glass?: boolean; className?: string }) {
  const extend = useExtendEnvironment(org);
  return (
    <Menu>
      <MenuTrigger asChild>
        <Button variant={glass ? 'glass' : 'secondary'} className={className} icon={<Clock className="size-4" />} loading={extend.isPending}>
          Extend
        </Button>
      </MenuTrigger>
      <MenuContent align="end" aria-label="Extend by">
        {EXTEND.map((option) => (
          <MenuItem
            key={option.minutes}
            onSelect={() =>
              extend.mutate(
                { id: env.id, minutes: option.minutes },
                {
                  onSuccess: (next) => toast.success(`Extended by ${option.label}. The node now closes it at ${clock(next.expires_at)}.`),
                  onError: (error) => toast.error(error instanceof ApiError ? error.message : 'It could not be extended. Nothing was changed.'),
                },
              )
            }
          >
            Extend by {option.label}
          </MenuItem>
        ))}
      </MenuContent>
    </Menu>
  );
}
