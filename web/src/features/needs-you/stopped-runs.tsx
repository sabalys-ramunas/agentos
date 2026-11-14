/**
 * Runs that stopped halfway (the node: `POST /runs/{id}/decide`). When the computer stops in the middle of a
 * step that does something (pressing Post, paying), the step may or may not have happened, and only a person
 * can look and say which. Trying again blind could post twice, so nothing here decides by itself. Three plain
 * answers: Leave it (it fails), It went through (the run carries on after that step), Try again (that step
 * runs again). Leave it sits left, as a no always does. The owner's; the list is empty where not served.
 */
import { toast } from 'sonner';
import { useDecideRun } from '@/api/hooks';
import type { Run } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { StateGlyph } from '@/components/ui/state-badge';
import { describeError } from '@/lib/errors';
import { timeAgo } from '@/lib/format';

type Answer = 'retry' | 'skip' | 'fail';

const DONE: Record<Answer, string> = {
  retry: 'Trying that step again.',
  skip: 'Noted that it went through. The rest carries on.',
  fail: 'Left as it is. Nothing more is done.',
};

/** One stopped run, with what it was doing, and the three answers under the words. */
function StoppedCard({ org, run, business }: { org: string; run: Run; business: string }) {
  const decide = useDecideRun(org);
  const choose = (decision: Answer) =>
    decide.mutate({ id: run.id, decision }, { onSuccess: () => toast(DONE[decision]), onError: (error) => toast.error(describeError(error)) });
  const busy = (decision: Answer) => decide.isPending && decide.variables?.decision === decision;
  const money = run.effect === 'pay';
  return (
    <li className="flex items-start gap-3 py-3">
      <span className="flex size-10 shrink-0 items-center justify-center rounded-lg bg-(--wait-soft)">
        <StateGlyph state="waiting" size={20} />
      </span>
      <div className="grid min-w-0 flex-1 justify-items-start gap-3">
        <div className="min-w-0">
          <p className="text-14 font-medium break-words">{run.summary || `A step on ${business} stopped halfway`}</p>
          <p className="text-13 break-words text-faint">
            {money ? 'The payment may or may not have gone through.' : 'It may or may not have gone through.'} Look at the account, then say which. Stopped{' '}
            {timeAgo(run.finished_at ?? run.started_at)}.
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <Button size="sm" variant="ghost" loading={busy('fail')} disabled={decide.isPending} onClick={() => choose('fail')}>
            Leave it
          </Button>
          <Button size="sm" loading={busy('skip')} disabled={decide.isPending} onClick={() => choose('skip')}>
            It went through
          </Button>
          <Button size="sm" loading={busy('retry')} disabled={decide.isPending} onClick={() => choose('retry')}>
            Try again
          </Button>
        </div>
      </div>
    </li>
  );
}

/** The section on Needs you: each stopped run, with its three answers. Renders nothing when none stopped. */
export function StoppedRuns({ org, runs, businessOf }: { org: string; runs: Run[]; businessOf: (lane: string) => string }) {
  if (runs.length === 0) return null;
  return (
    <section aria-label="Stopped halfway">
      <div className="mb-1 px-2">
        <h2 className="text-14 font-semibold">Stopped halfway</h2>
        <p className="text-12 text-faint">The computer stopped in the middle of these. Only you can say whether they went through.</p>
      </div>
      <Card className="px-4">
        <ul className="divide-y divide-line">
          {runs.map((run) => (
            <StoppedCard key={run.id} org={org} run={run} business={businessOf(run.lane)} />
          ))}
        </ul>
      </Card>
    </section>
  );
}
