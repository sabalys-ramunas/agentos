/**
 * The findings of an open check, grouped by component as the node groups them. Each one says what was
 * measured, what was expected, the fix, how it is known and where else it was seen, and gives the
 * text an agent can act on. The picked one opens; picking moves the picture to it.
 */
import { Check, Copy } from 'lucide-react';
import { useEffect, useRef } from 'react';
import type { ReactNode } from 'react';
import type { StudioRun } from '@/api/types';
import { Button } from '@/components/ui/button';
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

function Line({ label, children, tone }: { label: string; children: ReactNode; tone?: 'bad' | 'ok' }) {
  return (
    <>
      <dt className="text-secondary text-faint">{label}</dt>
      <dd className={cn('min-w-0 font-mono text-code break-words', tone === 'bad' ? 'text-bad' : tone === 'ok' ? 'text-ok' : 'text-fg')}>{children}</dd>
    </>
  );
}

function Item({
  run,
  finding,
  screen,
  picked,
  onPick,
  onScreenPick,
}: {
  run: StudioRun;
  finding: NumberedFinding;
  screen: string;
  picked: boolean;
  onPick: () => void;
  onScreenPick: (key: string) => void;
}) {
  const here = onScreen(finding, screen);
  const measured = recordLine(finding.measured);
  const expected = recordLine(finding.expected);
  const others = otherSelectors(finding);
  const ref = useRef<HTMLLIElement>(null);
  useEffect(() => {
    if (picked) ref.current?.scrollIntoView({ block: 'nearest' });
  }, [picked]);
  return (
    <li ref={ref} data-picked={picked ? '' : undefined} className={cn('border-b border-line last:border-b-0', picked && 'bg-hover shadow-[inset_2px_0_0_var(--color-fg)]')}>
      <button
        type="button"
        aria-pressed={picked}
        aria-label={`Finding ${finding.n}: ${findingTitle(finding)}, ${SEVERITY_WORD[finding.severity]}`}
        onClick={onPick}
        className="focus-inset flex w-full items-start gap-3 px-5 py-3 text-left"
      >
        <span
          aria-hidden="true"
          className={cn(
            'mt-px inline-flex size-5 shrink-0 items-center justify-center rounded-full font-mono text-micro tabular',
            finding.severity === 'blocker'
              ? 'bg-bad-dot text-status-ink'
              : finding.severity === 'major'
                ? 'bg-warn-dot text-status-ink'
                : 'border border-line-strong text-muted',
          )}
        >
          {finding.n}
        </span>
        <span className="grid min-w-0 flex-1 gap-1">
          <span className={cn('text-14 font-medium break-words', here ? 'text-fg' : 'text-muted')}>{findingTitle(finding)}</span>
          <span className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <Status status={{ label: SEVERITY_WORD[finding.severity], tone: SEVERITY_TONE[finding.severity] }} />
            <span className="font-mono text-code-caption text-muted">{finding.check}</span>
            {!here ? <span className="text-caption text-faint">not on this screen</span> : null}
          </span>
          {!picked && finding.fix ? (
            <span title={finding.fix} className="line-clamp-1 text-secondary text-faint">
              {finding.fix}
            </span>
          ) : null}
        </span>
      </button>
      {picked ? (
        <div className="grid gap-3 px-5 pb-4 pl-13">
          <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1">
            {measured ? <Line label="Measured" tone="bad">{measured}</Line> : null}
            {expected ? <Line label="Expected" tone="ok">{expected}</Line> : null}
            {finding.fix ? (
              <>
                <dt className="text-secondary text-faint">Fix</dt>
                <dd className="min-w-0 text-14 break-words text-fg">{finding.fix}</dd>
              </>
            ) : null}
            <Line label="Where">
              {finding.where.selector}
              {finding.component ? <span className="text-faint"> in {finding.component}</span> : null}
            </Line>
            {others.length ? (
              <Line label="Also at">
                {others.slice(0, 4).join(', ')}
                {others.length > 4 ? <span className="text-faint"> and {others.length - 4} more</span> : null}
              </Line>
            ) : null}
          </dl>
          <p className="text-secondary text-muted">{confidenceWords(finding)}</p>
          <div className="flex flex-wrap items-center gap-2">
            <span className="flex flex-wrap gap-1" role="group" aria-label="Seen on">
              {finding.seen_at.length === 0 ? <span className="text-caption text-faint">Seen on the whole page</span> : null}
              {finding.seen_at.map((seen) => (
                <button
                  key={seen.screen}
                  type="button"
                  aria-pressed={seen.screen === screen}
                  onClick={() => onScreenPick(seen.screen)}
                  title={`Seen ${plural(seen.count, 'time')} on ${keyWords(seen.screen)}`}
                  className={cn(
                    'inline-flex h-6 items-center gap-1 rounded-sm border px-1 font-mono text-code-caption',
                    seen.screen === screen ? 'border-fg text-fg' : 'border-line text-muted hover:text-fg',
                  )}
                >
                  {keyWords(seen.screen)}
                  {seen.count > 1 ? <span className="text-faint">×{seen.count}</span> : null}
                </button>
              ))}
            </span>
            <span className="ml-auto">
              <CopyForAgent run={run} finding={finding} />
            </span>
          </div>
        </div>
      ) : null}
    </li>
  );
}

export function FindingsList({
  run,
  findings,
  screen,
  picked,
  onPick,
  onScreenPick,
}: {
  run: StudioRun;
  findings: NumberedFinding[];
  screen: string;
  picked: NumberedFinding | undefined;
  onPick: (finding: NumberedFinding) => void;
  onScreenPick: (key: string) => void;
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
          <h3 className="flex items-center gap-2 border-b border-line px-5 pt-4 pb-2 text-caption text-faint">
            <span className="min-w-0 truncate font-mono text-code-caption text-muted" title={group.name}>
              {group.name === 'page' ? 'The whole page' : group.name}
            </span>
            <span className="ml-auto tabular">{group.items.length}</span>
          </h3>
          <ol>
            {group.items.map((finding) => (
              <Item
                key={finding.id}
                run={run}
                finding={finding}
                screen={screen}
                picked={picked?.id === finding.id}
                onPick={() => onPick(finding)}
                onScreenPick={onScreenPick}
              />
            ))}
          </ol>
        </section>
      ))}
    </div>
  );
}
