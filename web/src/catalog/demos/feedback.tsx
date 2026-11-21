/** Status and feedback: the status mark and word, the person states, loading, empty, error, and inline refusals. */
import { Inbox } from 'lucide-react';
import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { SettingRow } from '@/components/ui/form';
import { switched, toastChange } from '@/components/ui/toast';
import { Switch } from '@/components/ui/toggle';
import { NotFound } from '@/components/not-found';
import { Card } from '@/components/ui/card';
import { Meter } from '@/components/ui/misc';
import { Spinner } from '@/components/ui/spinner';
import { StateBadge } from '@/components/ui/state-badge';
import { Banner, EmptyState, ErrorState, InlineError, NoAccess, NotLive, Skeleton } from '@/components/ui/states';
import { Status, StatusDot } from '@/components/ui/status';
import type { Tone } from '@/lib/labels';
import type { PersonState } from '@/lib/states';
import { Example, Row } from '../parts';

const TONES: Array<[Tone, string, boolean?]> = [
  ['ok', 'Passed'],
  ['warn', 'Needs a look'],
  ['bad', 'Failed'],
  ['live', 'Running', true],
  ['idle', 'Waiting'],
  ['neutral', 'Queued'],
];

const PEOPLE: PersonState[] = ['good', 'waiting', 'stuck', 'sleeping', 'working'];

export function StatusDemo() {
  return (
    <Card className="grid gap-4 p-5">
      <Example label="Status: a shape and a word. The word takes its colour only when it asks for attention">
        <Row className="gap-x-6 gap-y-3">
          {TONES.map(([tone, label, pulse]) => (
            <Status key={tone} status={{ tone, label, pulse }} />
          ))}
        </Row>
      </Example>
      <Example label="StatusDot alone, beside words of your own">
        <Row className="gap-4">
          {TONES.map(([tone, , pulse]) => (
            <StatusDot key={tone} tone={tone} pulse={pulse} />
          ))}
        </Row>
      </Example>
      <Example label="StateBadge: what a person reads on Home, Accounts and Machines">
        <Row className="gap-x-6 gap-y-3">
          {PEOPLE.map((state) => (
            <StateBadge key={state} state={state} />
          ))}
        </Row>
      </Example>
      <Example label="StateBadge chip: the state at the head of a card or a page">
        <Row className="gap-3">
          {PEOPLE.map((state) => (
            <StateBadge key={state} state={state} chip />
          ))}
        </Row>
      </Example>
      <Example label="Spinner, and Meter (ink, amber from 80%, red from 95%; progress: ink, green when complete)">
        <Row className="gap-6">
          <Spinner className="text-muted" />
          <Meter value={40} max={100} label="Memory" className="w-32" />
          <Meter value={84} max={100} label="Disk" className="w-32" />
          <Meter value={97} max={100} label="Budget" className="w-32" />
          <Meter value={9} max={10} label="Accounts ready" progress className="w-32" />
          <Meter value={8} max={8} label="Accounts ready, all" progress className="w-32" />
        </Row>
      </Example>
      <Example label="InlineError: a refusal said where it happened">
        <InlineError>That did not save, so the last saved limit is back.</InlineError>
      </Example>
    </Card>
  );
}

export function StatesDemo() {
  return (
    <div className="grid gap-4 md:grid-cols-3">
      <Card className="grid content-start gap-3 p-5" aria-label="Loading">
        <Skeleton className="w-2/3" />
        <Skeleton className="w-1/2" />
        <Skeleton className="h-20" />
      </Card>
      <Card>
        <EmptyState icon={<Inbox />} title="Nothing needs you" description="Requests from your agents wait here until you decide." />
      </Card>
      <Card>
        <ErrorState error={new Error('down')} onRetry={() => undefined} />
      </Card>
      <Card>
        <NotLive />
      </Card>
      <Card>
        <NoAccess />
      </Card>
      <Card>
        <NotFound />
      </Card>
      <div className="grid gap-3 md:col-span-3">
        <Banner tone="bad" title="Reddit needs a look:" action={<Button size="sm">Open the account</Button>}>
          it is signed in as the wrong account.
        </Banner>
        <Banner tone="warn" title="Asks first for now." action={<Button size="sm">Clear it</Button>}>
          An agent read a page at coffee-reviews.example, so its next post asks you first.
        </Banner>
        <Banner tone="ok" title="The log is intact.">
          Every step matches its signed checkpoint. 1,451 steps checked.
        </Banner>
        <Banner title="Names only.">Secrets stay on your computers and never leave them.</Banner>
      </div>
    </div>
  );
}

/** A setting that saves on its own says what changed in a toast, with Undo for about six seconds. */
export function ChangeToastDemo() {
  const [on, setOn] = useState(false);
  const flip = (next: boolean) => {
    setOn(next);
    toastChange({
      ...switched('Quiet hours', next),
      // The catalog has no node: Undo waits a moment, as a save would, and puts the switch back.
      undo: () => new Promise((resolve) => setTimeout(() => resolve(setOn(!next)), 600)),
    });
  };
  return (
    <Card className="grid gap-4 p-5">
      <Example label="toastChange: what changed, in plain words, and Undo through the same save path. Hover or focus holds it">
        <Row className="gap-x-6 gap-y-3">
          <SettingRow label="Quiet hours" hint="Pushes wait until the end, in the time zone below.">
            <Switch aria-label="Quiet hours" checked={on} onCheckedChange={flip} />
          </SettingRow>
          <Button size="sm" onClick={() => toastChange({ said: 'Answers in chat turned off' })}>
            A change with no Undo
          </Button>
        </Row>
      </Example>
    </Card>
  );
}
