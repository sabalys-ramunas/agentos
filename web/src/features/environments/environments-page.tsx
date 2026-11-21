/**
 * Environments: the computers agents use (docs/design/environments.md, web/DESIGN.md "Environments"). Three
 * views of one list, chosen at the top with their counts:
 *
 * - Open (the default): each open one as a card with a picture of its screen, its state and time left on the
 *   picture; then, for the owner, one line for the prepared machines and the warm pool; then the saves (an
 *   environment saves when idle by default), which the owner resumes or an agent brings back by opening them
 *   again; then the newest closed ones as a table, the rest a click away.
 * - Saved: every save.
 * - Closed: every closed one.
 *
 * Everything is what the node serves; the page reads it again on its own (no events carry environments), and
 * asks the node for a recent still of each screen while it is shown (`pictures`).
 */
import { Link, useNavigate, useParams, useSearch } from '@tanstack/react-router';
import { ChevronRight, Lock } from 'lucide-react';
import { useEnvironments } from '@/api/hooks';
import { ApiError, isNotServed } from '@/api/http';
import type { EnvList, EnvSummary } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { CodeBlock, PageHeader } from '@/components/ui/misc';
import { EmptyState, ErrorState, NotLive, Skeleton } from '@/components/ui/states';
import { SegmentedControl } from '@/components/ui/tabs';
import { plural } from '@/lib/format';
import { useTechnical } from '@/lib/technical';
import { useNow } from '@/lib/use-now';
import { PLACES } from '@/lib/vocabulary';
import { ClosedTable } from './closed-table';
import { EnvCard, EnvCardsSkeleton } from './env-card';
import { ReadyAhead } from './ready-ahead';
import { SavedCards, savedLine } from './saved';
import type { EnvView } from './search';
import { openSummary, splitEnvironments } from './words';

/** Closed ones shown on the Open view before "Show all". */
export const CLOSED_SHOWN = 5;

/** How an agent asks for one, as it would call it. */
export const OPEN_CALL = 'environment_open(title="Fix the login page", repo="github.com/you/app")';

export const LEDE = 'Computers your agents use. Each one has a screen you can watch, a terminal and your code.';

export function EnvironmentsPage() {
  const { org } = useParams({ from: '/$org/environments' });
  const { show } = useSearch({ from: '/$org/environments' });
  const query = useEnvironments(org, 'all', true, { pictures: true });
  return (
    <div className="grid gap-6">
      <PageHeader title={PLACES.environments.word} description={LEDE} />
      <EnvironmentsBody org={org} view={show ?? 'open'} query={query} />
    </div>
  );
}

function Refused({ children }: { children: React.ReactNode }) {
  return <Card>{children}</Card>;
}

function EnvironmentsBody({ org, view, query }: { org: string; view: EnvView; query: ReturnType<typeof useEnvironments> }) {
  if (query.isPending) return <Loading view={view} />;
  if (isNotServed(query.error)) {
    return (
      <Refused>
        <NotLive />
      </Refused>
    );
  }
  if (query.error instanceof ApiError && query.error.status === 403) {
    return (
      <Refused>
        <EmptyState
          icon={<Lock />}
          title="Environments are not shown to your role"
          description="Seeing them needs the env workspace in your role. An owner or admin can add it."
        />
      </Refused>
    );
  }
  if (query.isError || !query.data) {
    return (
      <Refused>
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      </Refused>
    );
  }
  return <Loaded org={org} view={view} data={query.data} />;
}

/** The bar over the views: the three views with their counts, and what the open ones do and cost. */
function ViewBar({ org, view, counts, summary }: { org: string; view: EnvView; counts?: Record<EnvView, number>; summary?: string }) {
  const navigate = useNavigate();
  const label = (word: string, value: EnvView) => (
    <>
      {word}
      {counts ? <span className="font-mono text-code-caption text-faint tabular">{counts[value]}</span> : null}
    </>
  );
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-3">
      <SegmentedControl<EnvView>
        label="Which environments"
        size="md"
        value={view}
        onValueChange={(next) => void navigate({ to: '/$org/environments', params: { org }, search: next === 'open' ? {} : { show: next } })}
        options={[
          { value: 'open', label: label('Open', 'open') },
          { value: 'saved', label: label('Saved', 'saved') },
          { value: 'closed', label: label('Closed', 'closed') },
        ]}
      />
      {summary ? <p className="text-secondary text-muted sm:ml-auto">{summary}</p> : null}
    </div>
  );
}

function Loading({ view }: { view: EnvView }) {
  return (
    <div role="status" aria-label="Loading environments" className="grid gap-4">
      <div className="flex items-center gap-4">
        <Skeleton className="h-9 w-72 max-w-full rounded-full" />
        <Skeleton className="ml-auto w-56 max-sm:hidden" />
      </div>
      {view === 'open' ? <EnvCardsSkeleton /> : <Skeleton className="h-64 w-full rounded-xl" />}
    </div>
  );
}

function Loaded({ org, view, data }: { org: string; view: EnvView; data: EnvList }) {
  const { open, closed } = splitEnvironments(data.environments);
  const saved = data.saved ?? [];
  const now = useNow(open.length > 0, 30_000);
  if (!open.length && !closed.length && !saved.length) return <NoneYet org={org} />;
  const counts = { open: open.length, saved: saved.length, closed: closed.length };
  return (
    <>
      <ViewBar org={org} view={view} counts={counts} summary={open.length ? openSummary(open) : undefined} />
      {view === 'open' ? (
        <>
          <OpenCards org={org} open={open} now={now} saved={saved.length} closed={closed.length} />
          <ReadyAhead org={org} />
          {saved.length ? (
            <SectionHead title="Saved" meta={savedLine(saved)} className="mt-4">
              <SavedCards org={org} saved={saved} now={now} />
            </SectionHead>
          ) : null}
          {closed.length ? (
            <SectionHead
              title="Closed"
              meta={plural(closed.length, 'environment')}
              action={closed.length > CLOSED_SHOWN ? <ShowAll org={org} count={closed.length} /> : undefined}
              className="mt-4"
            >
              <ClosedTable org={org} rows={closed.slice(0, CLOSED_SHOWN)} now={now} />
            </SectionHead>
          ) : null}
        </>
      ) : view === 'saved' ? (
        saved.length ? (
          <SectionHead title="Saved" meta={savedLine(saved)}>
            <SavedCards org={org} saved={saved} now={now} />
          </SectionHead>
        ) : (
          <PictureEmpty grey title="Nothing is saved" line="An environment is saved when it is idle or its agent is done for now, and shows here until you resume it or an agent opens it again." />
        )
      ) : closed.length ? (
        <ClosedTable org={org} rows={closed} now={now} />
      ) : (
        <PictureEmpty grey title="Nothing has closed yet" line="When an agent is done with its computer, it shows here with what it cost." />
      )}
    </>
  );
}

function ShowAll({ org, count }: { org: string; count: number }) {
  return (
    <Link
      to="/$org/environments"
      params={{ org }}
      search={{ show: 'closed' }}
      className="inline-flex min-h-8 items-center gap-1 rounded-full text-secondary text-muted transition-colors duration-(--duration-fast) hover:text-fg"
    >
      Show all {count}
      <ChevronRight aria-hidden="true" className="size-4" />
    </Link>
  );
}

/** A section of the list: its title with quiet words on the same line, an action on the right. */
function SectionHead({ title, meta, action, className, children }: { title: string; meta?: React.ReactNode; action?: React.ReactNode; className?: string; children: React.ReactNode }) {
  const id = `env-${title.toLowerCase()}`;
  return (
    <section aria-labelledby={id} className={className}>
      <div className="mb-4 flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 id={id} className="text-section-title">
          {title}
        </h2>
        {/* On a phone the action stays beside the title and the quiet words go under them. */}
        {meta ? <p className="order-3 w-full min-w-0 text-secondary text-muted sm:order-2 sm:w-auto sm:flex-1">{meta}</p> : null}
        {action ? <div className="order-2 ml-auto sm:order-3">{action}</div> : null}
      </div>
      {children}
    </section>
  );
}

function OpenCards({ org, open, now, saved, closed }: { org: string; open: EnvSummary[]; now: number; saved: number; closed: number }) {
  if (!open.length) {
    const later = saved ? ` ${saved === 1 ? '1 is' : `${saved} are`} saved for later.` : '';
    const next = saved ? { show: 'saved' as const, word: 'See saved ones' } : closed ? { show: 'closed' as const, word: 'See closed ones' } : null;
    return (
      <PictureEmpty
        grey
        title="No environments are open"
        line={`They open when an agent starts a chat that needs a computer.${later}`}
        action={
          next ? (
            <Button asChild>
              <Link to="/$org/environments" params={{ org }} search={{ show: next.show }}>
                {next.word}
              </Link>
            </Button>
          ) : undefined
        }
      />
    );
  }
  return (
    <ul aria-label="Open environments" className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
      {open.map((env) => (
        <EnvCard key={env.id} org={org} env={env} now={now} />
      ))}
    </ul>
  );
}

/**
 * An empty part of the list, as the states sheet draws it: the shared empty state with the Dawn picture (greyed for
 * "nothing right now"), a title, one sentence and one action, centred in a card.
 */
function PictureEmpty({ title, line, action, grey = false, children }: { title: string; line: string; action?: React.ReactNode; grey?: boolean; children?: React.ReactNode }) {
  return (
    <Card>
      <EmptyState dawn={grey ? 'grey' : true} heading="h2" title={title} description={line} action={action} className="py-8">
        {children}
      </EmptyState>
    </Card>
  );
}

/** No environment ever: what one is, and the one next step (connect an AI). The call itself is for Technical details. */
function NoneYet({ org }: { org: string }) {
  const [technical] = useTechnical();
  return (
    <PictureEmpty
      title="Give your first agent a computer"
      line="Connect Claude Code or Codex, and each chat gets a computer of its own: a terminal, a browser and your code."
      action={
        <Button asChild variant="primary">
          <Link to="/onboarding/$org/$step" params={{ org, step: 'ai' }}>
            Connect your AI
          </Link>
        </Button>
      }
    >
      {technical ? <CodeBlock code={OPEN_CALL} label="Copy the call" className="mt-4 max-w-full text-left" /> : null}
    </PictureEmpty>
  );
}
