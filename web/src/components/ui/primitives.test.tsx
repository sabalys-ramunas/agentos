/**
 * The Mono primitives: what each one promises (web/DESIGN.md). The look is checked in the browser on
 * the catalog (/$org/design); these hold the behaviour and the accessible names.
 */
import { fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { Badge, Chip, Count } from './badge';
import { BeforeAfter } from './before-after';
import { BranchPill } from './branch';
import { Button } from './button';
import { Card, CardHeader } from './card';
import { IfYes } from './if-yes';
import { ImageFrame } from './image-frame';
import { KindEyebrow, KindTile } from './kind';
import { Meter, PageHeader } from './misc';
import { SelectionBar } from './selection-bar';
import { Slider } from './slider';
import { StatTile } from './stat-tile';
import { Status, StatusDot } from './status';
import { Swatch, TokenChip } from './swatch';
import { NoValue, Table, TableEmpty, TableSkeleton, Td, Th, Tr } from './table';
import { SegmentedControl } from './tabs';
import { parseAnsi, Terminal } from './terminal';

describe('Table', () => {
  it('makes a sortable head a button, says the order with aria-sort, and asks for the next direction', async () => {
    const onSort = vi.fn();
    render(
      <Table>
        <thead>
          <tr>
            <Th sort="desc" onSort={onSort} align="end" unit="min">
              Took
            </Th>
            <Th onSort={onSort}>Run</Th>
            <Th>Branch</Th>
          </tr>
        </thead>
      </Table>,
    );
    const took = screen.getByRole('columnheader', { name: /Took/ });
    expect(took).toHaveAttribute('aria-sort', 'descending');
    expect(took).toHaveClass('text-right');
    expect(within(took).getByText('min')).toBeInTheDocument();
    await userEvent.click(within(took).getByRole('button'));
    expect(onSort).toHaveBeenLastCalledWith('asc');
    const run = screen.getByRole('columnheader', { name: 'Run' });
    expect(run).not.toHaveAttribute('aria-sort');
    await userEvent.click(within(run).getByRole('button'));
    expect(onSort).toHaveBeenLastCalledWith('desc');
    expect(within(screen.getByRole('columnheader', { name: 'Branch' })).queryByRole('button')).toBeNull();
  });

  it('sets density and the phone list form on the table, figures in tabular sans, money in mono, and a selected row', () => {
    const onActivate = vi.fn();
    render(
      <Table density="compact" stack>
        <tbody>
          <Tr selected onActivate={onActivate}>
            <Td main>Cart totals</Td>
            <Td numeric label="Tests">
              440
            </Td>
            <Td money label="Cost">
              €0.08
            </Td>
            <Td phone="hide">
              <NoValue />
            </Td>
          </Tr>
        </tbody>
      </Table>,
    );
    const table = screen.getByRole('table');
    expect(table).toHaveAttribute('data-density', 'compact');
    expect(table).toHaveAttribute('data-stack');
    const row = screen.getByRole('row');
    expect(row).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByText('440')).toHaveClass('tabular', 'text-right');
    expect(screen.getByText('440')).not.toHaveClass('font-mono');
    expect(screen.getByText('€0.08')).toHaveClass('font-mono', 'tabular', 'text-right');
    expect(screen.getByText('440')).toHaveAttribute('data-label', 'Tests');
    expect(screen.getByLabelText('None')).toHaveTextContent('–');
    fireEvent.keyDown(row, { key: 'Enter' });
    fireEvent.click(row);
    expect(onActivate).toHaveBeenCalledTimes(2);
  });

  it('folds a head away on a phone with the cells of its column', () => {
    render(
      <Table stack>
        <thead>
          <tr>
            <Th phone="hide">Age</Th>
          </tr>
        </thead>
      </Table>,
    );
    expect(screen.getByRole('columnheader', { name: 'Age' })).toHaveAttribute('data-phone', 'hide');
  });

  it('has a loading form shaped like the table and an empty row that spans it', () => {
    const { container } = render(<TableSkeleton columns={['Run', 'Result']} rows={3} />);
    expect(container.querySelectorAll('tbody tr')).toHaveLength(3);
    expect(container.querySelector('table')).toHaveAttribute('aria-hidden', 'true');
    render(
      <Table>
        <tbody>
          <TableEmpty colSpan={4}>No runs yet</TableEmpty>
        </tbody>
      </Table>,
    );
    expect(screen.getByText('No runs yet').closest('td')).toHaveAttribute('colspan', '4');
  });
});

describe('Status', () => {
  it.each([
    ['ok', 'ok'],
    ['warn', 'warn'],
    ['bad', 'bad'],
    ['live', 'live'],
    ['info', 'live'],
    ['idle', 'idle'],
    ['neutral', 'neutral'],
  ] as const)('draws %s as the %s shape beside its word', (tone, shape) => {
    const { container } = render(<Status status={{ tone, label: 'Word' }} />);
    expect(screen.getByText('Word')).toBeInTheDocument();
    const mark = container.querySelector('.ui-mark')!;
    expect(mark).toHaveAttribute('data-tone', shape);
    expect(mark).toHaveAttribute('aria-hidden', 'true');
  });

  it('never pulses, dashes the ring while starting, and colours the word only when it asks for attention', () => {
    const { container } = render(<StatusDot tone="live" pulse />);
    expect(container.firstChild).not.toHaveAttribute('data-pulse');
    expect(container.firstChild).not.toHaveAttribute('data-dashed');
    const starting = render(<StatusDot tone="live" starting />);
    expect(starting.container.firstChild).toHaveAttribute('data-dashed');
    render(<Status status={{ tone: 'bad', label: 'Failed' }} />);
    expect(screen.getByText('Failed')).toHaveClass('text-bad');
    render(<Status status={{ tone: 'ok', label: 'Passed' }} />);
    expect(screen.getByText('Passed')).toHaveClass('text-muted');
  });
});

describe('Badges, counts and chips', () => {
  it('tints a badge per tone and makes a chip a toggle', async () => {
    render(<Badge tone="bad">2 failed</Badge>);
    expect(screen.getByText('2 failed')).toHaveClass('bg-bad-fill', 'text-bad');
    render(<Count value={6} tone="warn" />);
    expect(screen.getByText('6')).toHaveClass('text-warn');
    const onClick = vi.fn();
    render(<Chip label="Tests" kind="test" count={2} pressed={false} onClick={onClick} />);
    const chip = screen.getByRole('button', { name: /Tests/ });
    expect(chip).toHaveAttribute('aria-pressed', 'false');
    expect(chip).toHaveAttribute('data-kind', 'test');
    await userEvent.click(chip);
    expect(onClick).toHaveBeenCalled();
  });
});

describe('Kinds', () => {
  it('gives the four kinds their hue and leaves the rest neutral', () => {
    const { container } = render(
      <>
        <KindTile kind="test" labelled />
        <KindTile kind="push" />
        <KindTile kind="build" waiting />
      </>,
    );
    expect(screen.getByRole('img', { name: 'Tests' })).toHaveAttribute('data-kind', 'test');
    const [, push, build] = container.querySelectorAll('.ui-kind-tile');
    expect(push).not.toHaveAttribute('data-kind');
    expect(push).toHaveAttribute('aria-hidden', 'true');
    expect(build).toHaveAttribute('data-waiting');
  });

  it('writes the eyebrow as the kind word, in mono caps', () => {
    render(<KindEyebrow kind="design" />);
    expect(screen.getByText('Design check')).toHaveClass('text-eyebrow', 'ui-kind-word');
  });

  it('puts a kind on a card as its top edge', () => {
    const { container } = render(<Card kind="server">Dev server</Card>);
    expect(container.firstChild).toHaveAttribute('data-kind', 'server');
    expect(container.firstChild).toHaveClass('ui-kind-edge', 'shadow-lift');
  });
});

describe('Branch, if yes, stat tile, swatch, meter and page header', () => {
  it('keeps a cut branch readable and marks the main branch', () => {
    render(<BranchPill name="main" main />);
    expect(screen.getByTitle('main').parentElement).toHaveClass('bg-fg');
  });

  it('says what happens on a yes after the mono IF YES', () => {
    render(<IfYes title="3 commits into main">3 commits into main</IfYes>);
    expect(screen.getByText('If yes')).toHaveClass('text-eyebrow');
    expect(screen.getByTitle('3 commits into main')).toBeInTheDocument();
  });

  it('shows a stat with its unit, and a swatch as data with a name', () => {
    render(<StatTile label="On the system" value="94" unit="%" tone="warn" />);
    expect(screen.getByText('94')).toHaveClass('text-stat', 'text-warn');
    render(<Swatch color="var(--kind-test-mark)" name="kind.test" value="--kind-test-mark" />);
    expect(screen.getByRole('img', { name: 'kind.test, --kind-test-mark' })).toHaveAttribute('data-studio', 'ignore');
    render(<TokenChip name="--line-default" copy />);
    expect(screen.getByRole('button', { name: 'Copy --line-default' })).toBeInTheDocument();
  });

  it('turns a meter amber and red only near the top', () => {
    render(<Meter value={96} max={100} label="Memory" />);
    expect(screen.getByRole('meter', { name: 'Memory' }).firstChild).toHaveClass('bg-bad-dot');
  });

  it('gives the page one h1 in the page title role, with an eyebrow and a lede', () => {
    render(<PageHeader title="Runs" eyebrow="TESTS" description="Today: 14 runs." actions={<Button>Run</Button>} />);
    expect(screen.getByRole('heading', { level: 1, name: 'Runs' })).toHaveClass('md:text-page-title');
    expect(screen.getByText('Today: 14 runs.')).toBeInTheDocument();
  });

  it('heads a card with a title, meta and action', () => {
    render(<CardHeader title="Needs you" meta="the oldest for 2 h" action={<Button>See all</Button>} />);
    expect(screen.getByRole('heading', { level: 2, name: 'Needs you' })).toHaveClass('text-card-title');
  });
});

describe('Buttons', () => {
  it('shows the key that does the same thing, and names it for assistive tech', () => {
    render(
      <Button variant="primary" shortcut="A">
        Approve
      </Button>,
    );
    expect(screen.getByRole('button', { name: /Approve/ })).toHaveAttribute('aria-keyshortcuts', 'A');
  });
});

describe('Terminal', () => {
  it('reads ANSI colours into segments and drops other escapes', () => {
    expect(parseAnsi('\u001b[32m✓\u001b[0m ok \u001b[1;31mfail\u001b[0m\u001b[2K')).toEqual([
      { text: '✓', color: 'green' },
      { text: ' ok ' },
      { text: 'fail', color: 'red', bold: true },
    ]);
    expect(parseAnsi('\u001b[2mdim\u001b[22m plain')).toEqual([{ text: 'dim', color: 'dim' }, { text: ' plain' }]);
  });

  it('renders output as text, numbers the lines, marks a highlighted one and toggles follow and wrap', async () => {
    render(<Terminal label="Output" command="pnpm test" lines={['<b>not html</b>', '\u001b[31mfailed\u001b[0m']} highlight={{ 2: 'bad' }} />);
    const log = screen.getByRole('log', { name: 'Output' });
    expect(within(log).getByText('<b>not html</b>')).toBeInTheDocument();
    expect(log.querySelector('b')).toBeNull();
    expect(within(log).getByText('failed').closest('[data-line]')).toHaveAttribute('data-highlight', 'bad');
    expect(within(log).getByText('failed')).toHaveClass('text-ansi-red');
    const follow = screen.getByRole('button', { name: 'Follow' });
    expect(follow).toHaveAttribute('aria-pressed', 'false');
    await userEvent.click(follow);
    expect(follow).toHaveAttribute('aria-pressed', 'true');
    await userEvent.click(screen.getByRole('button', { name: 'Wrap' }));
    expect(log).toHaveAttribute('data-wrap');
  });
});

describe('ImageFrame', () => {
  it('names each numbered finding and lets one be picked', async () => {
    const onSelect = vi.fn();
    render(
      <ImageFrame
        src="data:image/png;base64,"
        alt="The product page"
        width={360}
        height={480}
        active="a"
        onSelect={onSelect}
        findings={[
          { id: 'a', n: 1, tone: 'bad', label: 'Text under 4.5:1', box: { x: 20, y: 300, w: 96, h: 28 } },
          { id: 'b', n: 2, tone: 'warn', label: 'Small target', box: { x: 0, y: 0, w: 40, h: 40 } },
        ]}
      />,
    );
    const first = screen.getByRole('button', { name: 'Finding 1: Text under 4.5:1' });
    expect(first).toHaveAttribute('aria-pressed', 'true');
    const second = screen.getByRole('button', { name: 'Finding 2: Small target' });
    expect(second.parentElement).toHaveAttribute('data-inside');
    expect(second.parentElement).toHaveAttribute('data-dim');
    await userEvent.click(second);
    expect(onSelect).toHaveBeenCalledWith('b');
    expect(screen.getByRole('button', { name: 'Zoom in' })).toBeInTheDocument();
  });
});

describe('BeforeAfter', () => {
  it('wipes with a range input, and shows both side by side on request', async () => {
    render(
      <BeforeAfter
        width={360}
        height={480}
        before={{ src: 'data:,a', alt: 'Before', label: 'Today' }}
        after={{ src: 'data:,b', alt: 'After', label: 'Version 5' }}
      />,
    );
    const range = screen.getByRole('slider', { name: 'How much of Version 5 to show' });
    fireEvent.change(range, { target: { value: '80' } });
    expect(range).toHaveAttribute('aria-valuetext', '80% Version 5');
    await userEvent.click(screen.getByRole('radio', { name: 'Side by side' }));
    expect(screen.queryByRole('slider')).toBeNull();
    expect(screen.getAllByRole('figure')).toHaveLength(2);
  });
});

describe('SelectionBar', () => {
  function Demo() {
    const [count, setCount] = useState(2);
    return <SelectionBar count={count} noun={['run', 'runs']} actions={null} onClear={() => setCount(0)} />;
  }

  it('says how many are selected and clears on Esc', async () => {
    render(<Demo />);
    expect(screen.getByRole('toolbar', { name: 'Selection' })).toHaveTextContent('2 runs selected');
    await userEvent.keyboard('{Escape}');
    expect(screen.queryByRole('toolbar')).toBeNull();
  });
});

describe('SegmentedControl', () => {
  it('keeps one choice and moves it on click', async () => {
    const onValueChange = vi.fn();
    render(
      <SegmentedControl
        label="Density"
        value="default"
        onValueChange={onValueChange}
        options={[
          { value: 'default', label: 'Comfortable' },
          { value: 'compact', label: 'Compact' },
        ]}
      />,
    );
    expect(screen.getByLabelText('Density')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('radio', { name: 'Comfortable' }));
    expect(onValueChange).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole('radio', { name: 'Compact' }));
    expect(onValueChange).toHaveBeenCalledWith('compact');
  });
});

describe('Slider', () => {
  it('is a carousel of named slides that the keyboard can reach', () => {
    render(
      <Slider label="Running now" head={<h2>Running now</h2>}>
        <article>One</article>
        <article>Two</article>
        <article>Three</article>
        <article>Four</article>
      </Slider>,
    );
    const region = screen.getByRole('region', { name: 'Running now' });
    expect(region).toHaveAttribute('aria-roledescription', 'carousel');
    expect(screen.getByRole('group', { name: '2 of 4' })).toHaveTextContent('Two');
    // jsdom lays nothing out, so one card shows: the pager and the pages are there.
    expect(screen.getByRole('button', { name: 'Earlier' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Later' })).toBeEnabled();
    expect(screen.getByRole('button', { name: 'Page 1 of 4' })).toHaveAttribute('aria-current', 'true');
    const track = region.querySelector('.ui-slider-track')!;
    expect(track).toHaveAttribute('tabindex', '0');
    fireEvent.keyDown(track, { key: 'ArrowRight' });
  });
});
