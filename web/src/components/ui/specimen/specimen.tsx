/**
 * The specimen: every Mono primitive in every state, on one page, for builders and for studio.
 * Dev only (/$org/design, registered in app/router.tsx when import.meta.env.DEV). Nothing here is
 * real data: it is the components' own examples.
 */
import { Copy, Inbox, Play, Search } from 'lucide-react';
import { useState } from 'react';
import type { ReactNode } from 'react';
import type { Tone } from '@/lib/labels';
import { Badge, Chip, Count } from '../badge';
import { BeforeAfter } from '../before-after';
import { BranchPill } from '../branch';
import { Button, IconButton } from '../button';
import { Card, CardFooter, CardHeader } from '../card';
import { IfYes } from '../if-yes';
import { ImageFrame } from '../image-frame';
import { KINDS, KindEyebrow, KindTile } from '../kind';
import type { AnyKind, Kind } from '../kind';
import { Kbd, Meter, PageHeader } from '../misc';
import { Slider } from '../slider';
import { StatStrip, StatTile } from '../stat-tile';
import { Status } from '../status';
import { EmptyState, ErrorState, Skeleton } from '../states';
import { Swatch, TokenChip } from '../swatch';
import { Table, TableEmpty, TableSkeleton, Th } from '../table';
import { Tab, TabList, TabPanel, Tabs } from '../tabs';
import { Terminal } from '../terminal';
import { shot } from './shot';
import { FormsDemo, LOG, OverlaysDemo, ROWS, TableDemo } from './specimen-demos';

function Block({ id, title, note, children }: { id: string; title: string; note: string; children: ReactNode }) {
  return (
    <section id={id} aria-labelledby={`${id}-h`} className="grid scroll-mt-20 gap-4 border-t border-line pt-8">
      <div>
        <h2 id={`${id}-h`} className="text-section-title">
          {title}
        </h2>
        <p className="mt-1 max-w-[60em] text-secondary text-muted">{note}</p>
      </div>
      {children}
    </section>
  );
}

// Whole class names, so Tailwind finds them.
const ROLES = [
  ['display', 'text-display', 'Five jobs running on four machines.'],
  ['page-title', 'text-page-title', 'Runs'],
  ['page-title-compact', 'text-page-title-compact', 'Runs, on a phone'],
  ['section-title', 'text-section-title', 'Running now'],
  ['lede', 'text-lede', 'Tests, builds and dev servers your agents run on your machines.'],
  ['card-title', 'text-card-title', 'Cart totals round per line'],
  ['table-header', 'text-table-header', 'Who and where'],
  ['label', 'text-label', 'Run again'],
  ['body', 'text-body', 'Everything read: cells and paragraphs.'],
  ['secondary', 'text-secondary', 'A row’s second line, descriptions.'],
  ['caption', 'text-caption', 'Timestamps, footnotes, help under a field.'],
  ['number', 'text-number', '1,284 · 0:50 · 13.5 MB'],
  ['stat', 'text-stat', '94%'],
  ['code', 'text-code', 'pnpm vitest run'],
  ['code-caption', 'text-code-caption', 'dev/run-acme-web'],
  ['eyebrow', 'text-eyebrow', 'Tests'],
  ['micro', 'text-micro', 'CC'],
] as const;

const TONES: Array<[Tone, string, boolean?]> = [
  ['ok', 'Passed'],
  ['warn', 'Needs a look'],
  ['bad', 'Failed'],
  ['live', 'Running', true],
  ['idle', 'Waiting'],
  ['neutral', 'Queued'],
];

export function Specimen() {
  const [chip, setChip] = useState<'all' | Kind>('all');
  const [finding, setFinding] = useState<string | undefined>('f1');
  const [tab, setTab] = useState('output');
  const kinds: Kind[] = ['test', 'design', 'build', 'server'];
  const neutral: AnyKind[] = ['machine', 'push', 'post', 'payment', 'vm', 'lane', 'brand', 'email'];

  return (
    <div className="grid gap-10 pb-16">
      <PageHeader
        eyebrow={<KindEyebrow kind="design" label="Design system" />}
        title="Mono, every primitive"
        description="Every base component in every state, in the app’s own tokens. For builders and for studio: open it in both themes at 375, 768 and 1440."
        actions={<Button variant="primary" shortcut="N" icon={<Play className="size-4" />}>Primary</Button>}
      />

      <Block id="type" title="Type roles" note="Use the role, never a raw size. Geist for words, Geist Mono for numbers, ids, commands and branches. One caps style: the eyebrow.">
        <Card className="divide-y divide-line">
          {ROLES.map(([role, className, sample]) => (
            <div key={role} className="grid gap-1 px-5 py-3 md:grid-cols-[180px_minmax(0,1fr)] md:items-baseline">
              <TokenChip name={className} className="justify-self-start" />
              <span className={`${className} min-w-0`}>{sample}</span>
            </div>
          ))}
        </Card>
      </Block>

      <Block id="colour" title="Colour means something" note="Hues are kept for status and for the kind of work. What is selected, primary or running is ink. Focus is the one blue.">
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-6">
          {['ok', 'warn', 'bad'].map((tone) => (
            <Swatch key={tone} color={`var(--status-${tone}-dot)`} name={`status.${tone}`} value={`--status-${tone}-dot`} />
          ))}
          {kinds.map((kind) => (
            <Swatch key={kind} color={`var(--kind-${kind}-mark)`} name={`kind.${kind}`} value={`--kind-${kind}-mark`} />
          ))}
          <Swatch color="var(--focus-ring)" name="focus.ring" value="--focus-ring" tag="the one blue" />
          <Swatch color="var(--accent-fill)" name="accent.fill" value="--accent-fill" tag="ink" />
          <Swatch color="var(--terminal-bg)" name="terminal.bg" value="--terminal-bg" />
        </div>
        <div className="flex flex-wrap gap-2">
          <TokenChip name="--surface-page" color="var(--surface-page)" />
          <TokenChip name="--surface-card" color="var(--surface-card)" />
          <TokenChip name="--surface-inset" color="var(--surface-inset)" />
          <TokenChip name="--line-default" color="var(--line-default)" copy />
        </div>
      </Block>

      <Block id="buttons" title="Buttons" note="The primary is ink and there is one per screen. The secondary is the card with a firm hairline and the lift. Busy keeps its look and its focus.">
        <Card className="grid gap-4 p-5">
          {(['primary', 'secondary', 'ghost', 'danger'] as const).map((variant) => (
            <div key={variant} className="flex flex-wrap items-center gap-2">
              <span className="w-20 text-caption text-faint">{variant}</span>
              <Button variant={variant} size="sm">Small</Button>
              <Button variant={variant}>Default</Button>
              <Button variant={variant} size="lg">Large</Button>
              <Button variant={variant} loading>Working</Button>
              <Button variant={variant} disabled>Disabled</Button>
              <Button variant={variant} shortcut="A">With a key</Button>
            </div>
          ))}
          <div className="flex flex-wrap items-center gap-2">
            <span className="w-20 text-caption text-faint">icon</span>
            <IconButton label="Search">
              <Search className="size-4" />
            </IconButton>
            <IconButton label="Search" variant="secondary">
              <Search className="size-4" />
            </IconButton>
            <IconButton label="Search" size="sm" variant="secondary" disabled>
              <Search className="size-4" />
            </IconButton>
          </div>
          <div className="flex flex-wrap items-center gap-2 rounded-lg bg-inverse p-3">
            <Button variant="inverse" size="sm">Inverse on ink</Button>
            <IconButton label="Copy" variant="inverse" size="sm">
              <Copy className="size-4" />
            </IconButton>
          </div>
        </Card>
      </Block>

      <Block id="status" title="Status: a shape and a word" note="Dot fine, diamond needs a look, square failed, ringed and pulsing dot running, hollow ring idle. The word takes its colour only when it asks for attention.">
        <Card className="flex flex-wrap gap-x-6 gap-y-3 p-5">
          {TONES.map(([tone, label, pulse]) => (
            <Status key={tone} status={{ tone, label, pulse }} />
          ))}
        </Card>
        <Card className="flex flex-wrap items-center gap-2 p-5">
          <Badge>Neutral</Badge>
          <Badge tone="ok">Passed</Badge>
          <Badge tone="warn">Needs a look</Badge>
          <Badge tone="bad">2 failed</Badge>
          <Badge tone="live">Running</Badge>
          <Badge tone="accent">Selected</Badge>
          <Badge tone="bad" solid>Blocker</Badge>
          <Badge tone="warn" solid>Major</Badge>
          <Count value={6} />
          <Count value={6} tone="ink" />
          <Count value={6} tone="warn" />
          <Count value={3} tone="bad" />
          <Kbd>⌘</Kbd>
          <Kbd>K</Kbd>
        </Card>
      </Block>

      <Block id="kinds" title="The kind of work" note="Four kinds carry a hue: tests, design checks, builds and dev servers. The rest are neutral. A kind is a tile with its icon and a mono caps word, never a dot.">
        <Card className="grid gap-4 p-5">
          <div className="flex flex-wrap items-center gap-4">
            {kinds.map((kind) => (
              <KindEyebrow key={kind} kind={kind} />
            ))}
          </div>
          <div className="flex flex-wrap items-center gap-3">
            {kinds.map((kind) => (
              <span key={kind} className="flex items-center gap-1">
                <KindTile kind={kind} size="xs" />
                <KindTile kind={kind} size="sm" />
                <KindTile kind={kind} size="md" labelled />
                <KindTile kind={kind} size="lg" />
                <KindTile kind={kind} size="md" waiting />
              </span>
            ))}
          </div>
          <div className="flex flex-wrap items-center gap-3">
            {neutral.map((kind) => (
              <span key={kind} className="flex items-center gap-2 text-13 text-muted">
                <KindTile kind={kind} size="sm" />
                {KINDS[kind].word}
              </span>
            ))}
          </div>
          <div className="flex flex-wrap gap-1">
            <Chip label="All" count={5} pressed={chip === 'all'} onClick={() => setChip('all')} />
            {kinds.map((kind) => (
              <Chip key={kind} kind={kind} label={KINDS[kind].word} count={kind === 'test' ? 2 : 1} pressed={chip === kind} onClick={() => setChip(kind)} />
            ))}
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <BranchPill name="fix/cart-total-rounding" />
            <span className="text-faint" aria-hidden="true">→</span>
            <BranchPill name="main" main />
            <BranchPill name="feat/a-branch-name-long-enough-that-the-pill-cuts-it-and-keeps-a-title" />
            <BranchPill name="fix/cart-total-rounding" size="lg" />
          </div>
          <IfYes title="3 commits into main +33 −5">
            <BranchPill name="fix/cart-total-rounding" />
            <span className="text-faint">→</span>
            <BranchPill name="main" main />
            <span className="font-medium text-fg">3 commits</span>
            <span className="font-mono text-ok">+33</span>
            <span className="font-mono text-bad">−5</span>
          </IfYes>
        </Card>
      </Block>

      <Block id="cards" title="Cards, the lift and stat tiles" note="White on the off-white page, held by the hairline and a 1 px lift (a top light in dark). A job card takes its kind as a 2 px top edge.">
        <div className="grid gap-4 md:grid-cols-3">
          {(['test', 'design', 'build'] as const).map((kind, index) => (
            <Card key={kind} kind={kind} className="grid content-start gap-2 p-4">
              <div className="flex items-center justify-between gap-2">
                <KindEyebrow kind={kind} />
                {index === 0 ? <Badge tone="bad">2 failed</Badge> : index === 1 ? <Badge tone="warn">1 blocker</Badge> : <Status status={{ tone: 'live', label: 'Running', pulse: true }} />}
              </div>
              <h3 className="text-section-title">{['Cart totals round per line', 'Check the product page', 'Fresh install and build'][index]}</h3>
              <p title={['pnpm vitest run', 'ui_check /products/trail-runner-2', 'pnpm i && pnpm build'][index]} className="truncate font-mono text-code-caption text-faint">
                $ {['pnpm vitest run', 'ui_check /products/trail-runner-2', 'pnpm i && pnpm build'][index]}
              </p>
              <Meter value={[319, 4, 64][index]!} max={[440, 6, 100][index]!} label="Progress" />
            </Card>
          ))}
        </div>
        <Card>
          <CardHeader title="Needs you" meta="the oldest for 2 h" action={<Button variant="ghost" size="sm">See all 6</Button>} />
          <div className="px-5 py-4 text-14 text-muted">A card’s head: title, meta, action, one hairline. No grey band.</div>
          <CardFooter>Updated 14:09</CardFooter>
        </Card>
        <StatStrip>
          <StatTile label="Component uses" value="492" hint="on 9 checked pages" />
          <StatTile label="Look-alikes" value="30" tone="warn" hint="hand-rolled" />
          <StatTile label="On the system" value="94" unit="%" />
          <StatTile label="Changes waiting" value="1" />
        </StatStrip>
      </Block>

      <Block id="table" title="Tables" note="No band: the head is 13 px medium in the tertiary ink over one hairline. Rows 52 (40 compact). Numbers right, mono, tabular, units in the head, an en dash for none. Click a row to select it; Esc clears. Under 768 px it becomes a list.">
        <TableDemo />
        <div className="grid gap-4 lg:grid-cols-2">
          <Card>
            <CardHeader title="Loading" />
            <TableSkeleton columns={['Run', 'Result', 'Took']} rows={4} />
          </Card>
          <Card>
            <CardHeader title="Empty" />
            <Table>
              <thead>
                <tr>
                  <Th>Run</Th>
                  <Th>Result</Th>
                  <Th align="end">Took</Th>
                </tr>
              </thead>
              <tbody>
                <TableEmpty colSpan={3}>
                  <EmptyState icon={<Inbox />} title="No runs yet" description="Runs show here when an agent starts one with agentos run." action={<Button size="sm">Run a command</Button>} />
                </TableEmpty>
              </tbody>
            </Table>
          </Card>
        </div>
      </Block>

      <Block id="tabs" title="Tabs and a segmented control" note="Tabs move between parts of one thing; a segmented control picks a view of the same thing.">
        <Card className="p-5">
          <Tabs value={tab} onValueChange={setTab}>
            <TabList>
              <Tab value="output">Output</Tab>
              <Tab value="tests">Tests</Tab>
              <Tab value="files">Files changed</Tab>
            </TabList>
            <TabPanel value="output" className="pt-4">
              <Terminal label="Output of pnpm vitest run" command="pnpm vitest run" lines={LOG} highlight={{ 4: 'bad', 5: 'bad', 6: 'ok', 8: 'warn' }} follow />
            </TabPanel>
            <TabPanel value="tests" className="pt-4 text-14 text-muted">
              31 passed, 1 failed.
            </TabPanel>
            <TabPanel value="files" className="pt-4 text-14 text-muted">
              2 files changed.
            </TabPanel>
          </Tabs>
        </Card>
      </Block>

      <Block id="pictures" title="Pictures of pages" note="An image frame with numbered findings (pick a pin) and a zoom; before and after as a slider or side by side.">
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
              { id: 'f3', n: 3, tone: 'idle', label: 'A cut line without a title', box: { x: 4, y: 6, w: 200, h: 24 } },
            ]}
          />
          <BeforeAfter
            width={360}
            height={480}
            before={{ src: shot('before'), alt: 'The product page today', label: 'Today' }}
            after={{ src: shot('after'), alt: 'The product page with version 5', label: 'Version 5' }}
          />
        </div>
      </Block>

      <Block id="slider" title="Slider" note="Cards in a row with the next one peeking in. Arrow and page keys, the pager, a mouse drag and a swipe all move it.">
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
      </Block>

      <Block id="overlays" title="Dialog, drawer, side panel and tooltip" note="Floating layers take a shadow; the side panel sits on the page.">
        <OverlaysDemo />
      </Block>

      <Block id="forms" title="Fields" note="Inputs and selects have a firm hairline on the card; the focus ring sits on the border. Help is a caption; an error is red with its words.">
        <FormsDemo />
      </Block>

      <Block id="states" title="Loading, empty and error" note="A skeleton shaped like what comes; an empty state with an icon in a tile, one bold line, one sentence, one action; an error that says nothing changed.">
        <div className="grid gap-4 md:grid-cols-3">
          <Card className="grid content-start gap-3 p-5">
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
        </div>
      </Block>
    </div>
  );
}
