import { CircleAlert, CircleCheck, CircleX } from 'lucide-react';
import { useMemo } from 'react';
import { isNotServed } from '@/api/http';
import { useNodeHealthRounds, useNodeUpgrade, useRunHealthCheck, useWorkspaces } from '@/api/hooks';
import type { CheckStatus, HealthCheck, HealthRound } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { CodeBlock } from '@/components/ui/misc';
import { NotLive, Skeleton } from '@/components/ui/states';
import { cn } from '@/lib/cn';
import { plural, timeAgo } from '@/lib/format';
import { accessDetail, checkName, fixWords, type PlaceNames, placeNames, plainDetail } from '@/lib/plain-node';
import { useTechnical } from '@/lib/technical';
import { NotReported, Panel } from './parts';

const LOOK: Record<CheckStatus, { icon: typeof CircleCheck; word: string; tone: string }> = {
  ok: { icon: CircleCheck, word: 'Passes', tone: 'text-ok' },
  warn: { icon: CircleAlert, word: 'Worth a look', tone: 'text-warn' },
  fail: { icon: CircleX, word: 'Fails', tone: 'text-bad' },
};

const ORDER: Record<CheckStatus, number> = { fail: 0, warn: 1, ok: 2 };

/** The one sentence a person reads first: what fails, what needs a look, or that all is well. */
export function healthAnswer(checks: HealthCheck[]): string {
  const total = checks.length;
  const fail = checks.filter((check) => check.status === 'fail').length;
  const warn = checks.filter((check) => check.status === 'warn').length;
  if (fail) {
    const need = warn ? `, and ${warn} ${warn === 1 ? 'needs' : 'need'} a look` : '';
    return `${fail === 1 ? '1 check fails' : `${fail} checks fail`}${need}. Fix them first.`;
  }
  if (warn) return `${warn === 1 ? '1 check needs' : `${warn} checks need`} a look. ${total - warn} of ${total} pass.`;
  return `All ${total} checks pass.`;
}

/** What to do about a check, in a sentence. The node's command shows under Technical details. */
function FixLine({ fix }: { fix: string }) {
  const [technical] = useTechnical();
  const { plain, command } = fixWords(fix);
  return (
    <div className="mt-2 text-secondary break-words text-fg">
      <p>
        <span className="text-faint">To fix it: </span>
        {plain}
      </p>
      {technical ? <p className="mono mt-1 text-caption break-all text-faint">{command}</p> : null}
    </div>
  );
}

/** The node's raw sentence, with its command, for Technical details only. */
function RawLine({ raw, shown }: { raw: string; shown: string }) {
  const [technical] = useTechnical();
  return technical && raw !== shown ? <p className="mono mt-1 text-caption break-all text-faint">{raw}</p> : null;
}

function Rounds({ rounds, node, notHere, names }: { rounds: HealthRound | null | undefined; node: string; notHere: boolean; names: PlaceNames }) {
  if (notHere) return <Card className="p-4 sm:p-6"><NotLive /></Card>;
  if (rounds === undefined) return <Skeleton className="h-24 rounded-xl" />;
  if (rounds === null) {
    return (
      <Card className="p-4 sm:p-6">
        <p className="text-body">The health checks are not answering on {node}, so there is no round to show.</p>
        <p className="mt-1 text-secondary text-muted">It runs on its own while the machine is up. Start it and this fills in by itself.</p>
      </Card>
    );
  }
  const attention = rounds.findings.filter((finding) => finding.level !== 'ok');
  const when = rounds.at ? `Ran ${timeAgo(rounds.at)}.` : '';
  return (
    <Card className="p-4 sm:p-6">
      <p className="text-body">
        {attention.length ? `${plural(attention.length, 'thing')} need${attention.length === 1 ? 's' : ''} a look.` : 'Nothing needs a look.'}{' '}
        <span className="text-faint">{when}</span>
      </p>
      {attention.length ? (
        <ul className="mt-3 grid gap-3">
          {attention.map((finding) => (
            <li key={finding.key} className="text-secondary">
              <span className="break-words text-fg">{plainDetail(finding.title, names)}</span>
              <span className="text-muted">. {accessDetail(finding.detail, names)}</span>
            </li>
          ))}
        </ul>
      ) : null}
    </Card>
  );
}

/**
 * The node's own doctor, and its health round, and its version. The answer is first. A full check
 * probes the proxies too, so it runs on request, with a running state, and never changes anything.
 */
export function HealthTab({ org, node, checks, owner }: { org: string; node: string; checks: HealthCheck[] | null; owner: boolean }) {
  const run = useRunHealthCheck(org, node);
  const rounds = useNodeHealthRounds(org, node);
  // The cloud does not run or keep health checks, so a cloud has no check to start and no round to show.
  const notHere = isNotServed(rounds.error);
  // Nothing is offered until the cloud or the machine has answered whether it has the checks.
  const answered = !rounds.isPending;
  const upgrade = useNodeUpgrade(org, node);
  const workspaces = useWorkspaces(org);
  const names = useMemo(() => placeNames(workspaces.data), [workspaces.data]);
  const [technical] = useTechnical();
  if (checks === null) return <NotReported node={node} what="its health checks" />;

  const latest = run.data ?? null;
  const list = latest ? latest.checks : checks;
  const sorted = [...list].sort((a, b) => ORDER[a.status] - ORDER[b.status] || a.name.localeCompare(b.name));
  const passing = list.filter((check) => check.status === 'ok').length;
  const newest = latest ? latest.ran_at : list.reduce<string | null>((most, check) => (!most || check.at > most ? check.at : most), null);

  return (
    <div className="grid gap-10">
      <section aria-label="Health" className="grid gap-4">
        <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-3">
          <div className="min-w-0">
            <p className="text-body" aria-live="polite">
              {healthAnswer(list)}
            </p>
            <p className="mt-1 text-secondary text-faint">
              {run.isPending
                ? 'Checking now. Each proxy can take a few seconds.'
                : latest
                  ? `Full check ran ${timeAgo(latest.ran_at)}, probing the proxies too.`
                  : newest
                    ? `Quick check ran ${timeAgo(newest)}, without probing the proxies.`
                    : 'No check has run yet.'}
            </p>
          </div>
          {owner && answered && !notHere ? (
            <Button size="sm" variant="secondary" loading={run.isPending} onClick={() => run.mutate()}>
              {run.isPending ? 'Checking' : 'Run the full check'}
            </Button>
          ) : null}
        </div>
        {run.isError ? (
          <Card className="flex flex-wrap items-center justify-between gap-3 p-4 sm:p-6">
            <p className="text-secondary text-muted">The full check did not finish. Nothing on this machine was changed.</p>
            <Button size="sm" onClick={() => run.mutate()}>
              Try again
            </Button>
          </Card>
        ) : null}
        <div role="img" aria-label={`${passing} of ${list.length} checks pass`} style={{ gap: 2 }} className="flex h-2 overflow-hidden rounded-full">
          {sorted.map((check) => (
            <span key={check.id ?? check.name} className={cn('block h-full flex-1', check.status === 'ok' ? 'bg-fill-strong' : check.status === 'warn' ? 'bg-warn' : 'bg-bad')} />
          ))}
        </div>
        <Card className="divide-y divide-line overflow-hidden">
          {sorted.map((check) => {
            const look = LOOK[check.status];
            const detail = accessDetail(check.detail, names);
            return (
              <div key={check.id ?? check.name} className="flex items-start gap-3 px-4 py-3 sm:px-6">
                <span className="flex h-5 shrink-0 items-center">
                  <look.icon aria-hidden="true" className={cn('size-4', look.tone)} />
                </span>
                <div className="min-w-0 flex-1">
                  <p className="text-body break-words">
                    {checkName(check.name)}
                    <span className="sr-only">: {look.word}</span>
                  </p>
                  <p className="text-secondary break-words text-muted">{detail}</p>
                  <RawLine raw={check.detail} shown={detail} />
                  {check.status !== 'ok' && check.fix ? <FixLine fix={check.fix} /> : null}
                </div>
                <span className={cn('shrink-0 text-caption', check.status === 'ok' ? 'text-faint' : 'text-muted')}>{look.word}</span>
              </div>
            );
          })}
        </Card>
      </section>

      <Panel title="Health rounds" description="The machine checks itself on its own. This is the last round it ran.">
        <Rounds rounds={rounds.data} node={node} notHere={notHere} names={names} />
      </Panel>

      <Panel title="Version" description="The agentos this machine runs.">
        {upgrade.isPending ? (
          <Skeleton className="h-24 rounded-xl" />
        ) : upgrade.data ? (
          <div className="grid gap-4">
            <Card className="p-4 sm:p-6">
              <p className="text-body">
                Running agentos <span className="mono">{upgrade.data.running}</span>.
              </p>
              <p className="mt-1 text-secondary text-muted">
                This machine cannot see a newer release from here, so it does not say whether one is out.
              </p>
            </Card>
            <div className="grid gap-2">
              <p className="text-secondary text-muted">
                Upgrading restarts agentos on this machine, so every account pauses while it runs. Do it in a terminal on the machine,
                after pulling the new code.
              </p>
              {technical ? <CodeBlock code={upgrade.data.command} label="Upgrade command" /> : null}
            </div>
          </div>
        ) : null}
      </Panel>
    </div>
  );
}
