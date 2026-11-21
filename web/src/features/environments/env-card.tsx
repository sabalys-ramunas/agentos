/**
 * An open environment as a card (web/DESIGN.md, "The screen's picture"): the picture of its screen with its
 * state and the time left on it as glass chips, its title (two lines at most), and who works in it with what
 * it is doing. The Environments list and Home's Working now show the same card, so the two agree.
 */
import { Link } from '@tanstack/react-router';
import { Clock } from 'lucide-react';
import type { EnvState, EnvSummary } from '@/api/types';
import { PictureChip, ScreenPicture } from '@/components/screen-picture';
import type { PictureState } from '@/components/screen-picture';
import { Card } from '@/components/ui/card';
import { Skeleton } from '@/components/ui/states';
import { StatusDot } from '@/components/ui/status';
import { ENV_DOING } from '../home/words';
import { ENV_STATUS, SHORT_MS, leftOf, leftWords, span } from './words';

/** What the picture draws for each state when there is no still. */
export const PICTURE: Record<EnvState, PictureState> = {
  starting: 'starting',
  ready: 'awake',
  working: 'awake',
  idle: 'awake',
  closing: 'asleep',
  closed: 'asleep',
  failed: 'asleep',
};

export function EnvCard({ org, env, now }: { org: string; env: EnvSummary; now: number }) {
  const status = ENV_STATUS[env.state];
  const left = leftOf(env, now);
  return (
    <li className="min-w-0">
      <Link
        to="/$org/environments/$id"
        params={{ org, id: env.id }}
        aria-label={`${env.title}. ${status.label}${left !== null ? `, ${leftWords(left)}` : ''}`}
        className="group block h-full rounded-xl focus-visible:outline-offset-2"
      >
        <Card className="flex h-full flex-col overflow-hidden p-2 transition-[border-color,background-color] duration-(--duration-fast) ease-standard group-hover:border-line-strong group-active:bg-inset">
          <ScreenPicture state={PICTURE[env.state]} title={env.title} screen={env.screen} className="rounded-md">
            <span className="absolute top-2 left-2">
              <PictureChip>
                <StatusDot tone={status.tone} starting={status.starting} />
                {status.label}
              </PictureChip>
            </span>
            {left !== null ? (
              <span className="absolute top-2 right-2">
                <PictureChip title={left < SHORT_MS ? 'Little time left before the node closes it' : 'Time left before the node closes it'}>
                  {left < SHORT_MS ? <StatusDot tone="warn" /> : <Clock aria-hidden="true" className="size-4" />}
                  <span className="tabular">{left <= 0 ? 'Ends now' : span(left)}</span>
                </PictureChip>
              </span>
            ) : null}
          </ScreenPicture>
          <div className="px-3 pt-3 pb-3">
            <p className="line-clamp-2 text-lede font-semibold text-fg [overflow-wrap:anywhere]" title={env.title}>
              {env.title}
            </p>
            <p className="mt-1 truncate text-secondary text-muted">
              {env.agent.name} · {ENV_DOING[env.state]}
            </p>
          </div>
        </Card>
      </Link>
    </li>
  );
}

/** Cards while the list is read: the same shape, so nothing moves when it arrives. */
export function EnvCardsSkeleton({ count = 3, label = 'Loading environments' }: { count?: number; label?: string }) {
  return (
    <ul aria-busy="true" aria-label={label} className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
      {Array.from({ length: count }, (_, index) => (
        <li key={index} className={index === 1 ? 'max-sm:hidden' : index > 1 ? 'max-lg:hidden' : undefined}>
          <Card className="p-2">
            <Skeleton className="aspect-video h-auto w-full rounded-md" />
            <div className="grid gap-2 px-3 pt-3 pb-3">
              <Skeleton className="h-6 w-2/3" />
              <Skeleton className="w-1/2" />
            </div>
          </Card>
        </li>
      ))}
    </ul>
  );
}
