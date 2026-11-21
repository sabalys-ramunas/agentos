/** The pieces of Home around its lists: the header and its sentence, the links at the foot, and Home with no workspace yet. */
import { Link } from '@tanstack/react-router';
import { CircleCheck, CirclePlay, Server } from 'lucide-react';
import { useState } from 'react';
import type {
  useJobs,
  useStudioRuns} from '@/api/hooks';
import {
  isStudioMissing
} from '@/api/hooks';
import { isNotServed } from '@/api/http';
import type { EnvList, Status } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { StateGlyph } from '@/components/ui/state-badge';
import { EmptyState, Skeleton } from '@/components/ui/states';
import { money } from '@/features/needs-you/kinds';
import { plural } from '@/lib/format';
import { NewWorkspaceDialog } from '../settings/workspaces';
import { GettingStarted } from '../onboarding/getting-started';
import { splitEnvironments } from '../environments/words';
import { HomeHero } from './home-hero';
import type { todayOf } from './home';
import { WorkingNow } from './working-now';

const footLink = 'group inline-flex min-h-8 min-w-0 items-center gap-2 text-body text-muted transition-colors duration-(--duration-fast) hover:text-fg [&_svg]:size-4 [&_svg]:shrink-0 [&_svg]:text-faint';

interface EarlierTodayProps {
  org: string;
  jobs: ReturnType<typeof useJobs>;
  studio: ReturnType<typeof useStudioRuns>;
  status: Status | undefined;
  today: ReturnType<typeof todayOf>;
}

/** One quiet row at the foot: the runs that ended today, the pages checked today, and the machines. */
export function EarlierToday({ org, jobs, studio, status, today }: EarlierTodayProps) {
  const { endedToday, failedToday, latest, openFindings, share, nodes, online, liveVms, vmCost } = today;
  return (
    <nav aria-labelledby="home-earlier" className="mt-10 flex flex-wrap items-center gap-x-8 gap-y-2">
      <h2 id="home-earlier" className="text-label font-semibold text-fg">
        Earlier today
      </h2>
      <Link to="/$org/runs" params={{ org }} className={footLink}>
        <CirclePlay aria-hidden="true" />
        {jobs.isError ? (
          isNotServed(jobs.error) ? 'Runs are shown on your computer' : 'Could not read the runs'
        ) : !jobs.data ? (
          <Skeleton className="w-40" />
        ) : !jobs.data.available ? (
          'No runs on this computer'
        ) : endedToday.length ? (
          <span>
            {plural(endedToday.length, 'run')} ended
            {failedToday ? (
              <>
                , <span className="text-bad">{failedToday} failed</span>
              </>
            ) : (
              ', none failed'
            )}
          </span>
        ) : (
          'No runs ended today'
        )}
      </Link>
      <Link to="/$org/checks" params={{ org }} className={footLink}>
        <CircleCheck aria-hidden="true" />
        {studio.isError ? (
          isNotServed(studio.error) ? 'Checks are shown on your computer' : 'Could not read the checks'
        ) : !studio.data ? (
          <Skeleton className="w-40" />
        ) : isStudioMissing(studio.data) ? (
          'Studio is not on this computer'
        ) : latest.length ? (
          `${plural(latest.length, 'page')} checked today, ${openFindings ? plural(openFindings, 'finding') : 'no findings'}${share !== null ? `, ${Math.round(share * 100)}% on the design system` : ''}`
        ) : (
          'No page checked today'
        )}
      </Link>
      <Link to="/$org/nodes" params={{ org }} className={footLink}>
        <Server aria-hidden="true" />
        {!status ? (
          <Skeleton className="w-40" />
        ) : nodes.length ? (
          <span>
            Machines: {online} of {nodes.length} online
            {liveVms.length ? `, ${plural(liveVms.length, 'VM')} rented${vmCost ? `, ${money(vmCost, 'EUR')} so far` : ''}` : ''}
          </span>
        ) : (
          'No machines yet'
        )}
      </Link>
    </nav>
  );
}

/** Home before there is a workspace: what runs anyway, and how to add the first one. */
export function NoWorkspaces({ org, now, date, envs, canSetUp }: { org: string; now: number; date: string; envs: EnvList | undefined; canSetUp: boolean }) {
  const [newWorkspace, setNewWorkspace] = useState(false);
  const open = envs ? splitEnvironments(envs.environments).open : [];
  // A machine that stopped is open until it is closed, but nobody works in it.
  const working = open.filter((env) => env.state !== 'gone').length;
  const stopped = open.length - working;
  return (
    <>
      <HomeHero
        org={org}
        now={now}
        date={date}
        current={null}
        workspaces={[]}
        sentence={null}
        title={
          working > 0
            ? `Your agents are working in ${plural(working, 'environment')}.`
            : stopped > 0
              ? `${stopped === 1 ? "An agent's machine" : `${stopped} agents' machines`} stopped.`
              : 'Nothing is running yet.'
        }
        lede={
          working > 0
            ? 'Each is a computer of its own, deleted when the work is done. Accounts and routines show here once you add a workspace.'
            : stopped > 0
              ? stopped === 1
                ? 'Close it, or ask the agent to open the same title again: it starts on a new machine.'
                : 'Close them, or ask the agents to open the same titles again: each starts on a new machine.'
              : 'Give your agents a workspace and a computer, and this fills up as they work.'
        }
      />
      <WorkingNow org={org} />
      <div className="mt-10">
        <GettingStarted org={org} />
      </div>
      <Card className="overflow-hidden">
        <EmptyState
          icon={<StateGlyph state="sleeping" size={24} />}
          title="No workspaces yet"
          description={
            canSetUp
              ? 'A workspace is one business or person: Aster Coffee, or your own accounts. Connect a computer and choose what your AI should do first, and openajar adds the accounts for you.'
              : 'A workspace is one business or person. An owner or admin of this organization sets them up, and yours appear here.'
          }
          action={
            canSetUp ? (
              <div className="flex flex-wrap justify-center gap-2">
                <Button variant="primary" onClick={() => setNewWorkspace(true)}>
                  New workspace
                </Button>
                <Button asChild>
                  <Link to="/onboarding/$org/$step" params={{ org, step: 'node' }}>
                    Use the setup guide
                  </Link>
                </Button>
              </div>
            ) : undefined
          }
        />
      </Card>
      <NewWorkspaceDialog org={org} open={newWorkspace} onOpenChange={setNewWorkspace} />
    </>
  );
}
