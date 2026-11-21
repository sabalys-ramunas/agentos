/**
 * One check, as a design system team opens it: the screens as a strip, the chosen screen with its
 * findings outlined and numbered, and the findings beside it grouped by component. Picking a finding
 * moves the picture to it; J and K step through findings, 1 to 9 through screens; the finding and the
 * screen live in the address. Under it: coverage, the design system, and what this change did.
 */
import { useNavigate, useParams, useSearch } from '@tanstack/react-router';
import { ArrowLeft, Check, Copy } from 'lucide-react';
import { useMemo } from 'react';
import { isUnavailable, useStudioRun } from '@/api/hooks';
import { ApiError, isNotServed } from '@/api/http';
import type { StudioRun } from '@/api/types';
import { usePageCrumb } from '@/components/shell/crumb';
import { Button } from '@/components/ui/button';
import { Card, CardHeader } from '@/components/ui/card';
import { KindEyebrow, KindTile } from '@/components/ui/kind';
import { PageHeader, useCopy } from '@/components/ui/misc';
import { SegmentedControl } from '@/components/ui/tabs';
import { EmptyState, ErrorState, Skeleton } from '@/components/ui/states';
import { Status } from '@/components/ui/status';
import { useKeys } from '@/lib/keyboard';
import { plural, timeAgo } from '@/lib/format';
import { Bench, BenchFoot, ScreenStrip } from './bench';
import { Coverage, DesignSystem, WhatChanged } from './check-lower';
import { FindingsList, OpenFinding } from './findings-list';
import { atLeast, checkAgain, countsByScreen, numbered, onScreen, pagePath, runStatus, verdictWords, worstByScreen } from './model';
import type { NumberedFinding } from './model';
import { NotYours, StudioMissingState } from './parts';
import type { CheckSearch } from './search';

function CopyCheck({ run }: { run: StudioRun }) {
  const { copied, copy } = useCopy();
  return (
    <Button icon={copied ? <Check className="size-4 text-ok" /> : <Copy className="size-4" />} onClick={() => copy(checkAgain(run))}>
      {copied ? 'Copied' : 'Copy a new check'}
    </Button>
  );
}

function Loading() {
  return (
    <div role="status" aria-label="Loading the check" className="grid gap-6">
      <div className="grid grid-cols-[auto_minmax(0,1fr)] items-start gap-x-4">
        <Skeleton className="size-10 rounded-lg" />
        <div className="grid gap-3">
          <Skeleton className="h-4 w-24" />
          <Skeleton className="h-8 w-80 max-w-full" />
          <Skeleton className="h-5 w-96 max-w-full" />
        </div>
      </div>
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {Array.from({ length: 4 }, (_, index) => (
          <Skeleton key={index} className="h-32 rounded-xl" />
        ))}
      </div>
      <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,5fr)_minmax(0,6fr)]">
        <Skeleton className="h-96 rounded-xl" />
        <div className="grid gap-6">
          <Skeleton className="h-56 rounded-xl" />
          <Skeleton className="h-72 rounded-xl" />
        </div>
      </div>
    </div>
  );
}

function Refused({ error, onRetry }: { error: unknown; onRetry: () => void }) {
  if (isNotServed(error)) return <ErrorState error={error} />;
  if (error instanceof ApiError && (error.status === 403 || error.code === 'denied')) return <NotYours />;
  if (isUnavailable(error)) {
    return <StudioMissingState missing={{ available: false, reason: error instanceof Error ? error.message : '', command: 'uv sync --extra studio' }} />;
  }
  if (error instanceof ApiError && (error.status === 404 || error.code === 'not_found')) {
    return (
      <EmptyState
        title="This check is no longer kept"
        description="Studio keeps the newest checks and lets old ones go. Ask your agent to check the page again."
      />
    );
  }
  return <ErrorState error={error} onRetry={onRetry} />;
}

export function CheckPage() {
  const { org, id } = useParams({ from: '/$org/checks/$id' });
  const search = useSearch({ from: '/$org/checks/$id' });
  const navigate = useNavigate({ from: '/$org/checks/$id' });
  const query = useStudioRun(org, id);
  const run = query.data;
  usePageCrumb(run ? `${pagePath(run.url).host}${pagePath(run.url).path}` : undefined);

  const all = useMemo(() => (run ? numbered(run.groups) : []), [run]);
  const findings = useMemo(() => atLeast(all, search.severity), [all, search.severity]);
  const counts = useMemo(() => countsByScreen(findings), [findings]);
  const worst = useMemo(() => worstByScreen(findings), [findings]);
  const set = (next: Partial<CheckSearch>) => void navigate({ search: (prev) => ({ ...prev, ...next }), replace: true });

  // The picked finding, and the screen it shows on. With none in the address: the first finding, on its first placed screen.
  const picked: NumberedFinding | undefined = findings.find((finding) => finding.n === search.finding) ?? findings[0];
  const screens = run?.screen_list ?? [];
  const firstPlaced = picked ? Object.keys(picked.rects ?? {}).find((key) => screens.some((screen) => screen.key === key && screen.ok)) : undefined;
  const screenKey =
    (search.screen && screens.some((screen) => screen.key === search.screen) ? search.screen : undefined) ??
    firstPlaced ??
    screens.find((screen) => screen.ok)?.key ??
    screens[0]?.key ??
    '';
  const screen = screens.find((item) => item.key === screenKey);

  const pick = (finding: NumberedFinding) => {
    const placed = Object.keys(finding.rects ?? {});
    const keep = finding.rects?.[screenKey] || (!placed.length && onScreen(finding, screenKey));
    const next = keep ? screenKey : (placed[0] ?? [...finding.seen_at.map((seen) => seen.screen)].find((key) => screens.some((s) => s.key === key)) ?? screenKey);
    set({ finding: finding.n, screen: next });
  };
  const step = (by: number) => {
    if (!findings.length) return;
    const index = picked ? findings.indexOf(picked) : -1;
    const next = findings[(index + by + findings.length) % findings.length];
    if (next) pick(next);
  };
  const keys: Record<string, () => void> = { j: () => step(1), k: () => step(-1), J: () => step(1), K: () => step(-1) };
  screens.slice(0, 9).forEach((item, index) => {
    keys[String(index + 1)] = () => set({ screen: item.key });
  });
  useKeys(keys, !!run);

  if (query.isPending) return <Loading />;
  if (query.isError || !run) {
    return (
      <>
        <PageHeader title="A check" eyebrow={<KindEyebrow kind="design" />} />
        <Card>
          <Refused error={query.error} onRetry={() => void query.refetch()} />
        </Card>
      </>
    );
  }

  const { path, host } = pagePath(run.url);
  const title = path === '/' && host ? `${host}/` : path;
  const fontsNote = run.problems.some((problem) => problem.toLowerCase().includes('fonts'));
  const kindWord = run.kind === 'system' ? 'Design system check' : 'Design check';

  return (
    <>
      <PageHeader
        lead={<KindTile kind="design" size="xl" />}
        eyebrow={<KindEyebrow kind="design" label={kindWord} tile={false} />}
        title={title}
        titleHint={run.url}
        aside={<Status chip status={runStatus(run)} />}
        description={
          <span>
            {run.verdict ? `${verdictWords(run.verdict)}. ` : ''}
            Checked {timeAgo(run.created_at).toLowerCase()} on {host || 'this computer'}
            {run.profile ? (
              <>
                , against <span className="font-mono text-code">{run.profile}</span>
              </>
            ) : null}
            .
          </span>
        }
        actions={
          <>
            <Button variant="ghost" icon={<ArrowLeft className="size-4" />} onClick={() => void navigate({ to: '/$org/checks', params: { org }, search: { page: run.url } })}>
              Every check of this page
            </Button>
            <CopyCheck run={run} />
          </>
        }
      />
      {run.images_gone ? null : (
        <div className="mb-6">
          <h2 className="sr-only">Screens</h2>
          <ScreenStrip org={org} run={run} current={screenKey} counts={counts} worst={worst} onPick={(key) => set({ screen: key })} />
        </div>
      )}
      <div className={run.images_gone ? 'grid gap-6' : 'grid items-start gap-6 lg:grid-cols-[minmax(0,5fr)_minmax(0,6fr)]'}>
        {run.images_gone ? null : (
          <Card className="min-w-0 overflow-hidden lg:sticky lg:top-20">
            <Bench org={org} run={run} screen={screen} findings={findings.filter((finding) => onScreen(finding, screenKey))} active={picked} onSelect={pick} />
            <BenchFoot fontsNote={fontsNote} />
          </Card>
        )}
        <div className="grid min-w-0 gap-6">
          {run.images_gone ? (
            <p className="text-body text-muted">
              The pictures of this check are no longer kept. Every finding is still here, with what was measured, what was expected and the fix.
            </p>
          ) : null}
          {picked ? <OpenFinding run={run} finding={picked} screen={screenKey} onScreenPick={(key) => set({ screen: key })} /> : null}
          <Card className="min-w-0 overflow-hidden">
            <CardHeader
              title="Findings"
              meta={
                all.length
                  ? `${screen ? `${counts.get(screenKey) ?? 0} on this screen, ` : ''}${plural(findings.length, 'in all', 'in all')}, worst first`
                  : undefined
              }
              action={
                all.length ? (
                  <SegmentedControl
                    label="Severity"
                    value={search.severity === 'major' ? 'major' : search.severity === 'blocker' ? 'blocker' : 'all'}
                    onValueChange={(value) => set({ severity: value === 'all' ? undefined : value, finding: undefined })}
                    options={[
                      { value: 'all', label: 'All' },
                      { value: 'major', label: 'Major up' },
                      { value: 'blocker', label: 'Blockers' },
                    ]}
                  />
                ) : undefined
              }
            />
            {findings.length ? (
              <FindingsList findings={findings} screen={screenKey} picked={picked} onPick={pick} />
            ) : (
              <EmptyState
                title={all.length ? 'Nothing at this severity' : 'No findings'}
                description={all.length ? 'Every finding is less severe than this filter.' : 'Studio measured every screen that rendered and found nothing to fix.'}
                dawn={all.length ? undefined : true}
              />
            )}
          </Card>
        </div>
      </div>
      <div className="mt-6 grid items-start gap-6 lg:grid-cols-2">
        <Coverage run={run} />
        <DesignSystem run={run} findings={all} onPick={pick} />
      </div>
      {run.kind === 'check' ? (
        <div className="mt-6">
          <WhatChanged org={org} run={run} screen={screenKey} since={search.since} onSince={(since) => set({ since })} onScreen={(key) => set({ screen: key })} />
        </div>
      ) : null}
    </>
  );
}
