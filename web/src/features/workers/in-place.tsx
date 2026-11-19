/**
 * What a worker did, in the places a person already looks. No timeline of its own: a thing done in
 * History shows its steps, a question in Needs you shows how it got there, and an account says what
 * the worker is doing on it.
 */
import { Link } from '@tanstack/react-router';
import { MousePointerClick } from 'lucide-react';
import { useState } from 'react';
import { useAccess, useSetWorkerAccount, useWorkerAccount, useWorkerOnAccount } from '@/api/hooks';
import { isCancelled } from '@/api/http';
import type { Lane, WorkerThing } from '@/api/types';
import { AgentMark } from '@/components/brand/identity';
import { Card } from '@/components/ui/card';
import { Switch } from '@/components/ui/toggle';
import { cn } from '@/lib/cn';
import { computerName } from '@/lib/computers';
import { principalName } from '@/lib/principal';
import { plural } from '@/lib/format';
import { accountName } from '@/features/home/words';
import { GiveAJob } from '@/features/lane/give-a-job';
import { StepLine, StepLineMini } from './step-line';
import { accountWorkerSentence, STOP_WORDS, thingLine, WORKER_NAME } from './words';

/** In a thing done: who said it, and each step, with who thought at each one. */
export function WorkerSteps({ thing }: { thing: WorkerThing }) {
  return (
    <section aria-label="The steps" className="grid gap-3 border-t border-line pt-4">
      <div className="min-w-0">
        <h3 className="text-body font-semibold">The browser worker did the steps</h3>
        <p className="text-secondary text-muted">{principalName(thing.said_by)} said what to do. {plural(thing.steps.length, 'step')}, and pointing at one reads it.</p>
      </div>
      <StepLine thing={thing} />
    </section>
  );
}

/** Above a worker's question: how it got here, so the answer is not a guess. */
export function WorkerStopped({ thing }: { thing: WorkerThing }) {
  return (
    <Card className="grid gap-3 p-4">
      <p className="flex min-w-0 items-start gap-2 text-body text-pretty">
        <AgentMark principal={thing.said_by} size={20} className="mt-px" />
        <span className="min-w-0"><span className="text-muted">{principalName(thing.said_by)} said:</span> {thing.told}</span>
      </p>
      <StepLine thing={thing} />
    </Card>
  );
}

/** One quiet strip on an account, and only while the worker has something to say about it. */
export function WorkerOnAccount({ org, lane }: { org: string; lane: string }) {
  const thing = useWorkerOnAccount(org, lane).data;
  if (!thing) return null;
  return (
    <div className="grid gap-2 rounded-xl border border-line px-4 py-3 sm:grid-cols-[minmax(0,1fr)_200px] sm:items-center sm:gap-4">
      <p className="min-w-0 text-secondary text-pretty">
        <span className="font-medium">{thingLine(thing)}</span>{' '}
        <span className="text-muted">{thing.stop ? STOP_WORDS[thing.stop] : `${principalName(thing.said_by)} said what to do.`}</span>{' '}
        <Link to="/$org/settings/workers" params={{ org }} className="link whitespace-nowrap">Workers</Link>
      </p>
      <StepLineMini thing={thing} />
    </div>
  );
}

/**
 * On or off for one account, under Ask me first. The row is only there while a worker runs on this
 * computer, and the switch only while the computer can hear it. Off is one flip. On is the direction
 * only a person takes, so it goes through the step-up, and a cancelled one rolls back in silence.
 */
export function AccountWorkerRow({ org, lane }: { org: string; lane: Lane }) {
  const worker = useWorkerAccount(org, lane.id).data;
  const access = useAccess(org);
  // Ask me first's two rungs, the other way round: taking the worker off is the stricter direction.
  const can = { off: access.can('ask_first.on'), on: access.can('ask_first.off') };
  const set = useSetWorkerAccount(org);
  const [refused, setRefused] = useState<string | null>(null);
  if (!worker) return null;
  const flip = (on: boolean) => {
    setRefused(null);
    set.mutate(
      { lane: lane.id, on },
      { onError: (error) => !isCancelled(error) && setRefused(`${computerName(lane.node)} did not answer, so the worker stays ${on ? 'off' : 'on'} here.`) },
    );
  };
  const word = worker.on ? 'On' : 'Off';
  const locked = !worker.on && !can.on;
  const note = !worker.online ? null : !can.off ? 'An owner or admin can change this.' : locked ? 'An owner or admin can turn this on.' : null;
  const sentence = [accountWorkerSentence(worker), note].filter(Boolean).join(' ');
  return (
    <section aria-label={WORKER_NAME.browser} className="border-t border-line pt-4">
      <div className="flex min-h-5 items-center gap-3">
        <MousePointerClick aria-hidden="true" className="size-4 shrink-0 text-faint" />
        <h2 className="min-w-0 flex-1 text-label">{WORKER_NAME.browser}</h2>
        <span className="flex shrink-0 items-center gap-2 text-secondary text-muted">
          {word}
          {worker.online && can.off ? (
            <Switch
              aria-label={`Browser worker on ${accountName(lane)}`}
              aria-disabled={set.isPending || locked || undefined}
              checked={worker.on}
              onCheckedChange={(next) => !set.isPending && !locked && flip(next)}
              className="relative before:absolute before:-inset-x-2 before:-inset-y-3 aria-disabled:opacity-50"
            />
          ) : null}
        </span>
      </div>
      <p aria-live="polite" className={cn('mt-1 pl-7 text-caption text-pretty', refused ? 'text-bad' : 'text-faint')}>
        {refused ?? (set.isPending ? 'Telling the computer.' : sentence)}
      </p>
      {worker.on && worker.online && access.can('lanes.operate') ? <GiveAJob org={org} lane={lane} /> : null}
    </section>
  );
}
