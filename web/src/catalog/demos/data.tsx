/** Data display: tables, stat tiles, badges and pills, details, the terminal and the slider. */
import { Copy, Inbox, RefreshCw } from 'lucide-react';
import { useState } from 'react';
import { Badge, Chip, Count } from '@/components/ui/badge';
import { BranchPill } from '@/components/ui/branch';
import { Button, IconButton } from '@/components/ui/button';
import { Card, CardHeader } from '@/components/ui/card';
import { IfYes } from '@/components/ui/if-yes';
import { KINDS, KindEyebrow, KindTile } from '@/components/ui/kind';
import type { Kind } from '@/components/ui/kind';
import { Avatar, Detail, Kbd } from '@/components/ui/misc';
import { SelectionBar } from '@/components/ui/selection-bar';
import { Slider } from '@/components/ui/slider';
import { StatStrip, StatTile } from '@/components/ui/stat-tile';
import { EmptyState } from '@/components/ui/states';
import { Status } from '@/components/ui/status';
import { CellTitle, NoValue, RowActions, Table, TableEmpty, TableScroll, TableSkeleton, Td, Th, Tr } from '@/components/ui/table';
import type { Density, SortDirection } from '@/components/ui/table';
import { SegmentedControl } from '@/components/ui/tabs';
import { Terminal } from '@/components/ui/terminal';
import { Checkbox } from '@/components/ui/toggle';
import { Example, Row } from '../parts';
import { LOG, ROWS } from '../sample';

type SortKey = 'name' | 'tests' | 'minutes';

function useRows() {
  const [sort, setSort] = useState<{ key: SortKey; dir: SortDirection }>({ key: 'minutes', dir: 'desc' });
  const rows = [...ROWS].sort((a, b) => {
    const av = sort.key === 'name' ? a.name : (a[sort.key] ?? -1);
    const bv = sort.key === 'name' ? b.name : (b[sort.key] ?? -1);
    const order = av < bv ? -1 : av > bv ? 1 : 0;
    return sort.dir === 'asc' ? order : -order;
  });
  const sortOf = (key: SortKey) => ({ sort: sort.key === key ? sort.dir : undefined, onSort: (dir: SortDirection) => setSort({ key, dir }) });
  return { rows, sortOf };
}

export function TableDemo() {
  const { rows, sortOf } = useRows();
  const [density, setDensity] = useState<Density>('default');
  const [selected, setSelected] = useState<Set<string>>(new Set(['r3']));
  const toggle = (id: string) =>
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const options = [
    { value: 'default' as const, label: 'Comfortable' },
    { value: 'compact' as const, label: 'Compact' },
  ];
  return (
    <Card>
      <CardHeader title="Runs" meta="5 runs today" action={<SegmentedControl label="Density" value={density} onValueChange={setDensity} options={options} />} />
      <SelectionBar
        count={selected.size}
        noun={['run', 'runs']}
        onClear={() => setSelected(new Set())}
        actions={
          <>
            <Button variant="inverse" size="sm" icon={<RefreshCw className="size-4" />}>Run again</Button>
            <Button variant="inverse" size="sm" icon={<Copy className="size-4" />}>Copy ids</Button>
          </>
        }
      />
      <TableScroll>
        <Table density={density} stack aria-label="Runs">
          <thead>
            <tr>
              <Th className="w-1">
                <span className="sr-only">Select</span>
              </Th>
              <Th {...sortOf('name')}>Run</Th>
              <Th>Branch</Th>
              <Th>Result</Th>
              <Th align="end" {...sortOf('tests')}>Tests</Th>
              <Th align="end" unit="min" {...sortOf('minutes')}>Took</Th>
              <Th>
                <span className="sr-only">Actions</span>
              </Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <Tr key={row.id} selected={selected.has(row.id)} onActivate={() => toggle(row.id)}>
                <Td phone="hide" onClick={(event) => event.stopPropagation()}>
                  <Checkbox aria-label={`Select ${row.name}`} checked={selected.has(row.id)} onCheckedChange={() => toggle(row.id)} />
                </Td>
                <Td main>
                  <div className="flex min-w-0 items-center gap-3">
                    <KindTile kind={row.kind} size="sm" waiting={row.status.tone === 'idle'} />
                    <CellTitle title={row.name} second={row.cmd} />
                  </div>
                </Td>
                <Td phone="hide">
                  <BranchPill name={row.branch} main={row.branch === 'main'} />
                </Td>
                <Td>
                  <Status status={row.status} />
                </Td>
                <Td numeric label="Tests">
                  {row.tests === null ? <NoValue /> : row.tests === 0 ? <span className="text-faint">0</span> : row.tests}
                </Td>
                <Td numeric label="Took">
                  {row.minutes.toFixed(1)}
                </Td>
                <Td className="w-1 text-right">
                  <RowActions>
                    <IconButton label={`Run ${row.name} again`} size="sm" onClick={(event) => event.stopPropagation()}>
                      <RefreshCw className="size-4" />
                    </IconButton>
                  </RowActions>
                </Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      </TableScroll>
    </Card>
  );
}

export function TableStatesDemo() {
  return (
    <div className="grid gap-4 lg:grid-cols-2">
      <Card>
        <CardHeader title="Loading" />
        <TableSkeleton columns={['Run', 'Result', 'Took']} rows={4} />
      </Card>
      <Card>
        <CardHeader title="Empty" />
        <Table aria-label="No runs">
          <thead>
            <tr>
              <Th>Run</Th>
              <Th>Result</Th>
              <Th align="end">Took</Th>
            </tr>
          </thead>
          <tbody>
            <TableEmpty colSpan={3}>
              <EmptyState icon={<Inbox />} title="No runs yet" description="Runs show here when an agent starts one with openajar run." action={<Button size="sm">Run a command</Button>} />
            </TableEmpty>
          </tbody>
        </Table>
      </Card>
    </div>
  );
}

export function StatsDemo() {
  return (
    <StatStrip>
      <StatTile label="Open now" value="3" hint="on 2 machines" />
      <StatTile label="Failed today" value="1" tone="bad" hint="Search results pagination" />
      <StatTile label="Time left" value="1" unit="h" hint="then it closes by itself" />
      <StatTile label="This month" value="€4.20" hint="2 machines kept warm" />
    </StatStrip>
  );
}

const WORK: Kind[] = ['test', 'design', 'build', 'server'];

export function PillsDemo() {
  const [chip, setChip] = useState<'all' | Kind>('all');
  return (
    <Card className="grid gap-4 p-5">
      <Example label="Badge (a tint per tone, or solid), Count, Kbd">
        <Row>
          <Badge>Neutral</Badge>
          <Badge tone="ok">Passed</Badge>
          <Badge tone="warn">Needs a look</Badge>
          <Badge tone="bad">2 failed</Badge>
          <Badge tone="live">Running</Badge>
          <Badge tone="accent">Selected</Badge>
          <Badge tone="bad" solid>Blocker</Badge>
          <Count value={6} />
          <Count value={6} tone="ink" />
          <Count value={3} tone="bad" />
          <Kbd>⌘</Kbd>
          <Kbd>K</Kbd>
        </Row>
      </Example>
      <Example label="Chip: a filter with aria-pressed; with a kind its square is the legend">
        <Row className="gap-1">
          <Chip label="All" count={5} pressed={chip === 'all'} onClick={() => setChip('all')} />
          {WORK.map((kind) => (
            <Chip key={kind} kind={kind} label={KINDS[kind].word} count={kind === 'test' ? 2 : 1} pressed={chip === kind} onClick={() => setChip(kind)} />
          ))}
        </Row>
      </Example>
      <Example label="BranchPill and IfYes">
        <Row>
          <BranchPill name="feat/black-friday-offer" />
          <span className="text-faint" aria-hidden="true">→</span>
          <BranchPill name="main" main />
          <BranchPill name="feat/a-branch-name-long-enough-that-the-pill-cuts-it-and-keeps-a-title" />
        </Row>
        <IfYes title="3 commits into main +33 −5">
          <BranchPill name="feat/black-friday-offer" />
          <span className="text-faint">→</span>
          <BranchPill name="main" main />
          <span className="font-medium text-fg">3 commits</span>
          <span className="font-mono text-ok">+33</span>
          <span className="font-mono text-bad">−5</span>
        </IfYes>
      </Example>
      <Example label="Detail rows and Avatar">
        <dl className="grid max-w-md divide-y divide-line">
          <Detail label="Provider">Hetzner Cloud</Detail>
          <Detail label="Type">
            <span className="font-mono text-code-caption">cpx31</span>
          </Detail>
          <Detail label="Started by">
            <span className="inline-flex items-center gap-2">
              <Avatar name="Priya Shah" />
              Priya Shah
            </span>
          </Detail>
        </dl>
      </Example>
    </Card>
  );
}

export function TerminalDemo() {
  return <Terminal label="Output of pnpm vitest run" command="pnpm vitest run" lines={LOG} highlight={{ 4: 'bad', 5: 'bad', 6: 'ok', 8: 'warn' }} follow />;
}

export function SliderDemo() {
  return (
    <Slider
      label="Running now"
      head={
        <>
          <h3 className="text-card-title">Running now</h3>
          <span className="text-secondary text-faint">5 jobs on 4 machines</span>
        </>
      }
    >
      {ROWS.map((row) => (
        <Card key={row.id} kind={row.kind} className="grid content-start gap-2 p-4">
          <KindEyebrow kind={row.kind} />
          <h4 className="line-clamp-2 text-section-title">{row.name}</h4>
          <p className="truncate font-mono text-code-caption text-faint">$ {row.cmd}</p>
          <Status status={row.status} />
        </Card>
      ))}
    </Slider>
  );
}
