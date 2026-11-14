/**
 * Checks: every studio run this computer keeps, newest first, as the Mono table. A row is one check of
 * one page: the page, the product it was checked against, the verdict as a status, findings by
 * severity, the design system share, the screens covered and failed, and when. Filter by page,
 * product or result; a summary sentence over the newest check of each page. Without studio, a calm
 * state with the command that adds it.
 */
import { useNavigate, useParams, useSearch } from '@tanstack/react-router';
import { Check, RotateCw, ScanSearch } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { useMemo } from 'react';
import { isStudioMissing, useStudioRuns } from '@/api/hooks';
import type { StudioRunRow } from '@/api/types';
import { Chip } from '@/components/ui/badge';
import { Card } from '@/components/ui/card';
import { CopyButton, PageHeader, useCopy } from '@/components/ui/misc';
import { Select } from '@/components/ui/select';
import { Status } from '@/components/ui/status';
import { CellTitle, NoValue, RowActions, Table, TableEmpty, TableScroll, TableSkeleton, Td, Th, Tr } from '@/components/ui/table';
import { EmptyState, ErrorState } from '@/components/ui/states';
import { timeAgo, plural } from '@/lib/format';
import { PLACES } from '@/lib/vocabulary';
import { checkAgain, pagePath, percent, runStatus } from './model';
import { ShareBar, StudioMissingState } from './parts';
import type { ChecksSearch } from './search';

const COLUMNS = ['Page', 'Result', 'Blocker', 'Major', 'Minor', 'On the system', 'Screens', 'Checked', ''];

type RowState = NonNullable<ChecksSearch['state']>;

function stateOf(run: StudioRunRow): RowState {
  if (run.screens > 0 && run.screens_failed > 0) return 'failed';
  if (run.counts.blocker > 0) return 'blocked';
  if (run.counts.total - run.counts.info > 0) return 'findings';
  return 'clean';
}

const STATE_WORDS: Record<RowState, string> = { clean: 'Clean', findings: 'With findings', blocked: 'With a blocker', failed: 'A screen failed' };

/** The newest check of each page: what is true of the page now. */
function newestPerPage(runs: StudioRunRow[]): StudioRunRow[] {
  const seen = new Set<string>();
  return runs.filter((run) => run.kind === 'check' && !seen.has(run.url) && (seen.add(run.url), true));
}

/** One sentence over the newest check of each page. */
export function summary(runs: StudioRunRow[]): string {
  if (runs.length === 0) return 'No page has been checked here yet.';
  const latest = newestPerPage(runs);
  const pages = new Set(runs.map((run) => run.url)).size;
  const head = `${plural(runs.length, 'check')} of ${plural(pages, 'page')}.`;
  if (latest.length === 0) return `${head} Only design system checks so far.`;
  const blockers = latest.reduce((sum, run) => sum + run.counts.blocker, 0);
  const open = latest.reduce((sum, run) => sum + run.counts.total - run.counts.info, 0);
  const pagesWith = latest.filter((run) => run.counts.total - run.counts.info > 0).length;
  const shares = latest.map((run) => run.system_share).filter((share): share is number => share !== null);
  const findings =
    open === 0
      ? 'The newest check of every page is clean'
      : `The newest checks have ${plural(open, 'open finding')} on ${plural(pagesWith, 'page')}${blockers ? `, ${plural(blockers, 'blocker')} among them` : ''}`;
  const share = shares.length ? `, and ${percent(shares.reduce((a, b) => a + b, 0) / shares.length)} of values are on the design system` : '';
  return `${head} ${findings}${share}.`;
}

function CopyCheckCommand() {
  const { copied, copy } = useCopy();
  return (
    <Button icon={copied ? <Check className="size-4 text-ok" /> : <ScanSearch className="size-4" />} onClick={() => copy('ui_check url=http://localhost:3000/')}>
      {copied ? 'Copied for your agent' : 'Check a page'}
    </Button>
  );
}

function Severity({ value, tone }: { value: number; tone?: 'bad' }) {
  if (value === 0) return <span className="text-faint">0</span>;
  return <span className={tone === 'bad' ? 'text-bad' : undefined}>{value}</span>;
}

function Row({ run, onOpen }: { run: StudioRunRow; onOpen: () => void }) {
  const { path, host } = pagePath(run.url);
  const second = [host, run.profile, run.kind === 'system' ? 'design system only' : null].filter(Boolean).join(' · ');
  return (
    <Tr onActivate={onOpen} aria-label={`Open the check of ${path}`}>
      <Td main>
        <CellTitle title={path} second={second} mono />
      </Td>
      <Td>
        <Status status={runStatus(run)} />
      </Td>
      <Td numeric phone="hide" label="Blocker">
        <Severity value={run.counts.blocker} tone="bad" />
      </Td>
      <Td numeric phone="hide" label="Major">
        <Severity value={run.counts.major} />
      </Td>
      <Td numeric phone="hide" label="Minor">
        <Severity value={run.counts.minor} />
      </Td>
      <Td phone="hide" label="On the system">
        {run.system_share === null ? <NoValue /> : <ShareBar share={run.system_share} label={`Values on a design token in the check of ${path}`} />}
      </Td>
      <Td numeric phone="hide" label="Screens">
        {run.screens - run.screens_failed} of {run.screens}
        {run.screens_failed ? <span className="block text-secondary text-bad">{run.screens_failed} failed</span> : null}
      </Td>
      <Td numeric label="Checked" title={new Date(run.created_at).toLocaleString()}>
        {timeAgo(run.created_at)}
      </Td>
      <Td phone="hide">
        <RowActions>
          <CopyButton text={checkAgain(run)} label={`Copy a new check of ${path} for your agent`} />
        </RowActions>
      </Td>
    </Tr>
  );
}

export function ChecksPage() {
  const { org } = useParams({ from: '/$org' });
  const search = useSearch({ from: '/$org/checks' });
  const navigate = useNavigate({ from: '/$org/checks' });
  const query = useStudioRuns(org, { limit: 200 });
  const set = (next: Partial<ChecksSearch>) => void navigate({ search: (prev) => ({ ...prev, ...next }), replace: true });

  const data = query.data;
  const runs = useMemo(() => (data && !isStudioMissing(data) ? data.runs : []), [data]);
  const pages = useMemo(() => {
    const counts = new Map<string, number>();
    for (const run of runs) counts.set(run.url, (counts.get(run.url) ?? 0) + 1);
    return [...counts.entries()];
  }, [runs]);
  const profiles = useMemo(() => [...new Set(runs.map((run) => run.profile).filter((p): p is string => !!p))].sort(), [runs]);
  const shown = runs.filter(
    (run) =>
      (!search.page || run.url === search.page) &&
      (!search.profile || run.profile === search.profile) &&
      (!search.state || stateOf(run) === search.state),
  );
  const filtered = !!(search.page || search.profile || search.state);

  const header = (
    <PageHeader
      title={PLACES.checks.word}
      description={
        data && !isStudioMissing(data)
          ? summary(runs)
          : 'Pages your agents checked in a real browser, at phone, tablet and desktop widths, in light and dark.'
      }
      actions={<CopyCheckCommand />}
    />
  );

  if (query.isError) {
    return (
      <>
        {header}
        <Card>
          <ErrorState error={query.error} onRetry={() => void query.refetch()} />
        </Card>
      </>
    );
  }
  if (data && isStudioMissing(data)) {
    return (
      <>
        {header}
        <Card>
          <StudioMissingState missing={data} />
        </Card>
      </>
    );
  }

  return (
    <>
      {header}
      {runs.length > 0 ? (
        <div className="mb-4 flex flex-wrap items-center gap-2" role="group" aria-label="Filter checks">
          <div className="scrollbar-none -mx-4 flex w-[calc(100%+32px)] min-w-0 gap-2 overflow-x-auto px-4 sm:mx-0 sm:w-auto sm:flex-1 sm:px-0" role="group" aria-label="Page">
            <Chip pressed={!search.page} label="Every page" count={runs.length} onClick={() => set({ page: undefined })} />
            {pages.map(([url, n]) => (
              <Chip
                key={url}
                pressed={search.page === url}
                label={
                  <span className="max-w-60 truncate font-mono text-code-caption" title={url}>
                    {pagePath(url).path}
                  </span>
                }
                title={url}
                count={n}
                onClick={() => set({ page: search.page === url ? undefined : url })}
              />
            ))}
          </div>
          {profiles.length > 1 ? (
            <Select
              aria-label="Product"
              size="sm"
              className="w-auto min-w-36"
              value={search.profile ?? 'all'}
              onValueChange={(value) => set({ profile: value === 'all' ? undefined : value })}
              options={[{ value: 'all', label: 'Every product' }, ...profiles.map((p) => ({ value: p, label: p }))]}
            />
          ) : null}
          <Select
            aria-label="Result"
            size="sm"
            className="w-auto min-w-36"
            value={search.state ?? 'all'}
            onValueChange={(value) => set({ state: value === 'all' ? undefined : (value as RowState) })}
            options={[{ value: 'all', label: 'Every result' }, ...(Object.keys(STATE_WORDS) as RowState[]).map((s) => ({ value: s, label: STATE_WORDS[s] }))]}
          />
        </div>
      ) : null}
      <Card className="overflow-hidden">
        {query.isPending ? (
          <div role="status" aria-label="Loading checks">
            <TableSkeleton columns={COLUMNS} />
          </div>
        ) : (
          <TableScroll>
            <Table stack aria-label="Checks, newest first">
              <thead>
                <tr>
                  <Th>Page</Th>
                  <Th>Result</Th>
                  <Th align="end" className="max-md:hidden">
                    Blocker
                  </Th>
                  <Th align="end" className="max-md:hidden">
                    Major
                  </Th>
                  <Th align="end" className="max-md:hidden">
                    Minor
                  </Th>
                  <Th className="max-md:hidden">On the system</Th>
                  <Th align="end" className="max-md:hidden">
                    Screens
                  </Th>
                  <Th align="end">Checked</Th>
                  <Th className="max-md:hidden">
                    <span className="sr-only">Actions</span>
                  </Th>
                </tr>
              </thead>
              <tbody>
                {shown.length === 0 ? (
                  <TableEmpty colSpan={COLUMNS.length}>
                    {filtered ? (
                      <EmptyState
                        icon={<ScanSearch />}
                        title="No check matches"
                        description="Nothing kept here fits these filters."
                        action={
                          <Chip pressed={false} label="Show every check" onClick={() => set({ page: undefined, profile: undefined, state: undefined })} />
                        }
                      />
                    ) : (
                      <EmptyState
                        icon={<ScanSearch />}
                        title="No pages checked yet"
                        description="Ask your agent to check a page with ui_check. Its findings show here on the real screenshots."
                        action={<CopyCheckCommand />}
                      />
                    )}
                  </TableEmpty>
                ) : (
                  shown.map((run) => (
                    <Row
                      key={run.id}
                      run={run}
                      onOpen={() => void navigate({ to: '/$org/checks/$id', params: { org, id: run.id }, search: {} })}
                    />
                  ))
                )}
              </tbody>
            </Table>
          </TableScroll>
        )}
      </Card>
      {runs.length > 0 ? (
        <p className="mt-3 flex items-center gap-2 text-caption text-faint">
          <RotateCw aria-hidden="true" className="size-3.5" />
          The copy button on a row gives your agent a new check of that page that reads only what changed.
        </p>
      ) : null}
    </>
  );
}
