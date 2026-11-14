import { Copy, RefreshCw } from 'lucide-react';
import { useState } from 'react';
import type { StatusLabel } from '@/lib/labels';
import { BranchPill } from '../branch';
import { Button, IconButton } from '../button';
import { Card, CardHeader, FormCard } from '../card';
import { Dialog, Drawer, SidePanel } from '../dialog';
import { Field, Input } from '../field';
import { IfYes } from '../if-yes';
import { KindTile } from '../kind';
import type { Kind } from '../kind';
import { Tooltip } from '../menu';
import { Select } from '../select';
import { SelectionBar } from '../selection-bar';
import { Status } from '../status';
import { CellTitle, NoValue, RowActions, Table, TableScroll, Td, Th, Tr } from '../table';
import type { Density, SortDirection } from '../table';
import { SegmentedControl } from '../tabs';
import { Checkbox, Switch } from '../toggle';

interface Row {
  id: string;
  name: string;
  cmd: string;
  kind: Kind;
  branch: string;
  status: StatusLabel;
  tests: number | null;
  failed: number;
  minutes: number;
}

export const ROWS: Row[] = [
  { id: 'r1', name: 'Cart totals round per line', cmd: 'pnpm vitest run', kind: 'test', branch: 'fix/cart-total-rounding', status: { label: '2 failed', tone: 'bad' }, tests: 440, failed: 2, minutes: 0.8 },
  { id: 'r2', name: 'Fresh install and build', cmd: 'pnpm i && pnpm build', kind: 'build', branch: 'main', status: { label: 'Running', tone: 'live', pulse: true }, tests: null, failed: 0, minutes: 3.2 },
  { id: 'r3', name: 'Check the product page', cmd: 'ui_check /products/trail-runner-2', kind: 'design', branch: 'feat/product-badges-and-a-very-long-branch-name-that-cuts', status: { label: '1 blocker', tone: 'warn' }, tests: 6, failed: 0, minutes: 0.4 },
  { id: 'r4', name: 'Dev server for the shop', cmd: 'pnpm dev', kind: 'server', branch: 'dev/web', status: { label: 'Online', tone: 'ok' }, tests: null, failed: 0, minutes: 134 },
  { id: 'r5', name: 'Orders API', cmd: 'uv run pytest -q tests/api', kind: 'test', branch: 'main', status: { label: 'Waiting', tone: 'idle' }, tests: 0, failed: 0, minutes: 0 },
];

export const LOG = [
  '\u001b[2m14:09:02\u001b[0m  RUN  v3.2.4 /srv/acme-web',
  '\u001b[32m✓\u001b[0m src/checkout/address.test.ts (22 tests) 54ms',
  '\u001b[32m✓\u001b[0m src/checkout/shipping.test.ts (17 tests) 61ms',
  '\u001b[31m✗\u001b[0m src/cart/totals.test.ts > rounds each line \u001b[1mexpected 41.99\u001b[0m',
  '  \u001b[31m- received 42\u001b[0m',
  '  \u001b[32m+ expected 41.99\u001b[0m',
  '\u001b[32m✓\u001b[0m src/account/orders.test.ts (26 tests) 72ms',
  '\u001b[33m⚠\u001b[0m 1 snapshot obsolete in src/account/login.test.ts, and this line is long enough to be cut on a phone so its full text is a title',
  '\u001b[36minfo\u001b[0m  Test Files  \u001b[31m1 failed\u001b[0m | \u001b[32m31 passed\u001b[0m (32)',
];

export function TableDemo() {
  const [sort, setSort] = useState<{ key: 'name' | 'tests' | 'minutes'; dir: SortDirection }>({ key: 'minutes', dir: 'desc' });
  const [density, setDensity] = useState<Density>('default');
  const [selected, setSelected] = useState<Set<string>>(new Set(['r3']));
  const rows = [...ROWS].sort((a, b) => {
    const k = sort.key;
    const av = k === 'name' ? a.name : (a[k] ?? -1);
    const bv = k === 'name' ? b.name : (b[k] ?? -1);
    const order = av < bv ? -1 : av > bv ? 1 : 0;
    return sort.dir === 'asc' ? order : -order;
  });
  const toggle = (id: string) =>
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  return (
    <Card>
      <CardHeader
        title="Runs"
        meta="5 runs today"
        action={
          <SegmentedControl
            label="Density"
            value={density}
            onValueChange={setDensity}
            options={[
              { value: 'default', label: 'Comfortable' },
              { value: 'compact', label: 'Compact' },
            ]}
          />
        }
      />
      <SelectionBar
        count={selected.size}
        noun={['run', 'runs']}
        onClear={() => setSelected(new Set())}
        actions={
          <>
            <Button variant="inverse" size="sm" icon={<RefreshCw className="size-4" />}>
              Run again
            </Button>
            <Button variant="inverse" size="sm" icon={<Copy className="size-4" />}>
              Copy ids
            </Button>
          </>
        }
      />
      <TableScroll>
        <Table density={density} stack>
          <thead>
            <tr>
              <Th className="w-1">
                <span className="sr-only">Select</span>
              </Th>
              <Th sort={sort.key === 'name' ? sort.dir : undefined} onSort={(dir) => setSort({ key: 'name', dir })}>
                Run
              </Th>
              <Th>Branch</Th>
              <Th>Result</Th>
              <Th align="end" sort={sort.key === 'tests' ? sort.dir : undefined} onSort={(dir) => setSort({ key: 'tests', dir })}>
                Tests
              </Th>
              <Th align="end" unit="min" sort={sort.key === 'minutes' ? sort.dir : undefined} onSort={(dir) => setSort({ key: 'minutes', dir })}>
                Took
              </Th>
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

export function OverlaysDemo() {
  const [dialog, setDialog] = useState(false);
  const [drawer, setDrawer] = useState(false);
  const [panel, setPanel] = useState(false);
  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_320px]">
      <Card className="grid content-start gap-3 p-5">
        <div className="flex flex-wrap gap-2">
          <Button onClick={() => setDialog(true)}>Open a dialog</Button>
          <Button onClick={() => setDrawer(true)}>Open a drawer</Button>
          <Button onClick={() => setPanel((open) => !open)} aria-expanded={panel}>
            {panel ? 'Close the side panel' : 'Open the side panel'}
          </Button>
          <Tooltip label="The full text of a cut line, or what an icon does">
            <Button variant="ghost">Hover for a tooltip</Button>
          </Tooltip>
        </div>
        <p className="text-secondary text-muted">The side panel sits beside the list from 1024 px and opens as a drawer under it.</p>
      </Card>
      <SidePanel open={panel} onOpenChange={setPanel} title="hel-1" description="Hetzner, Helsinki · 4 cores">
        <p className="text-14 text-muted">The picked row’s detail, beside the list.</p>
      </SidePanel>
      <Dialog
        open={dialog}
        onOpenChange={setDialog}
        title="Rent a disposable VM?"
        description="It costs €0.0060 an hour and deletes itself by 16:21."
        footer={
          <>
            <Button onClick={() => setDialog(false)}>Cancel</Button>
            <Button variant="primary" onClick={() => setDialog(false)}>
              Rent it
            </Button>
          </>
        }
      >
        <IfYes>€0.02 at most, deletes by 16:21</IfYes>
      </Dialog>
      <Drawer open={drawer} onOpenChange={setDrawer} title="Run r_8f2a" description="Started 14:09 by Claude Code">
        <p className="text-14 text-muted">Inspect drawers hold the plumbing.</p>
      </Drawer>
    </div>
  );
}

export function FormsDemo() {
  const [on, setOn] = useState(true);
  const [check, setCheck] = useState(true);
  const [machine, setMachine] = useState('hel-1');
  return (
    <FormCard title="A form card" description="Fields, a select, a switch and a checkbox." footnote="Saved for everyone in Acme Shop." action={<Button variant="primary">Save</Button>}>
      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Branch" hint="The branch the run checks out.">
          <Input defaultValue="fix/cart-total-rounding" mono />
        </Field>
        <Field label="Command" error="A command cannot be empty.">
          <Input defaultValue="" placeholder="pnpm vitest run" />
        </Field>
        <Field label="Disabled">
          <Input defaultValue="Read only here" disabled />
        </Field>
        <div className="grid content-start gap-2">
          <span className="text-13 font-medium">Machine</span>
          <Select
            aria-label="Machine"
            value={machine}
            onValueChange={setMachine}
            options={[
              { value: 'hel-1', label: 'hel-1' },
              { value: 'office-nuc', label: 'office-nuc' },
              { value: 'this-laptop', label: 'this-laptop' },
            ]}
          />
        </div>
        <div className="flex items-center gap-3 text-14">
          <Switch checked={on} onCheckedChange={setOn} aria-label="Keep the dev server on" />
          <span aria-hidden="true">Keep the dev server on</span>
        </div>
        <div className="flex items-center gap-3 text-14">
          <Checkbox aria-label="Run the cart tests first" checked={check} onCheckedChange={(value) => setCheck(value === true)} />
          <span aria-hidden="true">Run the cart tests first</span>
        </div>
      </div>
    </FormCard>
  );
}
