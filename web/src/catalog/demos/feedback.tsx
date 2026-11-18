/** Status and feedback: the status mark and word, the person states, loading, empty, error, and inline refusals. */
import { Inbox } from 'lucide-react';
import { NotFound } from '@/components/not-found';
import { Card } from '@/components/ui/card';
import { Meter } from '@/components/ui/misc';
import { Spinner } from '@/components/ui/spinner';
import { StateBadge } from '@/components/ui/state-badge';
import { EmptyState, ErrorState, InlineError, NoAccess, NotLive, Skeleton } from '@/components/ui/states';
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
      <Example label="Spinner, and Meter (ink, amber from 80%, red from 95%)">
        <Row className="gap-6">
          <Spinner className="text-muted" />
          <Meter value={40} max={100} label="Memory" className="w-32" />
          <Meter value={84} max={100} label="Disk" className="w-32" />
          <Meter value={97} max={100} label="Budget" className="w-32" />
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
    </div>
  );
}
