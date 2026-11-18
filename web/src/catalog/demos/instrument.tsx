/** The instrument layer: charts over time, bars, sparklines, pictures of pages, and the board. */
import { useState } from 'react';
import { StackedBars } from '@/components/charts/stacked-bars';
import { Sparkline } from '@/components/charts/small';
import { TimeChart } from '@/components/charts/time-chart';
import type { TimePoint } from '@/components/charts/time-chart';
import { BeforeAfter } from '@/components/ui/before-after';
import { Card, CardHeader } from '@/components/ui/card';
import { ImageFrame } from '@/components/ui/image-frame';
import { Example } from '../parts';
import { shot } from '../shot';

// A fixed hour, so the picture is the same on every run.
const START = Date.parse('2026-11-18T13:00:00Z');
const CPU = [12, 14, 18, 22, 31, 44, 38, 35, null, null, 26, 24, 29, 41, 52, 61, 58, 47, 39, 33, 30, 28, 26, 24];
const POINTS: TimePoint[] = CPU.map((value, index) => ({ at: new Date(START + index * 150_000).toISOString(), value }));
const BUCKETS = Array.from({ length: 12 }, (_, index) => ({
  at: new Date(START + index * 300_000).toISOString(),
  values: { cached: [8, 9, 7, 12, 14, 11, 9, 13, 15, 12, 10, 9][index]!, fresh: [1, 0, 2, 1, 3, 2, 1, 0, 2, 4, 1, 1][index]! },
}));

export function ChartsDemo() {
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card>
        <CardHeader title="CPU, the last hour" meta="the gap is a time the machine did not answer" />
        <div className="px-5 py-4">
          <TimeChart points={POINTS} label="CPU over the last hour" format={(value) => `${value}%`} bucketSeconds={150} max={100} bands={[{ from: 8, to: 9, label: 'Not answering' }]} />
        </div>
      </Card>
      <Card>
        <CardHeader title="Calls to a Site API" meta="the cache is the quiet majority" />
        <div className="px-5 py-4">
          <StackedBars
            buckets={BUCKETS}
            bucketSeconds={300}
            label="Calls in five-minute buckets"
            series={[
              { key: 'cached', label: 'From the cache', color: 'var(--chart-bar)' },
              { key: 'fresh', label: 'A fresh read', color: 'var(--chart-bar-strong)' },
            ]}
          />
        </div>
      </Card>
      <Card className="grid gap-2 p-5">
        <Example label="Sparkline: a row's last hour, in text colour">
          <Sparkline values={CPU.map((value) => value ?? 0)} className="h-8 w-40" />
        </Example>
      </Card>
      <Card className="board-surface registered grid min-h-32 content-start gap-2 p-5">
        <span className="silk">Heartbeat</span>
        <p className="text-secondary text-muted">The board: a drafting sheet for full-bleed instruments, silkscreen labels and one registered panel per screen.</p>
      </Card>
    </div>
  );
}

export function PicturesDemo() {
  const [finding, setFinding] = useState<string | undefined>('f1');
  return (
    <div data-studio="ignore" className="grid gap-4 lg:grid-cols-2">
      <ImageFrame
        src={shot('before')}
        alt="The product page at 360 px"
        width={360}
        height={480}
        active={finding}
        onSelect={(id) => setFinding((current) => (current === id ? undefined : id))}
        findings={[
          { id: 'f1', n: 1, tone: 'bad', label: 'The badge is 4.4:1, under 4.5:1', box: { x: 24, y: 300, w: 96, h: 28 } },
          { id: 'f2', n: 2, tone: 'warn', label: 'The size chips are 28 px tall', box: { x: 24, y: 360, w: 312, h: 40 } },
        ]}
      />
      <BeforeAfter
        width={360}
        height={480}
        before={{ src: shot('before'), alt: 'The product page today', label: 'Today' }}
        after={{ src: shot('after'), alt: 'The product page with version 5', label: 'Version 5' }}
      />
    </div>
  );
}
