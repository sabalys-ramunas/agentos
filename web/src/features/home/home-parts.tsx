/** The pieces of Home around its lists: the header and its sentence, the links at the foot, and Home with no workspace yet. */
import { Link } from '@tanstack/react-router';
import { ChevronRight, Server, SquareTerminal } from 'lucide-react';
import { useState } from 'react';
import type { ReactNode } from 'react';
import type {
  useJobs,
  useStudioRuns} from '@/api/hooks';
import {
  isStudioMissing
} from '@/api/hooks';
import { isNotServed } from '@/api/http';
import type { EnvList, Status, Workspace } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { KindTile } from '@/components/ui/kind';
import { StatusDot } from '@/components/ui/status';
import { StateGlyph } from '@/components/ui/state-badge';
import { EmptyState, Skeleton } from '@/components/ui/states';
import { cn } from '@/lib/cn';
import { money } from '@/features/needs-you/kinds';
import { plural } from '@/lib/format';
import { NewWorkspaceDialog } from '../settings/workspaces';
import { GettingStarted } from '../onboarding/getting-started';
import { HomeEnvironments } from '../environments/home-environments';
import { splitEnvironments } from '../environments/words';
import type { LinePart } from './sentence';
import type { homeSentence } from './sentence';
import type { todayOf } from './home';

export const inline = 'underline decoration-line-strong underline-offset-4 hover:decoration-fg';

/** One part of the line under the sentence: a link where it leads somewhere. */
function Part({ org, part }: { org: string; part: LinePart }) {
  switch (part.kind) {
    case 'trouble':
      if (part.to === 'check')
        return (
          <Link to="/$org/checks/$id" params={{ org, id: part.id }} className={cn(inline, 'inline-block text-fg')}>
            {part.text}
          </Link>
        );
      if (part.to === 'machine')
        return (
          <Link to="/$org/nodes/$id" params={{ org, id: part.id }} className={cn(inline, 'inline-block text-fg')}>
            {part.text}
          </Link>
        );
      {
        const [ws = '', name = ''] = part.id.split('/');
        return (
          <Link to="/$org/lanes/$ws/$name" params={{ org, ws, name }} className={cn(inline, 'inline-block text-fg')}>
            {part.text}
          </Link>
        );
      }
    case 'needs':
      return (
        <>
          <Link to="/$org/needs-you" params={{ org }} className={cn(inline, 'whitespace-nowrap text-fg')}>
            {part.text}
          </Link>
          {part.rest}
        </>
      );
    default:
      return <>{part.text}</>;
  }
}

const footLink = 'group flex min-w-0 items-center gap-3 rounded-xl border border-line bg-card px-5 py-4 shadow-lift transition-colors duration-(--duration-fast) hover:border-line-strong';

/** What a foot link says: its title with an icon, then the numbers. */
function FootBody({ icon, title, children }: { icon: ReactNode; title: string; children: ReactNode }) {
  return (
    <>
      <span className="grid min-w-0 flex-1 gap-1">
        <span className="flex items-center gap-2 text-caption text-faint [&_svg]:size-3.5">
          {icon}
          {title}
        </span>
        <span className="text-body">{children}</span>
      </span>
      <ChevronRight aria-hidden="true" className="size-4 shrink-0 text-faint transition-transform duration-(--duration-fast) group-hover:translate-x-0.5" />
    </>
  );
}

interface HomeHeaderProps {
  org: string;
  date: string;
  going: boolean;
  current: Workspace | null;
  workspaces: Workspace[];
  sentence: ReturnType<typeof homeSentence> | null;
}

/** The day, the workspace and Home's sentence: what is going on, said once. */
export function HomeHeader({ org, date, going, current, workspaces: data, sentence }: HomeHeaderProps) {
  return (
    <header className="relative isolate mb-10 pt-2">
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-x-0 -top-8 -z-10 h-56 bg-[radial-gradient(var(--line-strong)_1px,transparent_1px)] [background-size:16px_16px] [mask-image:linear-gradient(to_bottom,black,transparent)] opacity-60"
      />
      <p className="flex min-w-0 flex-wrap items-center gap-2 font-mono text-code-caption text-faint">
        {going ? <StatusDot tone="live" pulse /> : null}
        <span>{date}</span>
        <span aria-hidden="true">·</span>
        {current ? (
          <Link to="/$org/workspaces/$ws" params={{ org, ws: current.id }} className={inline}>
            {current.name}
          </Link>
        ) : (
          <span>{data.length === 1 ? data[0]!.name : plural(data.length, 'workspace')}</span>
        )}
      </p>
      {sentence ? (
        <>
          <h1 className="mt-3 max-w-4xl text-page-title-compact text-balance sm:text-display">{sentence.lead}</h1>
          {sentence.line.length ? (
            <p className="mt-3 max-w-3xl text-lede text-pretty text-muted">
              {sentence.line.map((part, index) => (
                <span key={index}>
                  <Part org={org} part={part} />.{index < sentence.line.length - 1 ? ' ' : ''}
                </span>
              ))}
            </p>
          ) : null}
        </>
      ) : (
        <div aria-busy="true">
          <h1 className="sr-only">Home</h1>
          <Skeleton className="mt-4 h-10 w-full max-w-2xl" />
          <Skeleton className="mt-4 h-5 w-full max-w-lg" />
        </div>
      )}
    </header>
  );
}

interface EarlierTodayProps {
  org: string;
  jobs: ReturnType<typeof useJobs>;
  studio: ReturnType<typeof useStudioRuns>;
  status: Status | undefined;
  today: ReturnType<typeof todayOf>;
}

/** Three links at the foot: the runs that ended today, the pages checked today, and the machines. */
export function EarlierToday({ org, jobs, studio, status, today }: EarlierTodayProps) {
  const { endedToday, failedToday, latest, openFindings, share, nodes, online, liveVms, vmCost } = today;
  return (
    <nav aria-label="Earlier today" className="mt-10 grid gap-4 md:grid-cols-3">
      <Link to="/$org/runs" params={{ org }} className={footLink}>
        <FootBody icon={<SquareTerminal aria-hidden="true" />} title="Earlier today">
        {jobs.isError ? (
          isNotServed(jobs.error) ? 'Runs are shown on your computer' : 'Could not read the runs'
        ) : !jobs.data ? (
          <Skeleton className="w-40" />
        ) : !jobs.data.available ? (
          'No runs on this computer'
        ) : endedToday.length ? (
          <>
            {plural(endedToday.length, 'run')} ended
            {failedToday ? (
              <>
                , <span className="text-bad">{failedToday} failed</span>
              </>
            ) : (
              ', none failed'
            )}
          </>
        ) : (
          'No runs ended today'
        )}
      </FootBody>
      </Link>
      <Link to="/$org/checks" params={{ org }} className={footLink}>
        <FootBody icon={<KindTile kind="design" size="xs" className="shadow-none" />} title="Pages">
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
      </FootBody>
      </Link>
      <Link to="/$org/nodes" params={{ org }} className={footLink}>
        <FootBody icon={<Server aria-hidden="true" />} title="Machines">
        {!status ? (
          <Skeleton className="w-40" />
        ) : nodes.length ? (
          <>
            {online} of {nodes.length} online
            {liveVms.length ? `, ${plural(liveVms.length, 'VM')} rented${vmCost ? `, ${money(vmCost, 'EUR')} so far` : ''}` : ''}
          </>
        ) : (
          'No machines yet'
        )}
      </FootBody>
      </Link>
    </nav>
  );
}

/** Home before there is a workspace: what runs anyway, and how to add the first one. */
export function NoWorkspaces({ org, date, envs, canSetUp }: { org: string; date: string; envs: EnvList | undefined; canSetUp: boolean }) {
  const [newWorkspace, setNewWorkspace] = useState(false);
  const openEnvs = envs ? splitEnvironments(envs.environments).open.length : 0;
  return (
    <>
      <header className="mb-10">
        <p className="font-mono text-code-caption text-faint">{date}</p>
        {openEnvs > 0 ? (
          <>
            <h1 className="mt-3 text-page-title-compact text-balance sm:text-display">
              Your agents are working in {plural(openEnvs, 'environment')}.
            </h1>
            <p className="mt-3 max-w-2xl text-lede text-muted">Each is a computer of its own, deleted when the work is done. Accounts and routines show here once you add a workspace.</p>
          </>
        ) : (
          <>
            <h1 className="mt-3 text-page-title-compact text-balance sm:text-display">Nothing is running yet.</h1>
            <p className="mt-3 max-w-2xl text-lede text-muted">Give your agents a workspace and a computer, and this fills up as they work.</p>
          </>
        )}
      </header>
      <HomeEnvironments org={org} />
      <GettingStarted org={org} />
      <Card className="overflow-hidden">
        <EmptyState
          icon={<StateGlyph state="sleeping" size={24} />}
          title="No workspaces yet"
          description={
            canSetUp
              ? 'A workspace is one business or person: Aster Coffee, or your own accounts. Connect a computer and choose what your AI should do first, and agentos adds the accounts for you.'
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
