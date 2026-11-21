/**
 * Notifications, under the bell (the drawings' notifications.png): what needs you first, each read as on Needs you with
 * the wrapper tile of its kind, then what happened earlier that a person should know (a run that failed, an account
 * switched off or waiting for a hand). Two views: Needs you and Everything. The foot opens Needs you and the settings.
 */
import { Link } from '@tanstack/react-router';
import { Bell, CircleAlert, FlaskConical } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import type { ReactNode } from 'react';
import { useState } from 'react';
import { useApprovals, useJobs, useWorkspaces } from '@/api/hooks';
import { cn } from '@/lib/cn';
import { timeAgo } from '@/lib/format';
import { needsHand } from '@/lib/lanes';
import { usePrincipalName } from '@/lib/use-principal';
import { AskTile } from '@/features/needs-you/ask-tile';
import { tileOf } from '@/features/needs-you/if-yes';
import { handOf, rowTitleOf, whereOf } from '@/features/needs-you/words';
import { JOB_STATUS, titleOf } from '@/features/runs/words';
import { Popover, PopoverContent, PopoverTrigger } from '../ui/menu';
import { SegmentedControl } from '../ui/tabs';
import { AppTile } from '../ui/tile';
import type { TileTone } from '../ui/tile';
import { useRailCounts } from './counts';

const DAY = 86_400_000;

function Row({ tile, title, line, to }: { tile: ReactNode; title: string; line: string; to: ReactNode }) {
  return (
    <li className="relative">
      <div className="flex items-start gap-3 rounded-lg px-3 py-2 hover:bg-hover">
        {tile}
        <span className="grid min-w-0 flex-1">
          <span title={title} className="line-clamp-2 text-secondary font-medium text-fg">
            {title}
          </span>
          <span className="truncate text-caption text-faint">{line}</span>
        </span>
      </div>
      {to}
    </li>
  );
}

function Tile({ tone, icon }: { tone: TileTone; icon: LucideIcon }) {
  return <AppTile tone={tone} icon={icon} size="sm" />;
}

export function NotificationsBell({ org, triggerClass }: { org: string; triggerClass: string }) {
  const [view, setView] = useState<'needs' | 'all'>('needs');
  const approvals = useApprovals(org).data ?? [];
  const workspaces = useWorkspaces(org).data ?? [];
  const jobs = useJobs(org, { limit: 50 }).data?.jobs ?? [];
  const nameOf = usePrincipalName(org);
  const count = useRailCounts(org).needsYou;
  const business = (lane: string) => workspaces.find((item) => item.id === lane.split('/')[0])?.name ?? null;
  const needs = [...approvals].sort((a, b) => b.created_at.localeCompare(a.created_at)).slice(0, 5);
  const now = Date.now();
  const failed = jobs.filter((job) => (job.state === 'failed' || job.state === 'timed_out' || job.state === 'lost') && now - Date.parse(job.started_at) < DAY).slice(0, 3);
  const hands = workspaces.flatMap((workspace) => workspace.lanes.filter(needsHand).map((lane) => ({ lane, name: workspace.name }))).slice(0, 3);
  const earlier = view === 'all' && (failed.length > 0 || hands.length > 0);
  const cover = 'absolute inset-0 rounded-lg focus-inset';

  return (
    <Popover>
      <PopoverTrigger className={cn(triggerClass, 'relative size-9 shrink-0 justify-center px-0 max-md:size-10')} aria-label={count ? `Needs you, ${count} waiting` : 'Needs you'}>
        <Bell className="size-[18px]" aria-hidden="true" />
        {count > 0 ? <span className="absolute top-[7px] right-[8px] size-2 rounded-full bg-primary outline-2 outline-page" aria-hidden="true" /> : null}
      </PopoverTrigger>
      <PopoverContent className="w-100 p-0">
        <div className="grid gap-3 px-4 pt-4 pb-2">
          <p className="text-label font-semibold text-fg">Notifications</p>
          <SegmentedControl
            label="Show"
            value={view}
            onValueChange={setView}
            options={[
              { value: 'needs', label: <>Needs you{count ? <span className="tabular text-faint">{count}</span> : null}</> },
              { value: 'all', label: 'Everything' },
            ]}
            className="justify-self-start"
          />
        </div>
        <div className="max-h-[min(440px,60dvh)] overflow-y-auto px-1 pb-2">
          {needs.length ? (
            <ul>
              {needs.map((item) => {
                const where = whereOf(item, business(item.lane));
                const title = rowTitleOf(item, where);
                return (
                  <Row
                    key={item.id}
                    tile={<AskTile kind={tileOf(item)} />}
                    title={where.business && !title.includes(where.business) ? `${title} for ${where.business}` : title}
                    line={[where.business, `${timeAgo(item.created_at)}`].filter(Boolean).join(' · ')}
                    to={<Link to="/$org/needs-you" params={{ org }} search={{ approval: item.id }} aria-label={title} className={cover} />}
                  />
                );
              })}
            </ul>
          ) : (
            <p className="px-3 py-4 text-secondary text-muted">You are all caught up. Nothing is waiting for you.</p>
          )}
          {earlier ? (
            <>
              <p className="px-3 pt-3 pb-1 text-caption font-medium text-faint">Earlier</p>
              <ul>
                {failed.map((job) => (
                  <Row
                    key={job.id}
                    tile={<Tile tone="ink" icon={FlaskConical} />}
                    title={`${titleOf(job)}: ${job.tests?.failed ? `${job.tests.failed} ${job.tests.failed === 1 ? 'test' : 'tests'} failed` : JOB_STATUS[job.state].label}`}
                    line={timeAgo(job.started_at)}
                    to={<Link to="/$org/runs/$id" params={{ org, id: job.id }} aria-label={titleOf(job)} className={cover} />}
                  />
                ))}
                {hands.map(({ lane, name }) => {
                  const hand = handOf(lane, name, (principal) => nameOf(principal));
                  return (
                    <Row
                      key={lane.id}
                      tile={<Tile tone="amber" icon={CircleAlert} />}
                      title={hand.title}
                      line={name}
                      to={<Link to="/$org/lanes/$ws/$name" params={{ org, ws: lane.workspace, name: lane.name }} aria-label={hand.title} className={cover} />}
                    />
                  );
                })}
              </ul>
            </>
          ) : null}
        </div>
        <div className="flex items-center justify-between gap-2 border-t border-line bg-inset px-4 py-3 text-secondary">
          <Link to="/$org/needs-you" params={{ org }} className="font-medium text-accent-text hover:underline">
            Open Needs you
          </Link>
          <Link to="/$org/settings/notifications" params={{ org }} className="text-muted hover:text-fg hover:underline">
            Settings for notifications
          </Link>
        </div>
      </PopoverContent>
    </Popover>
  );
}
