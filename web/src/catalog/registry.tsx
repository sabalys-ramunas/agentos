/**
 * The catalog's list: every component file, in its group, with the example that shows it. The catalog
 * page (/$org/design, dev only) draws this list, and src/test/design-system.test.ts fails when a file
 * under src/components/ is missing from it or from its group's index (web/design-system/<group>.md).
 *
 * A file that draws nothing of its own (data, a hook, a part another component draws) names where it
 * is seen with `shownBy` instead of a demo.
 */
import type { ComponentType } from 'react';
import { ButtonsDemo, CopyDemo, FieldsDemo, TabsDemo } from './demos/actions';
import { CostLineDemo, FactsDemo, LiveViewDemo, ProofDemo, TerminalListDemo, TimelineDemo } from './demos/composites';
import { PillsDemo, SliderDemo, StatsDemo, TableDemo, TableStatesDemo, TerminalDemo } from './demos/data';
import { StatesDemo, StatusDemo } from './demos/feedback';
import { IconsDemo, KindsDemo, TokensDemo, TypeDemo } from './demos/foundations';
import { ChartsDemo, PicturesDemo } from './demos/instrument';
import { CardsDemo, PageHeaderDemo } from './demos/layout';
import { OverlaysDemo } from './demos/overlays';

export interface Entry {
  /** The file, from web/: src/components/ui/button.tsx. */
  file: string;
  /** What it exports that a screen uses. */
  names: string;
  demo?: ComponentType;
  /** Where it is seen when it has no demo of its own. */
  shownBy?: string;
}

export interface Group {
  id: 'foundations' | 'layout' | 'data' | 'feedback' | 'actions' | 'overlays' | 'instrument' | 'composites';
  title: string;
  /** One line: what the group is for. Its index (web/design-system/<id>.md) says when to use which. */
  purpose: string;
  entries: Entry[];
}

const ui = (name: string) => `src/components/ui/${name}`;
const shell = (name: string) => `src/components/shell/${name}`;
const THE_SHELL = 'the shell around this page';

export const GROUPS: Group[] = [
  {
    id: 'foundations',
    title: 'Foundations',
    purpose: 'The tokens every value comes from, the type roles, the kinds of work, icons and marks.',
    entries: [
      { file: 'src/styles/tokens.css', names: 'Colour, space, radius, depth, motion and layer tokens', demo: TokensDemo },
      { file: 'src/styles/theme.css', names: 'Type roles (text-body, text-page-title ...)', demo: TypeDemo },
      { file: ui('swatch.tsx'), names: 'Swatch, TokenChip', shownBy: 'the tokens above' },
      { file: ui('kind.tsx'), names: 'KindTile, KindEyebrow, KINDS', demo: KindsDemo },
      { file: 'src/components/lane-icon.tsx', names: 'Icons and marks: lucide, LaneIcon, and the marks below', demo: IconsDemo },
      { file: 'src/components/brand/identity.tsx', names: 'BusinessMark, PersonMark, AgentMark', shownBy: 'icons and marks above' },
      { file: 'src/components/brand/site-mark.tsx', names: 'SiteMark', shownBy: 'icons and marks above' },
      { file: 'src/components/brand/vendored.ts', names: 'Two marks Simple Icons dropped (data)', shownBy: 'SiteMark and AgentMark' },
    ],
  },
  {
    id: 'layout',
    title: 'Layout and shell',
    purpose: 'The frame a page sits in, its header, and the cards and sections it is made of.',
    entries: [
      { file: ui('misc.tsx'), names: 'PageHeader', demo: PageHeaderDemo },
      { file: ui('card.tsx'), names: 'Card, CardHeader, CardFooter, cardLink, Section, FormCard', demo: CardsDemo },
      { file: shell('technical-toggle.tsx'), names: 'TechnicalToggle', shownBy: 'the settings card above, and the rail' },
      { file: shell('app-shell.tsx'), names: 'AppShell', shownBy: THE_SHELL },
      { file: shell('rail.tsx'), names: 'Rail, the phone tab bar', shownBy: THE_SHELL },
      { file: shell('top-bar.tsx'), names: 'TopBar', shownBy: THE_SHELL },
      { file: shell('status-pill.tsx'), names: 'StatusPill, the status line', shownBy: 'the foot of the rail' },
      { file: shell('freeze-control.tsx'), names: 'FrozenChip, FreezeDialogs', shownBy: 'the top bar while a freeze holds' },
      { file: shell('command-palette.tsx'), names: 'CommandPalette', shownBy: 'Search or run (⌘ K)' },
      { file: shell('shortcuts.tsx'), names: 'The keyboard list', shownBy: 'the top bar (?)' },
      { file: shell('nav.ts'), names: 'The menu: five groups, each place with its g key', shownBy: 'the rail' },
      { file: shell('counts.ts'), names: 'The counts beside a place', shownBy: 'the rail' },
      { file: shell('crumb.ts'), names: 'usePageCrumb', shownBy: 'the top bar' },
      { file: shell('commands.ts'), names: 'What features add to the command bar', shownBy: 'Search or run (⌘ K)' },
      { file: shell('palette-store.ts'), names: 'The command bar open state', shownBy: 'Search or run (⌘ K)' },
    ],
  },
  {
    id: 'data',
    title: 'Data display',
    purpose: 'Tables, numbers, pills and details: the job itself, in the most information per glance.',
    entries: [
      { file: ui('table.tsx'), names: 'Table, TableScroll, Th, Tr, Td, CellTitle, NoValue, RowActions, TableSkeleton, TableEmpty', demo: TableDemo },
      { file: ui('selection-bar.tsx'), names: 'SelectionBar', shownBy: 'the table above (select a row)' },
      { file: ui('table.tsx'), names: 'TableSkeleton and TableEmpty: a table loading, and empty', demo: TableStatesDemo },
      { file: ui('misc.tsx'), names: 'Detail, Kbd, Avatar', shownBy: 'pills below' },
      { file: ui('stat-tile.tsx'), names: 'StatTile, StatStrip', demo: StatsDemo },
      { file: ui('badge.tsx'), names: 'Badge, Count, Chip', demo: PillsDemo },
      { file: ui('branch.tsx'), names: 'BranchPill', shownBy: 'pills above' },
      { file: ui('if-yes.tsx'), names: 'IfYes', shownBy: 'pills above' },
      { file: ui('terminal.tsx'), names: 'Terminal', demo: TerminalDemo },
      { file: ui('slider.tsx'), names: 'Slider', demo: SliderDemo },
    ],
  },
  {
    id: 'feedback',
    title: 'Status and feedback',
    purpose: 'What state a thing is in, and what a person sees while it loads, is empty, fails or is refused.',
    entries: [
      { file: ui('status.tsx'), names: 'Status, StatusDot', demo: StatusDemo },
      { file: ui('state-badge.tsx'), names: 'StateBadge, StateGlyph, ConditionBadge', shownBy: 'status above' },
      { file: ui('spinner.tsx'), names: 'Spinner', shownBy: 'status above' },
      { file: ui('misc.tsx'), names: 'Meter', shownBy: 'status above' },
      { file: ui('states.tsx'), names: 'Skeleton, EmptyState, ErrorState, InlineError, QueryView, NotLive, NoAccess', demo: StatesDemo },
      { file: 'src/components/not-found.tsx', names: 'NotFound', shownBy: 'the states above' },
      { file: 'src/components/plan-notice.tsx', names: 'PlanNotice, OverLimitBanner', shownBy: 'Settings, Members and Workspaces when the plan is full' },
      { file: 'src/components/install-prompt.tsx', names: 'InstallPrompt', shownBy: 'Needs you, after a decision, on a phone' },
    ],
  },
  {
    id: 'actions',
    title: 'Actions and forms',
    purpose: 'What a person presses and fills in: buttons, fields, switches, tabs, menus and commands to copy.',
    entries: [
      { file: ui('button.tsx'), names: 'Button, IconButton', demo: ButtonsDemo },
      { file: ui('field.tsx'), names: 'Field, Input, Textarea, Label', demo: FieldsDemo },
      { file: ui('select.tsx'), names: 'Select', shownBy: 'the fields above' },
      { file: ui('toggle.tsx'), names: 'Switch, Checkbox', shownBy: 'the fields above' },
      { file: ui('tabs.tsx'), names: 'Tabs, TabList, Tab, TabPanel, SegmentedControl', demo: TabsDemo },
      { file: ui('menu.tsx'), names: 'Menu, MenuItem, Popover, Tooltip, TooltipProvider', shownBy: 'tabs above, and overlays' },
      { file: ui('misc.tsx'), names: 'CodeBlock, CopyButton, useCopy', demo: CopyDemo },
      { file: 'src/components/charts/small.tsx', names: 'Segmented (old; use SegmentedControl)', shownBy: 'Machines, History and Settings, until they move' },
    ],
  },
  {
    id: 'overlays',
    title: 'Overlays',
    purpose: 'Layers over the page: a decision, the inspect drawer, a picked row beside its list, tips.',
    entries: [
      { file: ui('dialog.tsx'), names: 'Dialog, ConfirmDialog, Drawer, SidePanel, useReturnFocus', demo: OverlaysDemo },
      { file: shell('step-up-dialog.tsx'), names: 'StepUpDialog', shownBy: 'any action the node answers with step_up_required' },
    ],
  },
  {
    id: 'instrument',
    title: 'The instrument layer',
    purpose: 'Charts over time, bars, sparklines, the board and pictures of pages: looking into the machine room.',
    entries: [
      { file: 'src/components/charts/time-chart.tsx', names: 'TimeChart', demo: ChartsDemo },
      { file: 'src/components/charts/stacked-bars.tsx', names: 'StackedBars', shownBy: 'the charts above' },
      { file: 'src/components/charts/small.tsx', names: 'Sparkline', shownBy: 'the charts above' },
      { file: 'src/components/charts/shared.tsx', names: 'Legend, Readout, ChartTable, useWidth', shownBy: 'every chart above' },
      { file: ui('image-frame.tsx'), names: 'ImageFrame', demo: PicturesDemo },
      { file: ui('before-after.tsx'), names: 'BeforeAfter', shownBy: 'pictures above' },
    ],
  },
  {
    id: 'composites',
    title: 'agentos composites',
    purpose: 'The parts only agentos has, drawn from the mock: the live view, the terminal list, proofs, checkpoints, the cost line.',
    entries: [
      { file: 'src/features/environments/env-header.tsx', names: 'Facts (the stat strip of an environment)', demo: FactsDemo },
      { file: 'src/features/environments/env-cards.tsx', names: 'BrowserCard: the live view frame', demo: LiveViewDemo },
      { file: 'src/features/lane/control.tsx', names: 'ControlFrame: take over and hand back', shownBy: 'the live view above' },
      { file: 'src/features/environments/env-cards.tsx', names: 'CommandsCard: the terminal list', demo: TerminalListDemo },
      { file: 'src/features/environments/proofs.tsx', names: 'ProofsCard: the proof card', demo: ProofDemo },
      { file: 'src/features/environments/checkpoints.tsx', names: 'CheckpointsCard: a timeline of checkpoints', demo: TimelineDemo },
      { file: 'src/features/environments/env-cards.tsx', names: 'MachineCard: the cost line (price, while unused)', demo: CostLineDemo },
    ],
  },
];
