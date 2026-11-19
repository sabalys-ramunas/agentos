/**
 * The lower half of an open check: coverage said plainly (what was not checked and why), the design
 * system (the share by category, values that only look like a token, hand-rolled controls), and what
 * this change did against the page's baseline or an earlier check (causes first, then the changes).
 */
import { CircleCheck, GitCompareArrows } from 'lucide-react';
import { studioShotUrl } from '@/api/client';
import { useStudioBaseline, useStudioDiff, useStudioRun, useStudioRuns, isStudioMissing } from '@/api/hooks';
import type { StudioDiffScreen, StudioRun } from '@/api/types';
import { Badge } from '@/components/ui/badge';
import { BeforeAfter } from '@/components/ui/before-after';
import { Card, CardFooter, CardHeader } from '@/components/ui/card';
import { Select } from '@/components/ui/select';
import { Skeleton, EmptyState, ErrorState } from '@/components/ui/states';
import { Table, TableScroll, Td, Th } from '@/components/ui/table';
import { cn } from '@/lib/cn';
import { plural, timeAgo } from '@/lib/format';
import { CATEGORY_WORDS, keyWords, lookAlikes, paintable, percent, screenOfKey, screenWords } from './model';
import type { NumberedFinding } from './model';
import { ShareBar, useImageSize } from './parts';

/** What was checked and what was not, in sentences. */
export function Coverage({ run }: { run: StudioRun }) {
  const ran = run.screen_list.filter((screen) => screen.ok);
  const failed = run.screen_list.filter((screen) => !screen.ok);
  const lines: string[] = [];
  for (const screen of failed) {
    lines.push(`${screenWords(screen)} did not render${screen.error ? `: ${screen.error}` : '.'}`);
  }
  const failedKeys = new Set(failed.map((screen) => screen.key));
  for (const problem of run.problems) {
    // The node also names failed screens by key; those are said above in words.
    if ([...failedKeys].some((key) => problem.startsWith(`${key} failed`))) continue;
    lines.push(problem.charAt(0).toUpperCase() + problem.slice(1));
  }
  if (run.system_share === null && run.kind === 'check') {
    lines.push(run.profile ? 'The design system was not measured in this check.' : 'Not checked against a design system: no product profile was named.');
  }
  if (run.images_gone) lines.push('The pictures are no longer kept, so the findings are shown without them.');
  const themes = new Set(ran.map((screen) => screen.theme));
  if (ran.length && themes.size === 1) lines.push(`Only the ${[...themes][0]} theme was checked: the page did not offer the other one to studio.`);
  return (
    <Card>
      <CardHeader title="Coverage" meta={`${ran.length} of ${plural(run.screen_list.length, 'screen')} checked`} />
      <div className="grid gap-3 p-5">
        <p className="flex flex-wrap gap-1">
          {run.screen_list.map((screen) => (
            <Badge key={screen.key} tone={screen.ok ? 'neutral' : 'bad'} className="font-mono">
              {screenWords(screen)}
              {screen.ok ? '' : ', failed'}
            </Badge>
          ))}
        </p>
        {lines.length ? (
          <>
            <h3 className="text-secondary text-faint">Not checked, and why</h3>
            <ul className="grid gap-2">
              {lines.map((line, index) => (
                <li key={index} className="flex gap-2 text-body text-fg">
                  <span aria-hidden="true" className="mt-2 size-1 shrink-0 rounded-full bg-faint" />
                  <span className="min-w-0 break-words">{line}</span>
                </li>
              ))}
            </ul>
          </>
        ) : (
          <p className="flex items-center gap-2 text-body text-muted">
            <CircleCheck aria-hidden="true" className="size-4 text-ok" />
            Every screen rendered and every check ran.
          </p>
        )}
      </div>
    </Card>
  );
}

/** The design system: share by category, values that only look like a token, hand-rolled controls. */
export function DesignSystem({ run, findings, onPick }: { run: StudioRun; findings: NumberedFinding[]; onPick: (finding: NumberedFinding) => void }) {
  const system = run.system;
  const near = lookAlikes(findings);
  const handRolled = findings.filter((finding) => finding.check === 'component.hand_rolled');
  if (!system && run.system_share === null) {
    return (
      <Card>
        <CardHeader title="On the design system" />
        <EmptyState
          title="Not measured in this check"
          description={
            run.profile
              ? 'This check did not measure the design system.'
              : 'Name a product profile with design tokens in the check, and studio measures how much of the page is on them.'
          }
        />
      </Card>
    );
  }
  const categories = Object.entries(system?.on_tokens ?? {});
  const hand = system?.hand_rolled_controls ?? handRolled.length;
  return (
    <Card>
      <CardHeader title="On the design system" meta={run.profile ? `against ${run.profile}` : undefined} />
      <div className="grid gap-5 p-5">
        <div className="flex flex-wrap items-end gap-x-8 gap-y-4">
          {run.system_share !== null ? (
            <div className="grid gap-1">
              <span className="text-caption text-faint">On a token</span>
              <span className="text-stat text-fg">{percent(run.system_share)}</span>
            </div>
          ) : null}
          {system?.probe ? (
            <p className="max-w-xs text-secondary text-muted">
              {system.probe.token} values come from a token, {system.probe.derived} are worked out from one, and{' '}
              <span className={system.probe.look_alike ? 'text-warn' : undefined}>{system.probe.look_alike} only look like one</span>.
            </p>
          ) : null}
        </div>
        {categories.length ? (
          <dl className="grid gap-2">
            {categories.map(([category, share]) => (
              <div key={category} className="grid grid-cols-[7rem_minmax(0,1fr)] items-center gap-3">
                <dt className="truncate text-body text-muted">{CATEGORY_WORDS[category] ?? category}</dt>
                <dd>
                  <ShareBar wide share={share} label={`${CATEGORY_WORDS[category] ?? category} on a token`} className="w-full" />
                </dd>
              </div>
            ))}
          </dl>
        ) : null}
        {near.length ? (
          <div className="grid gap-2">
            <h3 className="text-secondary text-faint">Values that only look like a token</h3>
            <ul className="grid">
              {near.map((item) => {
                const colour = item.category === 'color' ? paintable(item.value) : null;
                const finding = item.finding as NumberedFinding;
                return (
                  <li key={finding.id}>
                    <button
                      type="button"
                      onClick={() => onPick(finding)}
                      className="flex w-full items-center gap-3 rounded-md px-2 py-2 text-left hover:bg-hover"
                    >
                      {colour ? (
                        <span data-studio="ignore" role="img" aria-label={`The colour ${item.value}`} className="size-4 shrink-0 rounded-sm border border-line" style={{ background: colour }} />
                      ) : (
                        <span aria-hidden="true" className="size-4 shrink-0 rounded-sm border border-dashed border-line-strong" />
                      )}
                      <span className="min-w-0 flex-1 text-body">
                        <span className="font-mono text-code text-fg">{item.value}</span>{' '}
                        <span className="text-muted">
                          should be {item.token ? <span className="font-mono text-code">{item.token}</span> : 'a token'}
                          {item.tokenValue ? ` (${item.tokenValue})` : ''}
                        </span>
                      </span>
                      <span className="shrink-0 text-right text-caption text-faint tabular">
                        {[item.distance, item.count ? plural(item.count, 'place') : null].filter(Boolean).join(', ')}
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          </div>
        ) : null}
        <p className="text-body text-muted">
          {hand
            ? `${plural(hand, 'hand-rolled control')} ${hand === 1 ? 'looks' : 'look'} like a component but ${hand === 1 ? 'is' : 'are'} not one.`
            : 'No hand-rolled controls: every control comes from a component.'}
          {typeof system?.controls_in_components === 'number' ? ` ${percent(system.controls_in_components)} of controls are inside a component.` : ''}
        </p>
      </div>
    </Card>
  );
}

const CHANGE_WORDS: Record<StudioDiffScreen['changes'][number]['kind'], string> = {
  added: 'Added',
  removed: 'Removed',
  moved: 'Moved',
  resized: 'Resized',
  restyled: 'Restyled',
  text: 'Text',
};

function DiffPicture({ org, before, after, screen }: { org: string; before: string; after: string; screen: string }) {
  const afterSrc = studioShotUrl(org, after, screen, 1000);
  const beforeSrc = studioShotUrl(org, before, screen, 1000);
  const { size, failed } = useImageSize(afterSrc);
  if (failed) return <p className="text-body text-muted">The pictures of this screen are no longer kept; the causes below still stand.</p>;
  if (!size) return <Skeleton className="aspect-[16/10] h-auto w-full" />;
  return (
    <div className="mx-auto w-full max-w-3xl" style={{ maxWidth: Math.max((screenOfKey(screen)?.width ?? 600) * 1.25, 320) }}>
      <BeforeAfter
        width={size.width}
        height={size.height}
        before={{ src: beforeSrc, alt: `Before, at ${keyWords(screen)}`, label: 'Before' }}
        after={{ src: afterSrc, alt: `This check, at ${keyWords(screen)}`, label: 'This check' }}
      />
    </div>
  );
}

/** What this change did: the BeforeAfter of the same screen, and the cause sentences and changes, causes first. */
export function WhatChanged({
  org,
  run,
  screen,
  since,
  onSince,
  onScreen,
}: {
  org: string;
  run: StudioRun;
  screen: string;
  since: string | undefined;
  onSince: (id: string | undefined) => void;
  onScreen: (key: string) => void;
}) {
  const sinceRun = useStudioRun(org, since ?? '', !!since);
  const baseline = useStudioBaseline(org, run.url, run.profile ?? undefined, !since);
  const earlier = useStudioRuns(org, { url: run.url });
  const before = since ? sinceRun.data?.capture : baseline.data?.capture && baseline.data.capture !== run.capture ? baseline.data.capture : undefined;
  const diff = useStudioDiff(org, before ?? '', run.capture, !!before);
  const options =
    earlier.data && !isStudioMissing(earlier.data)
      ? earlier.data.runs.filter((item) => item.id !== run.id && item.capture !== run.capture)
      : [];

  const picker =
    options.length > 0 ? (
      <Select
        aria-label="Compare with"
        size="sm"
        className="w-auto max-w-64"
        value={since ?? 'baseline'}
        onValueChange={(value) => onSince(value === 'baseline' ? undefined : value)}
        options={[
          { value: 'baseline', label: 'The page’s baseline' },
          ...options.map((item) => ({ value: item.id, label: `The check of ${timeAgo(item.created_at).toLowerCase()}` })),
        ]}
      />
    ) : undefined;

  const head = <CardHeader title="What this change did" meta={before ? `against ${since ? 'an earlier check' : 'the baseline'}` : undefined} action={picker} />;
  const waiting = since ? sinceRun.isPending : baseline.isPending;
  if (waiting || (before && diff.isPending)) {
    return (
      <Card>
        {head}
        <div className="grid gap-3 p-5" role="status" aria-label="Comparing">
          <Skeleton className="h-4 w-2/3" />
          <Skeleton className="aspect-[16/10] h-auto w-full" />
        </div>
      </Card>
    );
  }
  const error = since ? sinceRun.error : baseline.error;
  if (error || diff.isError) {
    return (
      <Card>
        {head}
        <ErrorState error={error ?? diff.error} onRetry={() => void (error ? (since ? sinceRun.refetch() : baseline.refetch()) : diff.refetch())} />
      </Card>
    );
  }
  if (!before || !diff.data) {
    return (
      <Card>
        {head}
        <EmptyState
          icon={<GitCompareArrows />}
          title="Nothing to compare with yet"
          description={
            baseline.data?.capture === run.capture
              ? 'This check is the baseline of its page: later checks are compared with it.'
              : 'No baseline is kept for this page. Pick an earlier check of it, or ask your agent to keep one with ui_baseline.'
          }
        />
      </Card>
    );
  }
  const data = diff.data;
  const shown = data.screens.find((item) => item.screen === screen) ?? data.screens.find((item) => item.status === 'changed') ?? data.screens[0];
  return (
    <Card>
      {head}
      <div className="grid gap-4 p-5">
        <p className="text-body text-fg">{data.verdict}</p>
        <div className="flex flex-wrap gap-1" role="group" aria-label="Screens compared">
          {data.screens.map((item) => (
            <button
              key={item.screen}
              type="button"
              aria-pressed={shown?.screen === item.screen}
              onClick={() => onScreen(item.screen)}
              title={item.reason}
              className={cn(
                'inline-flex h-7 items-center gap-2 rounded-full border px-3 text-secondary',
                shown?.screen === item.screen ? 'border-fg bg-fg text-on-primary' : 'border-line bg-card text-muted hover:text-fg',
              )}
            >
              <span className="font-mono text-code-caption">{keyWords(item.screen)}</span>
              <span className={shown?.screen === item.screen ? undefined : 'text-faint'}>{item.status === 'changed' ? 'changed' : item.status === 'same' ? 'same' : 'skipped'}</span>
            </button>
          ))}
        </div>
        {data.not_compared ? <p className="text-secondary text-faint">{data.not_compared}</p> : null}
        {shown ? (
          shown.status === 'skipped' ? (
            <p className="text-body text-muted">Not compared{shown.reason ? `: ${shown.reason}` : '.'}</p>
          ) : shown.status === 'same' ? (
            <p className="flex items-center gap-2 text-body text-muted">
              <CircleCheck aria-hidden="true" className="size-4 text-ok" />
              {keyWords(shown.screen)} looks the same.
            </p>
          ) : (
            <>
              {shown.lines.length ? (
                <ol className="grid gap-2">
                  {shown.lines.map((line, index) => (
                    <li key={index} className="flex gap-3 text-body text-fg">
                      <span className="mt-px inline-flex h-5 shrink-0 items-center rounded-full border border-line px-2 text-micro text-muted">Cause</span>
                      <span className="min-w-0 break-words">{line}</span>
                    </li>
                  ))}
                </ol>
              ) : null}
              <DiffPicture org={org} before={data.before} after={data.after} screen={shown.screen} />
              {shown.changes.length ? (
                <TableScroll className="-mx-5">
                  <Table density="compact" aria-label="What changed">
                    <thead>
                      <tr>
                        <Th>Change</Th>
                        <Th>Element</Th>
                        <Th>Component</Th>
                      </tr>
                    </thead>
                    <tbody>
                      {shown.changes.map((change, index) => (
                        <tr key={index}>
                          <Td>{CHANGE_WORDS[change.kind] ?? change.kind}</Td>
                          <Td main>
                            <span className="block truncate font-mono text-code" title={change.key}>
                              {change.key}
                            </span>
                          </Td>
                          <Td className="text-muted">{change.component ?? <span className="text-faint">–</span>}</Td>
                        </tr>
                      ))}
                    </tbody>
                  </Table>
                </TableScroll>
              ) : null}
            </>
          )
        ) : null}
      </div>
      {shown && shown.status === 'changed' ? (
        <CardFooter>
          {percent(shown.pixels_changed)} of pixels differ
          {shown.unexplained.length ? `, ${plural(shown.unexplained.length, 'area')} with no cause found` : ''}.
        </CardFooter>
      ) : null}
    </Card>
  );
}
