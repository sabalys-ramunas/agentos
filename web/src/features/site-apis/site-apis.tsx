/**
 * Site APIs, as a person comes to them: what their websites are serving, what needs them, and what an
 * agent has proposed. The page answers in a sentence, then shows the week of calls, then the work.
 * What needs a person comes first and takes one line each with its one next step. What is answering
 * gets the room, each in the shape of its own job. Concept stage: answered by the mock only.
 */
import { Link, useParams } from '@tanstack/react-router';
import { ArrowRight, Plus, X } from 'lucide-react';
import { useMemo, useState } from 'react';
import { useSiteApis, useWorkspaces } from '@/api/hooks';
import { isNotServed } from '@/api/http';
import type { SiteApi, Workspace } from '@/api/types';
import { BusinessMark } from '@/components/brand/identity';
import { SiteMark } from '@/components/brand/site-mark';
import { StackedBars } from '@/components/charts/stacked-bars';
import { Button, IconButton } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { StateBadge, StateGlyph } from '@/components/ui/state-badge';
import { EmptyState, ErrorState, NotLive, Skeleton } from '@/components/ui/states';
import { cn } from '@/lib/cn';
import { compact, plural, timeAgo } from '@/lib/format';
import { hueOf } from './page-sheet';
import { Sample, sampleLine, Traffic } from './samples';
import { HEALTH_WORD, markOf, problemWords, STATE_OF } from './words';
import './site-apis.css';

const DISMISSED = 'agentos.site-apis.intro-dismissed';
const HOURS_6 = 6 * 3_600_000;

const NEXT_STEP: Record<SiteApi['health'], string> = {
  good: 'Open',
  changed: 'Show me',
  sign_in: 'Sign in again',
  site_check: 'Answer the site',
  empty: 'Show me',
  resting: 'See it',
  paused: 'Resume it',
  unread: 'Open',
};

function Hero({ apis }: { apis: SiteApi[] }) {
  const active = apis.filter((api) => api.stage !== 'proposed');
  const good = active.filter((api) => api.health === 'good').length;
  const needs = active.filter((api) => ['changed', 'sign_in', 'site_check', 'empty'].includes(api.health)).length;
  const calls = active.reduce((sum, api) => sum + api.calls_7d, 0);
  const saved = active.reduce((sum, api) => sum + (api.calls_7d * (api.cached_pct ?? 0)) / 100, 0);
  return (
    <div className="min-w-0">
      <h1 className="text-headline text-balance sm:text-40">
        {plural(good, 'website is', 'websites are')} answering. {needs ? <span className="text-faint">{plural(needs, 'needs', 'need')} you.</span> : null}
      </h1>
      <p className="mt-3 max-w-2xl text-lede text-pretty text-muted">
        <strong className="font-semibold text-fg">{compact(calls)} calls</strong> this week, and <strong className="font-semibold text-fg">{calls ? Math.round((saved / calls) * 100) : 0}%</strong> never touched the site, because a saved answer was still fresh.
      </p>
    </div>
  );
}

/** A page, a ribbon and a table, small enough to read as one idea. */
function Picture() {
  const columns = [0, 1, 2, 3];
  return (
    <div aria-hidden="true" className="relative w-72 shrink-0 max-md:hidden">
      <div className="rounded-md border border-board-line bg-card p-2">
        <div className="mb-1 h-1 w-16 rounded-full bg-fill-strong" />
        <div className="grid grid-cols-5 gap-1">
          {columns.map((column) => (
            <div key={column} className="grid gap-1 rounded-sm p-1 shadow-[inset_0_0_0_1px_var(--hue)]" style={{ '--hue': hueOf(column) } as React.CSSProperties}>
              {[0, 1, 2].map((row) => (
                <span key={row} className="h-1 rounded-full bg-fill-strong" />
              ))}
            </div>
          ))}
          <div className="grid gap-1 p-1 opacity-40">
            {[0, 1, 2].map((row) => (
              <span key={row} className="h-1 rounded-full bg-fill-strong" />
            ))}
          </div>
        </div>
      </div>
      <svg viewBox="0 0 288 28" className="block h-7 w-full">
        {columns.map((column) => {
          const from = 34 + column * 55;
          const to = 40 + column * 70;
          return <path key={column} d={`M${from} 0 C${from} 14 ${to} 14 ${to} 28`} fill="none" stroke={hueOf(column)} strokeOpacity={0.7} />;
        })}
      </svg>
      <div className="rounded-md border border-board-line bg-card p-2">
        <div className="grid grid-cols-4 gap-2">
          {columns.map((column) => (
            <span key={column} className="size-2 rounded-full" style={{ backgroundColor: hueOf(column) }} />
          ))}
        </div>
        {[0, 1].map((row) => (
          <div key={row} className="mt-2 grid grid-cols-4 gap-2 border-t border-line pt-2">
            {columns.map((column) => (
              <span key={column} className="h-1 rounded-full bg-fill-strong" />
            ))}
          </div>
        ))}
      </div>
    </div>
  );
}

function Intro({ org, onClose }: { org: string; onClose: () => void }) {
  return (
    <section aria-label="What a Site API is" className="picture-stage relative mt-8 flex items-center gap-8 rounded-(--radius-card) border border-line p-6">
      <div className="min-w-0 flex-1">
        <h2 className="text-heading text-balance">Turn a website you use into a live table your tools can read.</h2>
        <p className="mt-2 max-w-xl text-body text-pretty text-muted">
          Show it a page once. From then on your computer reads that page from your own signed-in account, so a sheet, a routine or a developer can have the answer without anyone opening the site.
        </p>
        <div className="mt-4 flex items-center gap-2">
          <Button variant="primary" asChild>
            <Link to="/$org/site-apis/new" params={{ org }}>
              Teach it a page
            </Link>
          </Button>
          <Button variant="ghost" asChild>
            <Link to="/$org/site-apis/$api" params={{ org, api: 'nordic-beans' }}>
              See one working
            </Link>
          </Button>
        </div>
      </div>
      <Picture />
      <IconButton label="Hide this" size="sm" className="absolute top-2 right-2" onClick={onClose}>
        <X aria-hidden="true" className="size-4" />
      </IconButton>
    </section>
  );
}

function Week({ apis }: { apis: SiteApi[] }) {
  const buckets = useMemo(() => {
    const length = Math.max(0, ...apis.map((api) => api.spark.length));
    const start = Math.floor(Date.now() / HOURS_6) * HOURS_6 - (length - 1) * HOURS_6;
    return Array.from({ length }, (_, index) => ({
      at: new Date(start + index * HOURS_6).toISOString(),
      values: apis.reduce(
        (sum, api) => {
          const bucket = api.spark[index];
          return bucket ? { cached: sum.cached + bucket.cached, fresh: sum.fresh + bucket.fresh, errors: sum.errors + bucket.errors } : sum;
        },
        { cached: 0, fresh: 0, errors: 0 },
      ),
    }));
  }, [apis]);
  return (
    <section aria-label="Calls this week" className="mt-8">
      <StackedBars
        label="Calls to every Site API this week, by how they were answered"
        bucketSeconds={21_600}
        height={104}
        format={compact}
        series={[
          { key: 'cached', label: 'From a saved answer', color: 'var(--mark-cached)' },
          { key: 'fresh', label: 'Read fresh from the site', color: 'var(--mark-fresh)' },
          { key: 'errors', label: 'Could not answer', color: 'var(--mark-failed)' },
        ]}
        buckets={buckets}
      />
    </section>
  );
}

function Proposal({ org, api, workspace }: { org: string; api: SiteApi; workspace: Workspace | undefined }) {
  const { proposal } = api;
  if (!proposal) return null;
  return (
    <section aria-label="Proposed by an agent">
      <div className="grid gap-4 rounded-(--radius-card) border border-dashed border-line-strong p-4 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)_auto] md:items-center">
        <div className="min-w-0">
          <p className="flex items-center gap-2 text-caption text-faint">
            {workspace ? <BusinessMark name={workspace.name} color={workspace.color} size={16} /> : null}
            Proposed by {proposal.by}, {timeAgo(proposal.at).toLowerCase()}
          </p>
          <h2 className="mt-1 text-section-title">{api.title}</h2>
          <p className="mt-1 text-secondary text-pretty text-muted">
            {proposal.tried_rows === undefined ? null : `It read ${plural(proposal.tried_rows, 'row')} once to try it. `}Nothing runs until a person turns it on.
          </p>
        </div>
        <div className="min-w-0 rounded-lg border border-line bg-inset p-3">
          <Sample sample={api.sample} />
        </div>
        <Button variant="secondary" asChild>
          <Link to="/$org/site-apis/$api" params={{ org, api: api.key }}>
            Look and decide
          </Link>
        </Button>
      </div>
    </section>
  );
}

/** A site API a person saved and has not turned on. It runs nothing until they do, and it stays on the list. */
function Saved({ org, api, workspace }: { org: string; api: SiteApi; workspace: Workspace | undefined }) {
  return (
    <div className="grid gap-3 rounded-(--radius-card) border border-dashed border-line-strong p-4 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-center">
      <div className="min-w-0">
        <p className="flex items-center gap-2 text-caption text-faint">
          {workspace ? <BusinessMark name={workspace.name} color={workspace.color} size={16} /> : null}
          {api.site.name}
        </p>
        <h3 className="mt-1 text-15 font-semibold">{api.title}</h3>
        <p className="mt-1 text-secondary text-pretty text-muted">Saved, and nothing runs until a person turns it on.</p>
      </div>
      <Button variant="secondary" asChild>
        <Link to="/$org/site-apis/$api" params={{ org, api: api.key }}>
          Open it
        </Link>
      </Button>
    </div>
  );
}

function ProblemRow({ org, api }: { org: string; api: SiteApi }) {
  const words = problemWords(api);
  return (
    <li>
      <Link
        to="/$org/site-apis/$api"
        params={{ org, api: api.key }}
        className="group grid gap-x-4 gap-y-1 px-4 py-3 transition-colors duration-(--duration-fast) ease-standard hover:bg-hover active:bg-pressed md:grid-cols-[32px_minmax(0,1.5fr)_minmax(0,1fr)_112px_128px] md:items-center"
      >
        <span className="relative hidden md:block">
          <SiteMark site={markOf(api.site)} size={32} tile label={false} className="bg-card" />
          <span className="absolute -top-1 -right-1 rounded-full bg-card p-px">
            <StateGlyph state={STATE_OF[api.health]} size={14} />
          </span>
        </span>
        <span className="min-w-0">
          <span className="block truncate text-label">{words?.title}</span>
          <span className="block truncate text-secondary text-muted">{api.title}</span>
        </span>
        <span className="min-w-0">
          <span className="tabular block truncate text-secondary text-muted">{sampleLine(api.sample)}</span>
          <span className="block text-caption text-faint">Last good read {timeAgo(api.read_at).toLowerCase()}</span>
        </span>
        <Traffic spark={api.spark} className="hidden md:block" />
        <span className="flex h-7 items-center justify-end gap-1 text-secondary font-medium text-fg">
          {NEXT_STEP[api.health]}
          <ArrowRight aria-hidden="true" className="size-4 transition-transform duration-(--duration-fast) ease-standard group-hover:translate-x-1" />
        </span>
      </Link>
    </li>
  );
}

function ApiCard({ org, api, workspace }: { org: string; api: SiteApi; workspace: Workspace | undefined }) {
  return (
    <Link
      to="/$org/site-apis/$api"
      params={{ org, api: api.key }}
      className={cn(
        'group flex min-w-0 flex-col rounded-(--radius-card) border border-line bg-card p-4 shadow-(--shadow-card)',
        'transition-[border-color,box-shadow,transform] duration-(--duration-base) ease-standard hover:border-line-strong hover:shadow-(--shadow-hover) active:translate-y-px',
      )}
    >
      <div className="flex items-start gap-3">
        <SiteMark site={markOf(api.site)} size={32} tile label={false} className="bg-card" />
        <div className="min-w-0 flex-1">
          <h3 className="truncate text-section-title">{api.title}</h3>
          <p className="flex min-w-0 items-center gap-2 text-caption text-faint">
            {workspace ? <BusinessMark name={workspace.name} color={workspace.color} size={14} /> : null}
            <span className="truncate">{workspace?.name}</span>
            <span className="mono truncate">{api.site.domain}</span>
          </p>
        </div>
        <ArrowRight aria-hidden="true" className="size-4 shrink-0 text-faint opacity-0 transition-[opacity,transform] duration-(--duration-fast) ease-standard group-hover:translate-x-1 group-hover:opacity-100" />
      </div>
      <p className="mt-3 text-body text-pretty text-muted">{api.words}</p>
      <div className="mt-4 flex-1">
        <Sample sample={api.sample} />
      </div>
      <div className="mt-4 flex items-center gap-3 border-t border-line pt-3 text-caption text-faint">
        <Traffic spark={api.spark} />
        <span className="tabular min-w-0 flex-1 truncate">
          <span className="font-medium text-fg">{compact(api.calls_7d)}</span> calls this week{api.cached_pct === null ? '' : `, ${api.cached_pct}% saved`}
        </span>
        <span className="shrink-0">Read {timeAgo(api.read_at).toLowerCase()}</span>
        <StateBadge state="good" label={HEALTH_WORD.good} size={14} className="shrink-0 text-caption" />
      </div>
    </Link>
  );
}

/** The lead: the busiest one, with the room to read its rows beside what it says. */
function LeadCard({ org, api, workspace }: { org: string; api: SiteApi; workspace: Workspace | undefined }) {
  return (
    <Link
      to="/$org/site-apis/$api"
      params={{ org, api: api.key }}
      className={cn(
        'group grid min-w-0 gap-6 rounded-(--radius-card) border border-line bg-card p-4 shadow-(--shadow-card) md:col-span-2 md:grid-cols-[minmax(0,5fr)_minmax(0,7fr)]',
        'transition-[border-color,box-shadow,transform] duration-(--duration-base) ease-standard hover:border-line-strong hover:shadow-(--shadow-hover) active:translate-y-px',
      )}
    >
      <div className="flex min-w-0 flex-col">
        <div className="flex items-start gap-3">
          <SiteMark site={markOf(api.site)} size={40} tile label={false} className="bg-card" />
          <div className="min-w-0 flex-1">
            <h3 className="text-heading text-balance">{api.title}</h3>
            <p className="mt-1 flex min-w-0 items-center gap-2 text-caption text-faint">
              {workspace ? <BusinessMark name={workspace.name} color={workspace.color} size={14} /> : null}
              <span className="truncate">{workspace?.name}</span>
              <span className="mono truncate">{api.site.domain}</span>
            </p>
          </div>
        </div>
        <p className="mt-3 text-body text-pretty text-muted">{api.words}</p>
        <div className="mt-auto flex items-end gap-4 pt-4">
          <div>
            <p className="tabular text-heading">{compact(api.calls_7d)}</p>
            <p className="text-caption text-faint">calls this week{api.cached_pct === null ? '' : `, ${api.cached_pct}% saved`}</p>
          </div>
          <Traffic spark={api.spark} className="ml-auto h-10 w-40" />
        </div>
      </div>
      <div className="flex min-w-0 flex-col rounded-lg border border-line bg-inset p-3">
        <Sample sample={api.sample} hued />
        <p className="mt-auto flex items-center justify-between gap-2 pt-3 text-caption text-faint">
          <span>Read {timeAgo(api.read_at).toLowerCase()}</span>
          <StateBadge state="good" label={HEALTH_WORD.good} size={14} className="text-caption" />
        </p>
      </div>
    </Link>
  );
}

/** What else a person checks by hand. The last tile of the grid, so it also closes the page's rhythm. */
function TeachTile({ org }: { org: string }) {
  return (
    <Link
      to="/$org/site-apis/new"
      params={{ org }}
      className="group flex min-h-48 flex-col justify-between rounded-(--radius-card) border border-dashed border-line-strong p-4 transition-colors duration-(--duration-fast) ease-standard hover:bg-hover active:bg-pressed"
    >
      <span>
        <span className="block text-section-title">What else do you check by hand?</span>
        <span className="mt-1 block text-body text-pretty text-muted">If you open a site just to look something up, your computer can read it for you.</span>
        <span className="mt-4 grid gap-2 text-secondary text-muted">
          {['A supplier\'s prices, every Monday', 'A delivery date, before the customer asks', 'A booking, before the guest arrives'].map((idea) => (
            <span key={idea} className="flex items-center gap-2 border-t border-line pt-2">
              <Plus aria-hidden="true" className="size-3 shrink-0 text-faint" />
              {idea}
            </span>
          ))}
        </span>
      </span>
      <span className="mt-4 flex items-center gap-1 text-secondary font-medium">
        Teach it a page
        <ArrowRight aria-hidden="true" className="size-4 transition-transform duration-(--duration-fast) ease-standard group-hover:translate-x-1" />
      </span>
    </Link>
  );
}

function Loading() {
  return (
    <div className="grid gap-8">
      <Skeleton className="h-24 max-w-2xl rounded-lg" />
      <Skeleton className="h-26 rounded-lg" />
      <div className="grid gap-4 md:grid-cols-2">
        {[0, 1, 2, 3].map((item) => (
          <Skeleton key={item} className="h-64 rounded-(--radius-card)" />
        ))}
      </div>
    </div>
  );
}

export function SiteApis() {
  const { org } = useParams({ from: '/$org/site-apis' });
  const apis = useSiteApis(org);
  const workspaces = useWorkspaces(org).data;
  const [hidden, setHidden] = useState(() => localStorage.getItem(DISMISSED) === '1');
  const business = (id: string) => workspaces?.find((item) => item.id === id);

  if (isNotServed(apis.error)) return <Card><NotLive /></Card>;
  if (apis.error) return <ErrorState error={apis.error} onRetry={() => void apis.refetch()} />;
  if (!apis.data) return <Loading />;

  const all = apis.data;
  const active = all.filter((api) => api.stage !== 'proposed');
  const proposals = all.filter((api) => api.stage === 'proposed');
  const needs = active.filter((api) => ['changed', 'sign_in', 'site_check', 'empty'].includes(api.health));
  const quiet = active.filter((api) => api.health === 'resting' || api.health === 'paused');
  const good = active.filter((api) => api.health === 'good');
  const teach = (
    <Button icon={<Plus aria-hidden="true" className="size-4" />} asChild>
      <Link to="/$org/site-apis/new" params={{ org }}>
        Teach it a page
      </Link>
    </Button>
  );

  if (all.length === 0) {
    return (
      <div className="grid gap-8">
        <h1 className="text-page-title-compact">Site APIs</h1>
        <Card>
          <EmptyState
            icon={<Plus />}
            title="Turn a website you use into a live table your tools can read"
            description="Show your computer a page once, and it keeps the table up to date from your own account."
            action={
              <Button variant="primary" asChild>
                <Link to="/$org/site-apis/new" params={{ org }}>
                  Teach it a page
                </Link>
              </Button>
            }
          />
        </Card>
      </div>
    );
  }

  return (
    <div className="pb-16">
      <div className="flex items-start justify-between gap-6">
        <Hero apis={all} />
        <div className="shrink-0">{teach}</div>
      </div>

      {hidden ? null : (
        <Intro
          org={org}
          onClose={() => {
            localStorage.setItem(DISMISSED, '1');
            setHidden(true);
          }}
        />
      )}

      <Week apis={active} />

      {proposals.length ? (
        <section aria-labelledby="site-saved" className="mt-8">
          <h2 id="site-saved" className="mb-3 text-section-title">
            Saved, not on yet
          </h2>
          <div className="grid gap-4">
            {proposals.map((api) =>
              api.proposal ? (
                <Proposal key={api.key} org={org} api={api} workspace={business(api.workspace)} />
              ) : (
                <Saved key={api.key} org={org} api={api} workspace={business(api.workspace)} />
              ),
            )}
          </div>
        </section>
      ) : null}

      {needs.length ? (
        <section aria-labelledby="site-needs" className="mt-8">
          <h2 id="site-needs" className="mb-3 text-section-title">
            Needs you
          </h2>
          <ul className="divide-y divide-line overflow-hidden rounded-(--radius-card) border border-line bg-card">
            {needs.map((api) => (
              <ProblemRow key={api.key} org={org} api={api} />
            ))}
          </ul>
        </section>
      ) : null}

      <section aria-labelledby="site-good" className="mt-8">
        <h2 id="site-good" className="mb-3 text-section-title">
          Answering
        </h2>
        <div className="grid gap-4 md:grid-cols-2">
          {good[0] ? <LeadCard org={org} api={good[0]} workspace={business(good[0].workspace)} /> : null}
          {good.slice(1).map((api) => (
            <ApiCard key={api.key} org={org} api={api} workspace={business(api.workspace)} />
          ))}
          {good.length % 2 === 0 ? <TeachTile org={org} /> : null}
        </div>
      </section>

      {quiet.length ? (
        <section aria-labelledby="site-quiet" className="mt-8">
          <h2 id="site-quiet" className="mb-3 text-section-title">
            Resting and paused
          </h2>
          <ul className="divide-y divide-line overflow-hidden rounded-(--radius-card) border border-line bg-card">
            {quiet.map((api) => (
              <ProblemRow key={api.key} org={org} api={api} />
            ))}
          </ul>
        </section>
      ) : null}
    </div>
  );
}
