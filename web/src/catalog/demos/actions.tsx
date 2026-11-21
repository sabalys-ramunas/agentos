/** Actions and forms: buttons, fields, selects, switches, tabs, menus and copyable commands. */
import { Copy, MoreHorizontal, Play, Search } from 'lucide-react';
import { useState } from 'react';
import { Button, IconButton } from '@/components/ui/button';
import { Card } from '@/components/ui/card';
import { Field, Input, Textarea } from '@/components/ui/field';
import { Menu, MenuContent, MenuItem, MenuLabel, MenuSeparator, MenuTrigger } from '@/components/ui/menu';
import { CodeBlock, CopyButton } from '@/components/ui/misc';
import { Select } from '@/components/ui/select';
import { SegmentedControl, Tab, TabList, TabPanel, Tabs } from '@/components/ui/tabs';
import { Checkbox, Switch } from '@/components/ui/toggle';
import { Example, Row } from '../parts';

const VARIANTS = ['primary', 'secondary', 'ghost', 'danger'] as const;

export function ButtonsDemo() {
  return (
    <Card className="grid gap-4 p-5">
      {VARIANTS.map((variant) => (
        <Example key={variant} label={variant}>
          <Row>
            <Button variant={variant} size="sm">Small</Button>
            <Button variant={variant}>Default</Button>
            <Button variant={variant} size="lg">Large</Button>
            <Button variant={variant} loading>Working</Button>
            <Button variant={variant} disabled>Disabled</Button>
            <Button variant={variant} shortcut="A">With a key</Button>
          </Row>
        </Example>
      ))}
      <Example label="IconButton: an icon alone, and its label is required">
        <Row>
          <IconButton label="Search">
            <Search className="size-4" />
          </IconButton>
          <IconButton label="Search" variant="secondary">
            <Search className="size-4" />
          </IconButton>
          <IconButton label="Search" size="sm" variant="secondary" disabled>
            <Search className="size-4" />
          </IconButton>
        </Row>
      </Example>
      <Example label="inverse: on an ink bar (the selection bar)">
        <Row className="rounded-lg bg-inverse p-3">
          <Button variant="inverse" size="sm" icon={<Play className="size-4" />}>Run again</Button>
          <IconButton label="Copy" variant="inverse" size="sm">
            <Copy className="size-4" />
          </IconButton>
        </Row>
      </Example>
    </Card>
  );
}

export function FieldsDemo() {
  const [on, setOn] = useState(true);
  const [check, setCheck] = useState(true);
  const [machine, setMachine] = useState('hel-1');
  return (
    <Card className="grid gap-4 p-5 sm:grid-cols-2">
      <Field label="Branch" hint="The branch the run checks out.">
        <Input defaultValue="feat/black-friday-offer" mono />
      </Field>
      <Field label="Command" error="A command cannot be empty.">
        <Input defaultValue="" placeholder="pnpm vitest run" />
      </Field>
      <Field label="Disabled">
        <Input defaultValue="Read only here" disabled />
      </Field>
      <Field label="Why, for whoever reads it later">
        <Textarea defaultValue="The offer page goes live on Friday." />
      </Field>
      <div className="grid content-start gap-2">
        <span className="text-label">Machine</span>
        <Select
          aria-label="Machine"
          value={machine}
          onValueChange={setMachine}
          options={[
            { value: 'hel-1', label: 'hel-1' },
            { value: 'office-nuc', label: 'office-nuc' },
          ]}
        />
      </div>
      <div className="grid content-start gap-3">
        <span className="flex items-center gap-3 text-body">
          <Switch checked={on} onCheckedChange={setOn} aria-label="Keep the dev server on" />
          <span aria-hidden="true">Keep the dev server on</span>
        </span>
        <span className="flex items-center gap-3 text-body">
          <Checkbox aria-label="Run the cart tests first" checked={check} onCheckedChange={(value) => setCheck(value === true)} />
          <span aria-hidden="true">Run the cart tests first</span>
        </span>
      </div>
    </Card>
  );
}

export function TabsDemo() {
  const [tab, setTab] = useState('output');
  const [view, setView] = useState<'live' | 'replay'>('live');
  return (
    <Card className="grid gap-4 p-5">
      <Example label="Tabs move between parts of one thing">
        <Tabs value={tab} onValueChange={setTab}>
          <TabList>
            <Tab value="output">Output</Tab>
            <Tab value="tests">Tests</Tab>
            <Tab value="files">Files changed</Tab>
          </TabList>
          <TabPanel value="output" className="pt-3 text-body text-muted">31 passed, 1 failed.</TabPanel>
          <TabPanel value="tests" className="pt-3 text-body text-muted">32 tests in 4 files.</TabPanel>
          <TabPanel value="files" className="pt-3 text-body text-muted">2 files changed.</TabPanel>
        </Tabs>
      </Example>
      <Example label="SegmentedControl picks a view of the same thing">
        <SegmentedControl<'live' | 'replay'>
          label="Browser view"
          value={view}
          onValueChange={setView}
          options={[
            { value: 'live', label: 'Live' },
            { value: 'replay', label: 'Replay so far' },
          ]}
        />
      </Example>
      <Example label="Menu: actions on one thing">
        <Menu>
          <MenuTrigger asChild>
            <Button icon={<MoreHorizontal className="size-4" />}>More</Button>
          </MenuTrigger>
          <MenuContent>
            <MenuLabel>Black Friday offer page</MenuLabel>
            <MenuItem>Copy its id</MenuItem>
            <MenuSeparator />
            <MenuItem>Close it</MenuItem>
          </MenuContent>
        </Menu>
      </Example>
    </Card>
  );
}

export function CopyDemo() {
  return (
    <Card className="grid gap-4 p-5">
      <Example label="CodeBlock: a command to run, with its copy button. It wraps on a phone">
        <CodeBlock code="openajar environments setup --pull" label="Copy the command" />
      </Example>
      <Example label="CopyButton: beside an id">
        <Row>
          <span className="font-mono text-code-caption text-muted">env/e7k2-terminal</span>
          <CopyButton text="env/e7k2-terminal" label="Copy the terminal id" />
        </Row>
      </Example>
    </Card>
  );
}
