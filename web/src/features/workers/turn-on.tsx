/**
 * Turning a worker on. Only a person does this, and it says what changes before it happens. The
 * step-up comes after the press, from the same request, and a cancelled one changes nothing.
 */
import { Lock } from 'lucide-react';
import { useState } from 'react';
import { useBrains, useSetWorkerPlace } from '@/api/hooks';
import { isCancelled } from '@/api/http';
import type { WorkerOverview } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Dialog } from '@/components/ui/dialog';
import { computerName } from '@/lib/computers';
import { plural } from '@/lib/format';
import { inWords, NEVER, serviceName } from './words';
import { InlineError } from '@/components/ui/states';

const VERB = { decide: 'decides each step', write: 'writes text', rescue: 'helps when it is unsure' };

export function TurnOnDialog({ org, data, node, onClose }: { org: string; data: WorkerOverview; node: string | null; onClose: () => void }) {
  const set = useSetWorkerPlace(org);
  const brains = useBrains(org).data ?? [];
  const [refused, setRefused] = useState<string | null>(null);
  const place = data.places.find((item) => item.node === node);
  const name = node ? computerName(node) : '';
  const who = data.jobs.flatMap((item) => {
    const brain = brains.find((one) => one.name === item.brain);
    return brain ? [`${serviceName(brain)} ${VERB[item.job]}`] : [];
  });
  const turnOn = () => {
    if (!node) return;
    setRefused(null);
    set.mutate(
      { node, on: true },
      {
        onSuccess: onClose,
        onError: (error) => {
          if (!isCancelled(error)) setRefused(`${name} did not answer, so the worker stays off.`);
        },
      },
    );
  };
  return (
    <Dialog
      open={node !== null}
      onOpenChange={(open) => !open && onClose()}
      title={`Turn on the browser worker on ${name}?`}
      description={`Your agents can hand it the steps on ${plural(place?.accounts ?? 0, 'account')} there.`}
      footer={
        <>
          <Button onClick={onClose}>Don't turn on</Button>
          <Button variant="primary" loading={set.isPending} onClick={turnOn}>Turn it on</Button>
        </>
      }
    >
      <div className="grid gap-3">
        {who.length ? <p className="text-body text-pretty">{inWords(who)}.</p> : null}
        <p className="text-body text-pretty">
          It stops at {data.limits.steps_per_day.toLocaleString('en-GB')} steps a day, and at {data.limits.steps_per_thing} on any one thing.
        </p>
        <ul className="grid gap-1">
          {NEVER.slice(0, 2).map((line) => (
            <li key={line} className="flex items-start gap-2 text-secondary text-pretty text-muted">
              <span className="grid h-5 shrink-0 place-items-center text-faint"><Lock aria-hidden="true" className="size-3" /></span>
              {line}
            </li>
          ))}
        </ul>
        {refused ? <InlineError>{refused}</InlineError> : null}
      </div>
    </Dialog>
  );
}
