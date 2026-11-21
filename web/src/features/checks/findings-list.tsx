/**
 * The findings of an open check: the open one as its own card (what was measured, what was expected, the
 * fix, how it is known, where else it was seen, and the text an agent can act on), and every finding in a
 * list grouped by component as the node groups them. Picking a row opens it and moves the picture to it.
 */
import { Check, Copy } from 'lucide-react';
import { useEffect, useRef } from 'react';
import type { ReactNode } from 'react';
import type { StudioRun } from '@/api/types';
import { Chip } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { useCopy } from '@/components/ui/misc';
import { Status } from '@/components/ui/status';
import { cn } from '@/lib/cn';
import { plural } from '@/lib/format';
import {
  SEVERITY_TONE,
  SEVERITY_WORD,
  confidenceWords,
  findingTitle,
  forAgent,
  keyWords,
  onScreen,
  otherSelectors,
  recordLine,
} from './model';
import type { NumberedFinding } from './model';

function CopyForAgent({ run, finding }: { run: StudioRun; finding: NumberedFinding }) {
  const { copied, copy } = useCopy();
  return (
    <Button size="sm" icon={copied ? <Check className="size-4 text-ok" /> : <Copy className="size-4" />} onClick={() => copy(forAgent(run, finding))}>
      {copied ? 'Copied' : 'Copy for the agent'}
    </Button>
  );
}

function Line({ label, children, mono = false }: { label: string; children: ReactNode; mono?: boolean }) {
  return (
    <>
      <dt className="text-body text-muted">{label}</dt>
      <dd className={cn('min-w-0 break-words text-fg', mono ? 'self-center font-mono text-code' : 'text-body')}>{children}</dd>
    </>
  );
}

const EDGE: Record<NumberedFinding['severity'], string> = {
  blocker: 'border-bad-line',
  major: 'border-warn-line',
  minor: '',
  info: '',
};

/** The number a finding wears on the picture, so a row and its pin read as one. */
function Pin({ finding }: { finding: NumberedFinding }) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        'inline-flex size-5 shrink-0 items-center justify-center rounded-full font-mono text-micro tabular',
        finding.severity === 'blocker'
          ? 'bg-bad-dot text-status-ink'
          : finding.severity === 'major'
            ? 'bg-warn-dot text-status-ink'
            : 'border border-line-strong text-muted',
      )}
    >
      {finding.n}
    </span>
  );
}

const severityOf = (finding: NumberedFinding) => ({ label: SEVERITY_WORD[finding.severity], tone: SEVERITY_TONE[finding.severity] });

/**
 * The open finding as its own card, edged in its severity: what was measured, what was expected, the fix,
 * where, how it is known, the screens it was seen on, and the text an agent can act on.
 */
export function OpenFinding({
  run,
  finding,
  screen,
  onScreenPick,
}: {
  run: StudioRun;
  finding: NumberedFinding;
  screen: string;
  onScreenPick: (key: string) => void;
}) {
  const measured = recordLine(finding.measured);
  const expected = recordLine(finding.expected);
  const others = otherSelectors(finding);
  return (
    <Card className={cn('min-w-0', EDGE[finding.severity])}>
      <section aria-label={`Finding ${finding.n}: ${findingTitle(finding)}`} className="grid gap-4 p-5">
        <div className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-2">
          <Pin finding={finding} />
          <Status chip status={severityOf(finding)} />
          <h2 className="min-w-0 flex-1 text-card-title break-words max-md:basis-full">{findingTitle(finding)}</h2>
          <span className="font-mono text-code-caption text-muted max-md:hidden">{finding.check}</span>
        </div>
        <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-6 gap-y-2">
          {measured ? <Line label="Measured" mono>{measured}</Line> : null}
          {expected ? <Line label="Expected" mono>{expected}</Line> : null}
          {finding.fix ? <Line label="Fix">{finding.fix}</Line> : null}
          <Line label="Where" mono>
            {finding.where.selector}
            {finding.component ? <span className="text-muted"> in {finding.component}</span> : null}
          </Line>
          {others.length ? (
            <Line label="Also at" mono>
              {others.slice(0, 4).join(', ')}
              {others.length > 4 ? <span className="text-muted"> and {others.length - 4} more</span> : null}
            </Line>
          ) : null}
          <dt className="text-body text-muted">Seen on</dt>
          <dd className="flex min-w-0 flex-wrap items-center gap-2">
            {finding.seen_at.length === 0 ? <span className="text-body text-muted">The whole page</span> : null}
            {finding.seen_at.map((seen) => (
              <Chip
                key={seen.screen}
                pressed={seen.screen === screen}
                onClick={() => onScreenPick(seen.screen)}
                title={`Seen ${plural(seen.count, 'time')} on ${keyWords(seen.screen)}`}
                label={
                  <>
                    {keyWords(seen.screen)}
                    {seen.count > 1 ? <span className="tabular"> ×{seen.count}</span> : null}
                  </>
                }
              />
            ))}
          </dd>
        </dl>
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <CopyForAgent run={run} finding={finding} />
          <p className="min-w-0 flex-1 basis-60 text-secondary text-muted">{confidenceWords(finding)}</p>
        </div>
      </section>
    </Card>
  );
}

function Row({ finding, screen, picked, onPick }: { finding: NumberedFinding; screen: string; picked: boolean; onPick: () => void }) {
  const here = onScreen(finding, screen);
  const ref = useRef<HTMLLIElement>(null);
  useEffect(() => {
    if (picked) ref.current?.scrollIntoView?.({ block: 'nearest' });
  }, [picked]);
  return (
    <li ref={ref} className={cn('border-b border-l-2 border-b-line last:border-b-0', picked ? 'border-l-accent bg-accent-soft' : 'border-l-transparent')}>
      <button
        type="button"
        aria-pressed={picked}
        aria-label={`Finding ${finding.n}: ${findingTitle(finding)}, ${SEVERITY_WORD[finding.severity]}`}
        onClick={onPick}
        className="focus-inset grid w-full grid-cols-[auto_minmax(0,1fr)] items-start gap-x-3 gap-y-1 py-3 pr-5 pl-4 text-left transition-colors duration-(--duration-fast) hover:bg-hover md:grid-cols-[auto_88px_minmax(0,1fr)_auto] md:items-center"
      >
        <Pin finding={finding} />
        <span className="max-md:hidden">
          <Status chip status={severityOf(finding)} />
        </span>
        <span className="grid min-w-0 gap-1">
          <span className={cn('text-body break-words', picked ? 'font-medium text-fg' : here ? 'text-fg' : 'text-muted')}>{findingTitle(finding)}</span>
          <span className="flex flex-wrap items-center gap-x-3 gap-y-1 md:hidden">
            <Status status={severityOf(finding)} />
            <span className="font-mono text-code-caption text-muted">{finding.check}</span>
          </span>
          {!here ? <span className="text-caption text-faint">Not on this screen</span> : null}
        </span>
        <span className="font-mono text-code-caption text-muted max-md:hidden">{finding.check}</span>
      </button>
    </li>
  );
}

/** Every finding, grouped by component as the node groups them, the open one selected. */
export function FindingsList({
  findings,
  screen,
  picked,
  onPick,
}: {
  findings: NumberedFinding[];
  screen: string;
  picked: NumberedFinding | undefined;
  onPick: (finding: NumberedFinding) => void;
}) {
  const groups: { name: string; items: NumberedFinding[] }[] = [];
  for (const finding of findings) {
    const last = groups[groups.length - 1];
    if (last && last.name === finding.group) last.items.push(finding);
    else groups.push({ name: finding.group, items: [finding] });
  }
  return (
    <div>
      {groups.map((group) => (
        <section key={group.name} aria-label={group.name === 'page' ? 'The page' : group.name}>
          <h3 className="flex items-center gap-2 border-b border-line bg-inset px-5 py-2 text-caption text-muted">
            <span className="min-w-0 truncate font-mono text-code-caption" title={group.name}>
              {group.name === 'page' ? 'The whole page' : group.name}
            </span>
            <span className="ml-auto tabular">{group.items.length}</span>
          </h3>
          <ol>
            {group.items.map((finding) => (
              <Row key={finding.id} finding={finding} screen={screen} picked={picked?.id === finding.id} onPick={() => onPick(finding)} />
            ))}
          </ol>
        </section>
      ))}
    </div>
  );
}
