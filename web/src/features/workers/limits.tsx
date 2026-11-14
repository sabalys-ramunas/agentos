/** Is it staying inside its limits. Steps, not money: money is a Technical detail here. */
import { useState } from 'react';
import { useSetWorkerLimits } from '@/api/hooks';
import type { WorkerLimits, WorkerOverview } from '@/api/types';
import { Button } from '@/components/ui/button';
import { Section } from '@/components/ui/card';
import { Dialog } from '@/components/ui/dialog';
import { Field } from '@/components/ui/field';
import { Meter } from '@/components/ui/misc';
import { Select } from '@/components/ui/select';
import { usd } from '@/lib/format';
import { useTechnical } from '@/lib/technical';
import { limitsTitle } from './words';

function Tile({ label, value, of, note }: { label: string; value: number; of: number; note: string }) {
  return (
    <div className="grid min-w-0 content-start gap-2 bg-bg px-4 py-3 sm:px-6 sm:py-4">
      <p className="text-13 text-muted">{label}</p>
      <p className="text-20 font-semibold">
        <span className="tabular">{value.toLocaleString('en-GB')}</span>
        <span className="text-14 font-normal text-faint"> of {of.toLocaleString('en-GB')}</span>
      </p>
      <Meter value={value} max={of} label={label} />
      <p className="text-13 text-pretty text-faint">{note}</p>
    </div>
  );
}

const CHOICES: Record<keyof Omit<WorkerLimits, 'usd_per_day'>, number[]> = {
  steps_per_day: [1_000, 2_000, 5_000, 10_000],
  help_per_day: [10, 20, 40, 80],
  steps_per_thing: [30, 60, 90],
};

function LimitsDialog({ org, data, open, onClose }: { org: string; data: WorkerOverview; open: boolean; onClose: () => void }) {
  const set = useSetWorkerLimits(org);
  const pick = (key: keyof typeof CHOICES, label: string, hint: string) => (
    <Field label={label} hint={hint}>
      <Select
        aria-label={label}
        value={String(data.limits[key])}
        options={CHOICES[key].map((value) => ({ value: String(value), label: value.toLocaleString('en-GB') }))}
        onValueChange={(value) => set.mutate({ [key]: Number(value) })}
      />
    </Field>
  );
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()} title="Change its limits" description="Each one saves as you choose it, and takes effect on the next step." footer={<Button onClick={onClose}>Done</Button>}>
      <div className="grid gap-4">
        {pick('steps_per_day', 'Steps in a day', 'At this many it stops until midnight.')}
        {pick('help_per_day', 'Times it can ask for help in a day', 'After this it stops and asks you instead.')}
        {pick('steps_per_thing', 'Steps on any one thing', 'A thing that takes more stops, and tells you.')}
        {set.isError ? <p role="alert" className="text-13 text-red">That did not save, so the last saved limit is back.</p> : null}
      </div>
    </Dialog>
  );
}

export function LimitsPanel({ org, data, canChange }: { org: string; data: WorkerOverview; canChange: boolean }) {
  const [technical] = useTechnical();
  const [open, setOpen] = useState(false);
  const { today, limits } = data;
  const room = Math.max(0, limits.steps_per_day - today.steps);
  return (
    <Section title={limitsTitle(data)} action={canChange ? <Button size="sm" variant="ghost" onClick={() => setOpen(true)}>Change limits</Button> : undefined}>
      <div className={`grid gap-px overflow-hidden rounded-xl border border-line bg-line ${technical ? 'sm:grid-cols-2 lg:grid-cols-4' : 'sm:grid-cols-3'}`}>
        <Tile label="Steps today" value={today.steps} of={limits.steps_per_day} note={room ? `Room for ${room.toLocaleString('en-GB')} more` : 'Starts again at midnight'} />
        <Tile label="Asked for help" value={today.helped} of={limits.help_per_day} note="Then it asks you instead" />
        <Tile label="Most steps on one thing" value={today.longest} of={limits.steps_per_thing} note="It stops there, and tells you" />
        {technical ? (
          <div className="grid min-w-0 content-start gap-2 bg-bg px-4 py-3 sm:px-6 sm:py-4">
            <p className="text-13 text-muted">AI usage today</p>
            <p className="text-20 font-semibold"><span className="tabular">{usd(today.usd)}</span><span className="text-14 font-normal text-faint"> of {usd(limits.usd_per_day)}</span></p>
            <Meter value={today.usd} max={limits.usd_per_day} label="AI usage today" />
            <p className="text-13 text-faint">About {usd(today.steps ? today.usd / today.steps : 0)} a step</p>
          </div>
        ) : null}
      </div>
      <LimitsDialog org={org} data={data} open={open} onClose={() => setOpen(false)} />
    </Section>
  );
}
