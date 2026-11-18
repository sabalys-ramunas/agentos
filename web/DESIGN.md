# agentos design system: Mono

The app follows **Mono**, the style the owner approved on 2026-11-13: precise and monochrome, in
the spirit of vercel.com and resend.com, with a colour for each kind of work. The prototype was
`panel/prototypes/dashboard/style-mono/` (removed with the classic panel; it is in git history at
59634500), its reasons are in [`docs/design/style-style-mono.md`](../docs/design/style-style-mono.md), the UX in
[`docs/design/app-design.md`](../docs/design/app-design.md) and the tables, type roles and statuses in
[`docs/design/foundations.md`](../docs/design/foundations.md). Where those and this file disagree about
the app, this file is the rule. If you change a rule, change it here in the same commit.

The product is agentos's own: do not copy Vercel's or Resend's logo, wording or layouts.

## What changed with Mono

Said plainly, for anyone who knew the app before:

- **One token source.** [`tokens/build-tokens.py`](tokens/build-tokens.py) writes
  [`tokens/tokens.json`](tokens/tokens.json) (DTCG) and [`src/styles/tokens.css`](src/styles/tokens.css).
  Both are generated: edit the script and run it. `light-dark()` is gone from the token set; each theme
  is a block of plain values, so studio can read every token.
- **Page and card differ.** The page is `#fafafa` (black in dark), a card white (`#0a0a0a`). It was one
  colour.
- **Cards lift by 1 px.** A 1 px shadow in light, a 1 px light on the top edge in dark (`--lift`,
  `shadow-lift`). It was "nothing in the page casts a shadow".
- **No hue accent.** The primary, what is selected and what is running are ink. Links are ink with an
  underline. Focus is the one blue. `text-blue` and `--blue` now give ink.
- **Running is ink, not pink.** `live`, `--current` and the Working now glyph were pink. They are ink
  with a ringed, pulsing dot, so no status shares a hue with a kind of work. Changed in one place
  (`src/styles/aliases.css`); every screen that says `bg-live` or `var(--current)` follows.
- **A colour per kind of work.** Tests violet, design checks pink, build and CI teal, dev servers blue:
  always a tile with the kind's icon and its word in mono caps (`KindTile`, `KindEyebrow`).
- **Type roles.** `text-page-title` (28/36), `text-display` (40/44), `text-table-header`, `text-code`
  and the rest. The old `text-11` to `text-32` still work.
- **Tables** have no grey band, rows 52 px (40 compact), sortable heads with `aria-sort`, a list form
  under 768 px, and a selection bar.
- **Terminals are dark in both themes.** **Radius xl is 10** (was 12). Durations 100 / 150 / 200 ms.
- **One caps style:** the mono eyebrow, for a kind's word and "IF YES". Sentence case everywhere else.
- **The shell.** The rail runs full height on the left with the menu in five groups; the top bar sits
  over the page only, and Computers is called Machines everywhere a person reads it. See "The shell".

## Tokens

[`tokens/build-tokens.py`](tokens/build-tokens.py) is the one place the values live. It writes
`tokens/tokens.json` and `src/styles/tokens.css`, and from [`tokens/theme.py`](tokens/theme.py) the Tailwind
theme (`src/styles/theme.css`) and its keys as data (`tokens/theme-keys.json`, which the design rules read).
It checks 112 text and mark pairs against WCAG AA in both
themes, and checks that the kind colours stay apart from each other and from status for protanopia,
deuteranopia and tritanopia (OKLab distance 0.04 or more). `src/styles/tokens.test.ts` fails when the
two files drift from each other or from the script, or a check fails:

```bash
python3 tokens/build-tokens.py            # write both files and check
python3 tokens/build-tokens.py --check    # what the test runs: write nothing, fail on drift
```

Light is the default. Dark comes from the system (`prefers-color-scheme`) unless `data-theme` on
`<html>` pins one, so "system" still needs no JavaScript.

Three files, in this order, from `src/styles/app.css`:

0. `theme.css`: the Tailwind theme, every class name onto a token (generated from `tokens/theme.py`).
1. `tokens.css`: the Mono set (generated). Roles, never steps: `--surface-*`, `--line-*`, `--text-*`,
   `--status-*`, `--kind-*`, `--terminal-*`, `--ansi-*`, `--diff-*`, `--chart-*`, `--type-*`.
2. `aliases.css`: the names the screens used before Mono (`--background`, `--foreground-muted`,
   `--border`, `--ok-dot`, `--current` ...), each a reference to a token. It writes no colour of its own
   (a test holds it): the board's wires, Site API fields, the night band and the layout sizes are tokens
   now. New code uses Mono names; an alias goes when nothing uses it.
3. `primitives.css`: the rules the primitives need real selectors for (table, status shapes, kinds,
   terminal, image frame, slider). Screens never write these classes.

`theme.css` (generated) maps tokens to Tailwind and **clears Tailwind's default palette, sizes, radii and
shadows**. A class like `bg-red-500` or `text-sm` does not exist, and the design rules fail on one. Both sets
of names work, and the rules let the old ones only shrink:

| Use | Mono class | Old class (still works) | Token |
|---|---|---|---|
| The page | `bg-page` | (body) | `--surface-page` |
| Cards, tables, panels | `bg-card` | `bg-bg` | `--surface-card` |
| Wells: code, card footers, pictures loading. Never a table head | `bg-inset` | `bg-subtle` | `--surface-inset` |
| Menus, popovers, dialogs | `bg-raised` | `bg-raised` | `--surface-raised` |
| Tooltips, toasts, the selection bar | `bg-inverse`, `text-on-inverse` | | `--surface-inverse` |
| Hover, pressed | `bg-hover`, `bg-pressed` | `bg-fill-1`, `bg-fill-2` | `--fill-hover`, `--fill-pressed` |
| Hairline; firm edge; control edge (3:1) | `border-line`, `border-line-strong`, `border-control` | same | `--line-*` |
| Text: primary, secondary, tertiary, disabled | `text-fg`, `text-muted`, `text-faint`, `text-disabled` | same | `--text-*` |
| The primary, selected, checked: ink | `bg-primary`, `text-on-primary`, `bg-accent-soft` | same | `--accent-*` |
| Focus: the one blue | (the ring) | | `--focus-ring` |
| Status words | `text-ok`, `text-warn`, `text-bad`, `text-idle` | `text-green`, `text-amber`, `text-red` | `--status-*-text` |
| Status marks, tints, edges | `bg-ok-dot`, `bg-ok-fill`, `border-ok-line` (and warn, bad, live, idle) | `--ok-dot`, `--ok-soft` ... | `--status-*` |
| Running | `text-live`, `bg-live-dot`, `bg-live-soft` | `bg-live`, `--current` | `--status-live-*` (ink) |
| A kind's word, mark, tile, edge | `text-kind-test`, `bg-kind-test-mark`, `bg-kind-test-soft`, `border-kind-test-line` (and design, build, server) | | `--kind-*` |
| Terminal | `bg-terminal`, `text-terminal-text`, `text-ansi-red` ... | | `--terminal-*`, `--ansi-*` |
| Diff lines | `bg-diff-add`, `text-diff-add-mark` ... | | `--diff-*` |
| Charts | `bg-chart-bar`, `bg-chart-bar-strong`, `bg-chart-grid` | | `--chart-*` |
| People and business marks (fill only) | `bg-identity-1` to `-8` | `--ws-1` to `--ws-8` | `--identity-*` |

Every text colour passes AA on the page, the card, the well and the raised surface; the pairs are in
the script's output. `text-disabled` is exempt, as disabled controls are, so never use it for something
meant to be read. On a selected row's tint, `text-faint` steps up to `text-muted` (the table does it).

### Type

Geist (`'Geist Variable'`) for words, Geist Mono (`'Geist Mono Variable'`) for numbers, ids, hashes,
commands and branches, with tabular figures. Use a **role**, which sets size, line height, weight and
tracking in one class; never pick a raw size for new work.

| Role | Size / line, weight, tracking | Use |
|---|---|---|
| `text-display` | 40/44, 600, -0.045em | Home's sentence. One per app |
| `text-page-title` | 28/36, 600, -0.035em | The h1 (`PageHeader` does it) |
| `text-page-title-compact` | 24/32, 600 | The h1 on a phone |
| `text-section-title` | 16/24, 600 | A section, a dialog title, a job card's title |
| `text-lede` | 16/24 | The sentence under a page title, in `text-muted` |
| `text-card-title` | 14/20, 600 | A card's head |
| `text-table-header` | 13/20, 500 | Column heads, in `text-faint` (the table does it) |
| `text-label` | 14/20, 500 | Buttons, tabs, form labels |
| `text-body` | 14/20 | Everything read |
| `text-secondary` | 13/20 | A row's second line, descriptions |
| `text-caption` | 12/16 | Timestamps, help under a field |
| `text-number` | mono 13/20 | Numbers in tables and lists |
| `text-stat` | 28/32, 600 | A stat tile's number |
| `text-code` | mono 13/20 | Ids, commands, terminal output |
| `text-code-caption` | mono 12/16 | A command or branch on a second line |
| `text-eyebrow` | mono 11/16, 500, +0.04em, caps | A kind's word and "IF YES". The one caps style |
| `text-micro` | 11/16, 500 | Avatar initials and keys only |

The old scale (`text-11` to `text-32`) still maps onto the same sizes for existing screens. Weights:
400, 500, 600. Nothing heavier.

### Space, radius, depth, motion

- **Spacing:** a strict 4 px grid. Half steps such as `gap-1.5` are banned and a test fails on them.
- **Radius, one per job:** 4 (`rounded-sm`) marks and checkboxes, 6 (`rounded-md`) buttons and inputs,
  8 (`rounded-lg`) pictures and previews, 10 (`rounded-xl`) cards and dialogs, full for status pills,
  counts and branches.
- **Depth:** hairlines, plus the card lift (`shadow-lift`: a 1 px shadow in light, a 1 px top light in
  dark) on cards, secondary buttons and tiles. `shadow-popover` and `shadow-dialog` for floating layers.
  Nothing else casts a shadow, and selected edges, tab underlines and rings are borders or outlines,
  never box-shadow lines, so studio can see them.
- **Motion:** 100 ms for hover, 150 ms for most things, 200 ms for drawers, on `ease-standard`;
  `ease-enter` for what arrives. Opacity and 2 px moves only. The live dot pulses; under reduced motion it
  is a still ring and nothing moves.
- **Icons:** `lucide-react`, 16 px, stroke 1.5 (set once in CSS).

## Kinds of work, and how they stay apart from status

| Kind | Mark (light / dark) | Icon | `kind` |
|---|---|---|---|
| Tests (`agentos run` suites) | violet `#9a5bd6` / `#b18cfa` | flask | `test` |
| Design (ui_check, ui_diff, a brand change) | pink `#c8337f` / `#f06bb0` | the check frame | `design` |
| Build and CI | teal `#0b6e88` / `#3fc6dc` | box | `build` |
| Dev server | blue `#1e40af` / `#4c7dff` | radio | `server` |
| Machine, push, post, payment, VM, lane, brand, email | none: a neutral tile | its own | `machine` ... |

A kind never reads as a status: different hues (status keeps green, amber, red, and ink for running),
different shapes (a kind is always a rounded tile with an icon; a status is always a mark and a word),
and different places (on a card the kind sits left and on the 2 px top edge, the status sits right as a
pill). There is no fifth hue: a new kind shares its family's hue and leans on its icon.

## Components

Primitives live in `src/components/ui`. Use them; do not restyle them in a screen. Every one is in the
catalog, **`/$org/design`** (dev only, `src/catalog/`), in every state and by group; open it in both themes
at 390 and 1440 before you change one. How to build with them, the groups and the checks:
[DESIGN-SYSTEM.md](DESIGN-SYSTEM.md).

- **Button:** `primary` is ink, once per view, for the point of a screen (an empty state's next action,
  a dialog's confirm, Approve). `secondary` is the card with a firm hairline and the lift, and is the
  default. `ghost` for quiet actions in rows. `danger` is a red word on a hairline, never a red fill.
  `inverse` sits on an ink bar. `sm` 28 (32 on a phone), `md` 32, `lg` 40. `shortcut="A"` shows the key
  and sets `aria-keyshortcuts`. Pass `loading` rather than disabling by hand. An icon alone uses
  `IconButton`, whose `label` is required.
- **Status:** `<Status status={...} />`, a shape and a word. Dot fine, diamond needs a look, square
  failed, ringed dot running (pulsing with `pulse`), hollow ring idle, quiet solid dot neutral. The word
  takes its colour only when it asks for attention (failed, needs a look, running). Tones are
  `ok | warn | bad | live | idle | neutral` (`info` is the old name for live). Every enum maps to words
  and a tone in `src/lib/labels.ts`; add new ones there, never inline.
- **Badge** (a 20 px pill, a soft tint per tone, or `solid`), **Count** (quiet, ink, warn, bad) and
  **Chip** (a filter pill with `aria-pressed`; with `kind` its square doubles as the legend).
- **KindTile, KindEyebrow:** the kind's tile (`xs` 18, `sm` 24, `md` 28, `lg` 32; `waiting` is dashed)
  and its word in mono caps. `Card kind="test"` adds the 2 px top edge.
- **BranchPill:** a mono pill, `main` in ink, full name as a title. **IfYes:** "IF YES" and one line of
  what a yes does, cut with its full text as a title.
- **Card, CardHeader, CardFooter:** radius 10, the lift, a head of 52 px with the title, quiet meta and
  the action, one hairline, no band. **FormCard** for settings. **Section** for a titled block.
- **StatTile, StatStrip:** the stat role in tabular figures, its unit in the tertiary ink; a strip of
  tiles in one card divided by hairlines.
- **Tables** (the showpiece, below).
- **Tabs** (words on a hairline, the selected one ink with a 2 px edge) and **SegmentedControl** (a view
  of the same thing: the chosen segment is the card on a quiet well; arrow keys move).
- **Dialog** (centred, a bottom sheet on a phone), **Drawer** (the inspect surface), **SidePanel** (a
  picked row's detail beside the list from 1024 px, a Drawer under it), **ConfirmDialog** (with
  `typeToConfirm` when it cannot be undone). The close button is last in the DOM so the first field
  takes focus; focus returns to whatever opened it (`useReturnFocus`).
- **Tooltip:** the inverse surface, caption size. Every cut line also carries its full text as a title.
- **Skeleton, EmptyState, ErrorState, QueryView:** an empty state is an icon in a small bordered tile,
  one bold line, one sentence, one action. An error says nothing changed and offers Try again.
- **PageHeader:** the page title role (24/32 on a phone), an optional `eyebrow`, the lede, actions on the
  right on the first row at every width.
- **Field, Input, Select, Switch, Checkbox:** a firm hairline on the card; the focus ring sits on the
  border; help is a caption; an error is red words. Checked is ink.
- **Kbd** (mono micro on the card), **Meter** (4 px, ink on the chart track, amber at 80%, red at 95%),
  **CodeBlock** (a well with a copy button).
- **Terminal:** read-only output, dark in both themes. ANSI colours are parsed into text spans (never
  HTML). Line numbers, `highlight={{ 4: 'bad' }}`, follow mode (keeps the newest line in view until you
  scroll up), wrap. A light status tint never goes on it: lines tint from the ANSI colours.
- **ImageFrame:** a page screenshot with findings outlined and numbered, boxes in the picture's own
  pixels; pins are buttons named "Finding 2: ..." when they can be picked; Zoom opens it large.
- **BeforeAfter:** a wipe on a range input, or the two side by side.
- **SelectionBar:** the ink bar over a table while rows are selected: "2 runs selected", the actions
  (`variant="inverse"`), Clear (Esc).
- **Slider:** a row of cards with the next one peeking; the pager ("1–3 of 5"), arrow, page, Home and End
  keys, a mouse drag and a swipe move it, and it snaps to a card.
- **Swatch, TokenChip:** a colour shown as data (marked `data-studio="ignore"`), a token name as a chip.
- **States for people:** `StateBadge` (`state-badge.tsx`) for screens a non-technical person reads: a
  shaped glyph and a word for All good, Waiting for you, Stuck, Sleeping and Working now (ink, with the
  ring). `lib/states.ts` maps every condition to one with one plain sentence.
- **Words:** `src/lib/vocabulary.ts` holds the everyday words; `principalName` names who did something.
- **Technical details:** `useTechnical()` is one person's switch for ids, logs, the terminal and costs.
- **Identity marks:** `BusinessMark`, `PersonMark`, `SiteMark`, `AgentMark`: fills on small marks only.
- **Grids:** a bare `.grid` has a shrinkable `minmax(0, 1fr)` track (`app.css`); flex children that
  truncate still need `min-w-0`.
- **Focus:** one blue ring from `:focus-visible`, keyboard only (`lib/input-modality.ts`). Inside
  anything clipped, add `focus-inset`. On a terminal the ring is the ANSI blue so it shows on black.

### Tables

From `table.tsx`. Real `<table>`, no zebra, **no grey band**: the head is the table-header role in the
tertiary ink on the card over one hairline, sticky in its scroll box (`TableScroll`). Why: the band made
every table look like a spreadsheet and added a third grey.

- **Rows** 52 px (`density="compact"` 40, which hides second lines; `relaxed` 60), head 40, a hairline
  between rows, 20 px gutters at both ends.
- **Sort:** `Th sort="asc" | "desc" onSort={...}` makes the whole head a button, sets `aria-sort`, and
  shows the arrow on the label's far side so the label never moves.
- **Numbers:** `Td numeric` (and `Th align="end"`): right, Geist Mono, tabular. Units in the head
  (`Th unit="min"`). `<NoValue />` is an en dash; a zero is a zero, in `text-faint`.
- **Main cell:** `Td main` (or `mainCell`) takes the spare width and truncates; `CellTitle` gives it a
  title with its full text on hover and a second line.
- **Rows that act:** `Tr onActivate` takes a hover fill, a pointer, focus and Enter. `Tr selected` is a
  tint with a 2 px ink edge. Row actions go in `RowActions`, which fade in on hover and focus (always
  shown on touch). Selecting shows the `SelectionBar`.
- **Loading** is `TableSkeleton` (the real head and seven rows); **empty** is `TableEmpty` holding an
  `EmptyState`, with the head kept.
- **On a phone:** `Table stack` turns the rows into a list under 768 px: the main cell gets its own line,
  `Td phone="hide"` cells fold away, `Td label="Tests"` shows a label before its value. An older table
  may still use `hidden md:table-cell` and move values into the main cell; new tables use `stack`.
- Anything inside a fitted column that has no content width of its own needs one (`shrink-0` on a
  `Meter`, `w-max` on a bare `.grid`).

## The shell

The frame every page sits in (`src/components/shell/`), drawn from the Mono prototype.

- **The rail** runs full height on the left (240 px from 1024 px, icons only from 768 px, gone on a
  phone). From the top: the org and its switch, **Search or run** (the command bar, with `⌘ K`), the
  menu, and at the foot the **status line** and the Technical details switch.
- **The menu** is the one the owner approved with Mono, in five groups, each place with its "g" key:

  | Group | Places |
  | --- | --- |
  | Work | Home (G H), Environments (G V), Runs (G R), Checks (G C) |
  | Where it runs | Machines (G M) |
  | Who it is for | Brand (G B), Accounts (G A) |
  | Automations | Routines (G O), Email (G E), Site APIs (G I) |
  | You | Needs you (G N), History (G Y), Settings (G S) |

  A new place goes into one of these groups (`nav.ts`), with a key no screen acts on by itself. The key
  after g is always swallowed, so "g d" can never deny and "g a" can never approve. An approver's menu
  stays Needs you, History and Settings, with no group headings and no status line.
- **Counts** sit on the right of a place: what waits for you in ink on Needs you, runs going now in a
  quiet pill on Runs, open environments in a quiet pill on Environments. On hover or focus the place's keys take the count's spot. On the icon rail a dot
  stands for the number. Every count comes from `counts.ts`; a number the node cannot give yet is left out,
  never guessed.
- **The status line** says the machines in a few words ("2 online · 1 not answering") beside one bar per
  machine (pale when answering, amber when slow, low when not answering). It opens the system popover: the
  worst thing first as a sentence, then each machine, the AI and today's AI usage, with Freeze for whoever may
  freeze. On a phone the same popover opens from a dot in the top bar.
- **The top bar** covers the page column only: the breadcrumb on the left (the workspace switch, the place,
  then the page's own name in mono, which a page can set with `usePageCrumb`), and on the right a Frozen chip
  while a freeze holds, **Live** while the event stream is open (Reconnecting when it drops, nothing when the
  node has no stream), the keyboard list, the theme (one press between light and dark; System stays in the
  account menu), what needs you (the bell) and the account menu.
- **No clock.** The browser's clock is not the machine's, and a number that changes every second pulls the
  eye from the one thing allowed to move: work that is running. Times on a page say "2 min ago".
- **On a phone** the top bar holds the org's mark, the workspace switch, search, the status dot, the bell
  and the account; the tab bar holds Home, Runs, Checks, Machines and Needs you, and More holds the rest.
- **The command bar** lists every place with its keys, then what features add, the workspaces, accounts once
  you type, and the actions. A feature adds its own rows by exporting `commands` from
  `src/features/<it>/commands.ts` (see `commands.ts`); it never edits the shell.
- **Addresses.** Machines' address is `/machines` (inside the app the route is still `nodes`, see
  `app/paths.ts`); `/computers` and `/nodes` links keep working. Each place's params live in its own folder
  (`features/<place>/search.ts`), so a screen adds a param without touching the router.

## The instrument layer

This is a computer, and the panel should feel like looking into a machine room you own. Calm
comes from hierarchy, not from showing less. The layer lives in
[`src/styles/instrument.css`](src/styles/instrument.css), on top of everything above.

- **The current.** `--current` (class `live`) is ink in Mono (it was pink), and it means exactly one thing: an
  agent is acting right now. It is never decoration and never a resting state, so the eye
  learns to trust it. Light that travels a wire on the map is this colour.
- **State marks.** `<StateMark>` shows state by shape as well as colour: a dot for fine, a
  ringed dot for acting, a diamond for a person in control, a triangle for needs a look, a
  square for blocked, a hollow ring for idle, a cross for down. Words for each live in
  `MARK_WORDS`. Use them on boards and instruments; use `<Status>` in tables and lists.
- **The board.** `board-surface` is a dotted drafting sheet in light and a machine room in
  dark, with its own hairlines (`border-board-line`) and wires (`--board-wire`). Only full-bleed
  instruments sit on it. A full-bleed page marks itself `data-bleed`
  and the shell drops its gutters and folds the rail to icons.
- **Silkscreen.** `silk` is the small mono caps label printed on a board: column heads on a
  board, `Heartbeat` on a node. It is the one place capitals are allowed. Never for a sentence.
- **Registration marks.** `registered` puts two small crosses on the corners of a panel. One
  per screen at most, on the instrument that matters most there.
- **Encoding.** Prefer a ring, an outline or an edge width to a solid status fill. A blast
  radius outlines in red; a down node is hatched; an incident window is a faint red wash with
  a dashed edge. Solid red is for a mark a few pixels wide.

### Machines

Machines (`features/nodes/machines.tsx`) is one table, as app-design.md section 2 and the Mono
prototype draw it. The businesses map and the fleet diagram that were its centre are retired (their
work is done elsewhere: accounts by business on Accounts, who is working on Home, the wiring on
each machine's page).

- **A sentence on top** names what matters: how many answer, which is busiest, what is not
  answering and since when, and a disposable VM with its time left (`machinesSentence`, model.ts).
  One machine is a sentence of its own ("home-mini is your one machine at 22% CPU, running ...").
- **The table**: machine (kind tile, a dashed cloud for a VM; name; clock zone and memory, or the
  VM's place and size), status as shape and word (Online; Busy at 85% CPU or more; Struggling; Not
  answering), doing now (the running `agentos run` command with its kind tile and who started it,
  or the accounts an agent is in), CPU with its last hour as a sparkline, memory as a meter,
  browsers, terminals and the requests waiting. A value the node does not send is an en dash, never
  a guess. Columns drop by the table's own width (container queries), never by scrolling.
  Machine, status, CPU and memory sort, and the sort is in the address (`sort`, `dir`).
- **Detail on demand.** A row opens the machine beside the table at `/machines/<id>` (a Drawer
  under 1024 px): about it, load for the last hour, runs with a link to each, lanes by kind with
  their state, services, health, Freeze and Remove, and a link to each of the twelve tabs of the
  full page (`/machines/<id>?tab=...`). A VM shows its price, time left, what it is for, and the
  real command to delete it sooner.
- **The last hour** draws real runs only (start and end per machine). With no run in the hour it
  is left out and the page says so; over six machines only those with runs get a line.
- **One machine** gets what is on it now under the table, and, on a node, the real ways to add
  another (`agentos vm setup`, `agentos connect root@my-server --install`,
  `agentos controller join-token`). Mock it with `?mock=one-machine`; twelve rows with
  `?mock=machines-many`.

### Node pages

About This Mac, Activity Monitor and Console in one. The pattern for any deep object page:

- **Live header.** Name and status, then one plain sentence generated from data
  (`describeNode`: "Runs 18 lanes for ..., holds the anthropic-api brain, and has 3 schedules
  that fire 2 times a day"), then a mono meta line. Under it, one `registered` panel: a
  heartbeat strip (one tick a minute, as tall as the CPU was, a red floor tick when away) and
  a stat row (label, value, micro trend, note; the same four rows in every tile).
- **Tabs in the URL.** `?tab=`, and every filter on a tab too (`range`, `scope`, `source`,
  `level`, `q`). Tab changes replace history and keep the scroll. Counts sit in a fixed slot.
- **One incident window.** `incidentsFrom(connections)` gives the times the node was away, and
  every chart on the page shades the same windows (`TimeChart bands`, `MiniTrend bands`).
- **Not reported is not empty.** A depth route that answers `unsupported` reads as `null`, and
  the tab says `<node> does not report <this> yet` with `NotReported`. Never a zero, never a
  made-up number. When a node reports none of a tab's parts, say it once, not per part.
- **Logs** stream from the node, are redacted on the way, and are never stored, and the tab
  says so. Scrolled up or paused, the view holds still and new lines wait behind a pill; End
  or the pill jumps back. Filters apply at once; the search lands in the URL a moment later.
  Away or not an admin, it says why instead of showing a stale cache.
- **Optimistic toggles** (pausing a schedule) patch every view that shows the thing and roll
  all of them back together.

### The lane live view and terminal

`features/lane/live/`. noVNC and xterm load on demand, so the panel's own bundle stays small.

- **Read-only until it is yours.** Watching never sends input. Taking over opens a `control`
  connection behind the view one, and only once it shows a frame does the toolbar say "Your
  keyboard and mouse go to the lane", the frame gets an inner ring and the keyboard moves in.
  Handing back puts focus back where it was. The holder is matched by user id, never a name.
- **Keys typed into a lane never fire a shortcut.** Anything inside `data-lane-input` counts as
  typing for `isTyping`, Cmd-K and Ctrl-K included. Shift Escape leaves the lane.
- **Every connection state says what happened and what to do:** connecting, live, reconnecting
  (with the time of the next try), node away (keeps waiting), waiting for approval, signed out,
  not allowed, not found, ended, could not connect. Words live in `live/copy.ts`; the close
  codes are read in `live/stream.ts`.
- **On a phone:** Full window, pinch, double tap and drag to zoom and pan (the hand toggle does it
  while in control), and a field that types a code into the lane. Paste reads the clipboard only
  on a tap and fills the field first. Nothing on screen is stored, and the lane's clipboard never
  reaches this one.
- **Terminal output opens no links**, and a reconnect says it is a new shell.

### Push, links and standing approvals (S3)

- **No link or notification approves.** A push carries Review and Deny only; an email or webhook
  carries Review. Review opens `/{org}/review/{id}?e=&s=`, which shows the one approval with the
  usual signed-in Approve and Deny and decides nothing by being opened. A link that has run out,
  was made for someone else or was edited lands on a calm page that says so and offers Needs you.
- **Deny from a notification** needs the signed-in session (the worker reads `csrf` from
  `/api/session`) and the content hash the push carried. Anything but a plain yes opens the
  approval instead, so the person sees why. It is never retried.
- **The service worker caches nothing but the offline page and its icon.** Never an API response,
  never anything that holds a session. Page loads go to the network and only fall back when it fails.
- **Push states are all spelled out:** this browser cannot, Safari on iPhone needs the Home Screen
  first, the site is blocked (with where to unblock it), the server has no key, the plan lacks it,
  off, on. A dead switch never sits there.
- **A computer offers no email.** Where the server sends none (`magic_link` is false on `/api/auth/providers`),
  the notification page draws no email switch, digest, email column or test email, and one sentence says so.
  While the answer loads, no email choice is drawn, and the note waits for the answer so a cloud never shows it.
- **Getting started offers only what can be done here.** A computer with no phone push has no phone step, and
  the push settings read the same fact (`PHONE_PUSH_HERE`).
- **The install prompt waits for a decision.** Never on a first visit, and a month of rest after
  Not now. On iPhone and iPad it says the two taps instead.
- **Heavy and changed items take a second step on the desk too.** `pay`, `irreversible`,
  `install_base` and anything marked changed open the arming confirm, and never go through in a batch.
- **Standing approvals say what no node does.** The cloud's own `enforcement.message` is shown as
  written on the form (asked before anything is filled in), on every active item, and on the lanes
  it covers. Nothing anywhere says approved automatically. `lanes: null` reads as every lane.
- **A commit limit says when it is not one.** With Ask me first off, a commit runs on its own and uses
  no standing approval, so its count caps nothing. Wherever what it covers runs on its own, the
  approval says so beside it, on Standing approvals and on the account, with how to keep the limit
  (`limitGap`). Only commits, only while active, and never for a computer that always asks.
- **Settings save as they change**, optimistically, and roll back on a refusal with a line that says
  the last saved choice is back. A busy button is `aria-disabled`, never `disabled`, so focus stays.

### An account's page

The account page (`features/lane/`, the sections are `account-*.tsx`, the words in `account-words.ts`) gives
an account's pace, limits, who is using it, outside text it read, health, sign-in upkeep, sites and a watch
link. Each section is one quiet block under a faint heading, answer first: a dot and its word, then one sentence.

- **Shown only with something to say.** A section that is empty stays out of the page. Pace, limits and sites
  still offer their one action to the owner when nothing is set, because that is something to say too.
- **Money and model use wait for Technical details**, as they do everywhere. A plain limit is a count of posts.
- **Only the owner changes pace, limits, sites or clears taint.** Members read and watch. The node refuses anyone
  else, and the screen hides the control for them, so a refusal is never the first thing they see.
- **Outside text says what it does.** An agent that read a page asks first, so the section says that in those
  words and offers clearing it to the owner, typed, with the node's own fresh sign-in.
- **A watch link is shown once**, copied from the page, and never stored.
- **Health reads the brief first.** The full checklist runs only when the brief says the account is not safe.
- **One problem, said once.** A problem (a failing check, a hold, a missing folder, a freeze) is shown in one place. A
  check's summary is never said again where its notes already say it.
- **Doing now, then needs you, then what it did.** The page reads in that order: the live view, the plan and who is using
  it, then what waits for a yes, then History. Pace, limits, sites, sign-in upkeep, files, watch link, standing approvals
  and routines sit behind one closed Settings disclosure, so every one is reachable and the page stays calm.
- **The state is the same everywhere.** An account's word and sentence come from `lib/states.ts` (a failing check makes it
  Stuck, "Not safe to use right now"), so Accounts, the business line, Machines and this page agree.
- **History names what the person did.** The owner's own changes (who has control, a freeze, a pace, limits, worker and
  outside-text choices) appear in the account's History in their words ("You froze it"). Reads of the page are not history.
- **Ids are names here.** An account is "Biz A · Social" and its plan "Social on Instagram", never `biz-a/social`.

### Memory, the day, and an account's files

- **Memory is on Settings, for the owner only.** It is what agents remember, so it sits with retention and audit,
  not in the rail. Each workspace is a room against its caps, with what waits for a look and whether its record
  checks out. Its text is shown as plain text and never followed. Forgetting erases for good, so it is typed and
  needs a reason; the reason stays on record.
- **An account's files sit on its page, before its history.** A browser account only. Anyone who sees the account
  downloads; an operator adds and deletes. An add is checked against the node's own limit before it is sent, and a
  file is only ever kept and handed back as it is.

### Ask me first

Routines run on their own. Every routine and every account reads "Runs on its own" or "Asks you
first", from one place (`lib/ask-first.ts`), and the cloud decides which: never work it out on a
screen. `features/ask-first/` has the row word, the row switch and the panel.

- **A switch is only drawn where it would change something.** A payment or something that can't be
  undone (a lock, it always asks), a routine that only looks or drafts, a routine whose account is
  what asks (a link to the account), a computer without `ask_first` in `hello.supports` (it asks
  before every commit, and says to update it), and a computer that is offline each say why instead.
- **What runs and who may change it is the cloud's.** A viewer or approver sees the state and no
  control. A member can turn a switch on and is told who can turn it off. Turning one off needs an
  owner or admin and the step-up, which says what is about to happen (`stepUpReason`).
- **One flip.** It moves at once in every view, says "Telling the computer." in a slot that is
  always held open, and only that switch rolls back, with a line saying which choice is back. A
  cancelled step-up rolls back in silence. Busy is `aria-disabled`, so focus never leaves the switch.
- **Say who, and why.** An agent can turn a switch on and never off, so that line says only a person
  can undo it. A routine an agent saved beside one a person set to ask first starts on, and its
  reason sits in the same line (`ask_first_why` through `whyWords`, always as text).
- **It stays quiet.** One strip under what it belongs to, never a card that competes with it.
- **Never promise more than the switch does.** A publish by clicking is refused only where the
  lane's guard recognises it. So an account never says "everything waits", and its panel always
  carries `ACCOUNT_LIMIT` beside the switch.
- **Why it asks, on the decision itself.** `whyAsked` turns the node's `reason` into plain words. A
  question a safety check forced gets a notice above the decision, naming what it read. Words that
  can't be put plainly stay in Details for the technical. Node text is rendered as text.
- **Needs you** holds only what someone asked to see, plus money and what can't be undone. Examples
  always span emails, invoices, forms and orders, never posts alone.

### Routines

A routine and a workflow are the same thing, on one surface (`features/flows/`, model in
`lib/routines.ts`). A person comes here to know what ran, what comes next and what is stuck, so that
is the page. Concept stage: on the mock only, until the owner has steered it.

- **The list answers before it lists.** One sentence as the title, as on Home, then the line through
  now (`day-line.tsx`: what ran on the left, what is planned on the right, each as its site; tiles
  that would collide fold into a count, so 170 reads like 17), then what is stuck, then every routine
  by business. Rows carry the sites, the sentence, what it last did and when it runs next.
- **Nothing in the list can be set off by a slip.** No switch and no Run in a row. Those live on the
  routine's page, where a person has read what they are changing.
- **A routine's page leads with the job.** The sentence is the title, and each phrase lights its step
  on the map. Then the steps as a flow, then what it really made (the email, the invoice, the post,
  with how we know), then its rhythm and runs. Ask me first is one quiet strip, and technical facts
  appear only with Technical details.
- **The sentence is the editor.** In the builder, unfinished phrases are underlined, and pressing one
  opens its step. Turn on never greys out into a dead end: it takes the person to what is missing.
- **Every board action has a keyboard twin and an undo.** Drag by the handle or Alt and an arrow, A
  adds, Delete removes, Cmd Z undoes, and focus stays on the step. Steps settle on a transform.
- **Send steps say their rules with a lock, never a control.** Daily limit, suppression, unsubscribe.
- **Data shows as a named chip, never as braces.** Text from outside can fill in words, and a decision
  cannot read it: the picker leaves it out and says why.
- **A template says what it needs.** One that sends or reads mail carries `needs: ["mailbox"]` from the node, and the
  picker says "Needs a mailbox first. Connect one in Email." on it while no mailbox is connected. It is still offered.

### Site APIs

One job on a site a person already uses, turned into something to call (`features/site-apis/`, the
shapes in `api/types.ts`). Concept stage: on the mock only, until the owner has
steered it. Two audiences share every page, and neither is a mode: a person never meets a technical word
on the way to the data, and a developer never has to ask for the URL.

- **The page-to-data view** (`split.tsx`) is the signature. The page as the computer saw it on top, the
  data it became underneath, a ribbon of wires between. Stacked, not side by side, so that a column on the
  page falls straight into the same column of data and both keep room for real values. Pointing at a value
  on either side lights its partner and draws the line, and it runs down the column's edge, never through
  text. Header cells take keyboard focus and do the same.
- **A field keeps one hue on both sides** (`--field-1` to `--field-6`). Marks, outlines and wires only,
  never text and never a surface, and every field has its label beside it. Pink and red are not in the set.
- **The sheet is a wireframe, never a copy.** Its text is shown as text; it has no logo, no colours of the
  site's and never a login. Sites in the demo are invented, under `.example`. What was left out stays
  visible under a hatch, because seeing what was skipped is half of trusting what was kept.
- **The picture says the state.** A broken field is a broken red wire and "Held back" in the data, resting
  and paused are quiet dotted wires, needing a person veils the page, and a read in progress runs the current (ink) down
  every wire, because the computer is acting now. Under any trouble the data panel says "Last good read".
- **Each state has plain words and one next step** (`words.ts`): what happened, what callers get meanwhile,
  and one button. A step that needs a look first (Show me) changes the button, so the decision is always
  beside the picture it is about. Nothing scrolls away from it.
- **The list shows the job.** Each sample has the shape of what it gives: rows, money by how late, stock
  against its reorder point, a parcel's journey, what people said. Trouble gets one line each, and the
  busiest answering API leads.
- **Two doors.** See the data (table, copy for a sheet, CSV, keep updated) and Use it in code (a form built
  from the input schema, the real answer with how it was served, snippets, OpenAPI). A failing try shows
  exactly what a caller would get.
- **Teach it** is pointing at a page. The sentence on top is the whole Site API so far and each phrase goes
  back to its step. Found regions are outlined, columns are pressed on the page itself, and sample values
  move in the panel. JSON definition flips the panel at any step, from the same draft (`draft.ts`).
- **Only a person turns one on.** An agent's proposal shows the node's own words, never the agent's, with
  Don't turn on to the left of Turn it on. A key is shown once and reads by default. An API runs on one
  account: pace is a gauge of that account, one tick per read, and nothing offers another account, a
  rotation, or a way past a site's limit. `pay` and `irreversible` are not in the endpoint type at all.

### Email

Connecting a mailbox, reading what is new, and writing to leads (`features/email/`, the shapes in
`api/types.ts`). Concept stage: on the mock only, until the owner has steered it. A person
with no technical knowledge has to be able to do all of it unaided.

- **The hosted panel holds no password, ever.** No field, no type, no handler. At the moment one is needed it
  hands over to the person's own computer, and the route picture (`route-picture.tsx`) makes that read as the
  short way: You, your computer, your mail, with agentos Cloud to one side and no wire a password could take.
- **The computer's form is a drawing.** It stands outside the panel at `/computer-form`, says plainly that a
  computer serves it, and its box is dots with no input behind it. A test keeps it that way.
- **One picture says every state.** Looking runs the current (ink), the password is a key that moves, and a failure breaks
  the exact wire: read, send, or the door. Each failure has plain words, says nothing was saved, and gives one
  next step, usually a note a person can copy for whoever can fix it. A certificate that does not check out
  has no way past it.
- **Jargon waits to be asked for.** "Found Gmail", then Show the server names. A low-trust result is the one
  place names lead, because a person is being asked to judge them, and the part that differs is marked.
- **Never imitate Google.** Steps are our own words with a link to Google's own page. Its mark only names it.
- **A mailbox is two wires.** Today's mail sits on the wire in, and the day's limit is the wire out, as a tick
  gauge: ink for sent, an outline for planned. They are the two checks from connecting, so trouble shows where
  it is.
- **A stranger's words have their own edge.** `stranger` is a hatched margin with Written by someone else. Mail
  is text, a picture is a hatched note that says what was not fetched, a link is its text and address and cannot
  be pressed, and an attachment is listed as not opened. What agentos itself says never sits inside that edge.
- **Where text comes from is said.** Mail and leads' addresses stay on the computer, so a page that shows them
  carries `FromComputer`.
- **The sheet is the signature.** All fifty in one look, on a desk of its own (`data-bleed`). Everyone starts in;
  pressing leaves someone out under the hatch, dragging leaves out several, and arrows and space do the same.
  What is worth reading twice comes first in its group with an outline and a glyph, because an address on a page
  is text a stranger wrote. Who is on the do-not-write list is locked in the same grid and cannot be pressed.
  One fixed place reads the person pointed at, so reading moves nothing. Taking them drops the tiles into the
  desk's dots. Drop stays left of Take.
- **The plan is a shape.** One dot for one person, in a day as tall as the mailbox's limit, so what cannot happen
  cannot be drawn. Dragging the pace moves dots between days. `plan.ts` is pure and tested: once each, weekdays,
  a follow-up waits its days, no day over the limit.
- **The note on being the sender shows once**, on a business's first list. It informs and does not gate.
- **The chase is the routine's promise, drawn.** One line for each person from their first email; a reply cuts it
  short, and day 3 is one straight gate. The sentence and the map are the Routines ones, and pointing at a step
  lights its people.
- **Nothing offers another sender, a guessed address, or anything about a spam filter.** A test reads the source.

### Workers and AI models

Browser and emulator work is expensive when a frontier model does every step. So an agent says what to do,
and a worker on a person's own computer does the steps with an AI model that costs far less
(`features/workers/`, with `features/settings/brains.tsx`). Concept stage: on the mock only, until the owner
has steered it. It is self-contained, so the public build can ship it or leave it out.

- **Two things, kept apart.** An AI service is who thinks (`provider`): one account, several models under it.
  A worker is what you turn on (`plugin`). A worker hands each job to a service: Decides each step, Writes text,
  Helps when it is unsure. Still unsure, it stops and asks a person, and that row is a lock, never a choice.
  "Job" only ever means one of those. What a worker finishes is a thing done, as everywhere. "Ladder" belongs to
  How a person says yes, so a worker asks for help and never climbs.
- **Where it lives.** Nothing in the rail. Settings, AI models (`/settings/brains`) is who thinks and Settings,
  Workers (`/settings/workers`) is what is turned on, because both are set once and read rarely. A computer's
  Workers tab reads what it runs, what it cannot run and why, and which services it keeps. It has no switch, so
  a worker has one switch per computer in one place.
- **No timeline of its own.** What a worker did sits in History (its steps in the story, "with the browser
  worker" on the row), on the account (one quiet strip, only while it has something to say), and in Needs you when
  it asks (how it got there, above the question). The Workers page shows one thing, now or last, and links on.
- **The step line is the signature** (`step-line.tsx`). One thing done, a tick for each step. The rows are who
  thought, in the same order as the jobs, so most of it stays on the top row and a step that needed help falls out of
  it, as far as it had to go, the way a stop falls on What needs a yes. It is step by step where that is a day.
- **The rail is the limit.** It is as long as the most steps allowed on one thing, so all step lines share a scale
  and a step past the limit cannot be drawn. The current (ink, was pink) is the step being taken now and nothing else: done, asked and
  stopped never carry it. Pointing at a step, or arrows on the slider, changes one fixed line underneath, so reading
  moves nothing. A phone keeps the same 60 steps and moves the counts to a line under the picture.
- **Answer first.** Lead with what needs a person: a stop, a robot check, an offline computer, a spent limit, and
  only then what got done. Every state is one sentence and at most one button (`workerAnswer`).
- **Off shows what it would be.** Before there is a real thing, the line is drawn faint under "How one thing will
  look", and the one primary is the next step. With nothing that can decide, that is Add an AI service, and the rows
  below do not repeat it. Turn on never dead-ends.
- **A switch is only drawn where it would change something.** A computer with no service that can decide says so
  with Add an AI service. An offline computer says so with no control. Only a person turns one on: the dialog says
  what changes, Don't turn on sits left of Turn it on, and the step-up says why (`TURNING_ON_WORKER`). Turning off
  is one flip, with "Telling the computer." in the sentence's own place. An agent can ask, and never press it.
- **Off for one account.** Under Ask me first on the account, one row: on or off, and what off means (an agent can
  still work here directly, and you can take over). It shows only while a worker runs on that computer, with no switch
  while the computer is offline. Off is one flip anyone who works the account can make; on is the looser direction, so
  it needs an owner or admin and the step-up (`TURNING_ON_ACCOUNT_WORKER`). Settings, Workers says the same under where it runs, in one line: the accounts it is
  off on, as links to that row, and how its posts go.
- **A person can give it a job.** Give it a job sits under that row, only while the worker works the account, for
  whoever works the account. It is the same job an agent gives, and it runs the same way. Nothing is promised about
  what it will do: the strip above and History say what it did.
- **Its posts have no switch of their own.** They go by the account's Ask me first, and only that strip names the worker,
  only while the worker works there. So `NEVER` and the step words never promise a yes on an account that runs on its own.
  And a post that follows a page asks on a computer that says so (`after_a_page_asks`, the default), which the worker
  always does, so there "Runs on its own" still reads as asking you. Never say a post goes alone until it can.
- **Limits are steps, not money. Tiles use the usual rows, the title is the answer ("Inside its limits today"), and
  the meter colours are the usual ones. Money, model names and milliseconds wait for Technical details.
- **Only what can do a job is offered.** A service that never writes is not a choice for Writes text.
- **The hosted panel holds no key, ever.** No field, no type, no handler, and a test reads the source. Adding a service
  asks who and which computer, then hands over as Email does: `key-route.tsx` shows You, the computer and the service
  in a line with agentos Cloud to one side, and the form at `/computer-form/ai-key` is a drawing. A computer with no
  screen says so and shows its one line. Otherwise `agentos brains add` waits behind Technical details.
- **AI models answers first too.** One sentence on how many are ready, then each service as what it is for, where it
  is kept and what uses it. The try order and its arrows are for agents, so they wait for Technical details.
- **What is coming is said calmly.** The Android and desktop workers are a line of words at the foot: no button, no
  dead switch, and what each computer lacks.

### Proof, in History

The record behind a story (`features/activity/proof/`, words in `words.ts`). One account's day is replayed step by
step, and each step says in words whether it is signed, not signed yet, from before the log began, or changed.
"The log is intact" is said only when the node checks the whole log, and only the owner is asked. A read-only link
(`/shared#token=`, `features/shared/`) opens with no sign-in and no app around it. It shows what the link allows,
with no controls and no arguments, and says plainly when the link is not valid. A token is shown once, never listed.

- **Sharing and checking are the owner's.** The node refuses anyone else, and the screen never asks them.
- **Stopping a link is one confirm.** It stops at once, and the list says what each link did (opened, ended, stopped).

### Work handed to a model

What an agent or worker gave an AI model is listed under AI models for the owner (`features/workers/handed-to-models.tsx`).
It reads its own state again every few seconds while anything is still going, and stops once nothing is. Stopping asks
first and says what stays done. Carrying on is offered only where the node can resume the task.

### Overrides, the business queue and devices

- **Overrides** (a safety check a person turned off, and why) show on an account's Ask me first, under Technical details,
  to the owner alone.
- **A business** (`/workspaces/$ws`) lists its accounts for anyone who sees it. The logins still to do are the owner's.
- **Signed-in devices** of a member show to an admin, from Members, and an admin can end one after a confirm. Their own
  devices stay on their profile.

### How a person says yes

Most things never ask. This is about the few that do (`features/confirmations/`, words in
`lib/confirm.ts`, the rules in docs/CONFIRMATIONS.md). Concept stage: on the mock only,
until the owner has steered it.

- **Words, never levels.** A person reads On its own, Just a tap, A tap in agentos and Prove it's you, each
  with its drawing. `signed_in` and `device_key` stay on the wire and in Technical details.
- **One ladder everywhere.** The setting, the places you can say yes and the record of a yes all use the same
  rungs in the same order, so a strength is learned once.
- **The day is the setting.** What needs a yes opens on a real day: every line is one thing done, and what
  stopped at a person drops out of the stream as far as its yes is strong. Pointing at a stop on a row
  re-draws the day first, so a person sees what a change means before making it.
- **A floor is drawn, never met.** What can't go lower is hatched under a lock with its reason beside it.
  Money and the irreversible always ask, so only how strong the yes is can move. Choosing a business lifts
  the hatch to the org's rung, because a business can only ask for more.
- **The fingerprint is earned.** It marks what needs proof and nothing else: a row in Needs you, the yes button,
  the strip above it. A plain item shows none of them, so it keeps meaning something.
- **The step up comes before the press.** The strip says what it takes and why, then Prove it's you is the
  second step, in place of Are you sure. A failed check decides nothing, and says so.
- **A yes on the record says who, where and how strong.** A yes in chat reads like any other, and says agentos
  wrote the question and the agent could not answer it.
- **The agent never holds the pen.** In the chat concept the words are agentos's own and are never a message.
  The look of a prompt is the chat app's: never imitate a real product, and never promise it a logo.
- **What is coming is said calmly.** A later method is a line of words with no button and no dead switch.

### Break glass and login codes

- **Freeze is one press and one confirmation.** It lives on the account page, the machine page, the status line's
  popover and the command bar, and only for the people the node lets freeze: an admin freezes an account, the
  owner freezes a machine. While one holds, the top bar says Frozen on every page.
  The dialog says what happens in plain lines (every agent there stops at once, every grant that reaches it is
  revoked, the browser is kept so a person can take over and look), with an optional reason. Its button is `danger`:
  a red label on a hairline, never a red fill.
- **A freeze is shown where it reaches.** The account or the computer says Frozen with `StateBadge`, and a notice
  says who froze it and why. Only the owner sees Unfreeze, and it asks for a fresh check first. Its dialog says that
  nothing is restored: grants stay revoked and paused schedules stay paused.
- **A login code waits in Needs you, weighted like a question.** Only the owner is asked for one. The field is plain
  text, never a password field, and it is cleared the moment the code is sent. The code is never kept in the query
  cache or shown to an agent, and "No code came" stops the login instead.

## States

Every screen designs all of these. `QueryView` handles the first two, so use it.

- **Loading:** skeletons shaped like the content: same columns, same row height, and a header if
  the table has one. The page must not move when data arrives, so a subtitle holds its line and
  a header action shows disabled. Never a page spinner.
- **Error:** says what happened, that nothing changed, and offers Try again. It sits in a card,
  so pass `card` to `QueryView` when the view is not already inside one.
- **Empty:** an icon, what would be here, and the next action. Never "No data".
- **Live:** values change in place with tabular figures and fixed widths, so nothing shifts.
  New activity rows wait behind a pill rather than moving what is being read.

## Deciding safely

An approval is the one thing here that must never go wrong, so these are rules, each with a test:

- What is on screen is what gets decided. Focus and the phone card are held by id, so a live
  arrival cannot change the target of the next key or tap.
- Only visible items can be decided. A selection hidden by a workspace switch is out of play.
- Deny is always on the left and Approve on the right, on every screen and at every width.
- A held key decides nothing, and the key after `g` is always swallowed, so `g d` cannot deny.
- A double tap on the phone is one decision.
- Toasts on a phone go to the top. The bottom of the screen belongs to the thumb.

## Copy

Calm, short, specific, plain. Sentence case everywhere.

- **No em dash** (U+2014) anywhere under `src`, comments included. `src/test/source-rules.test.ts`
  fails on it, and `make emdash` at the repository root checks the text people read. Use a period, comma, colon, parentheses or a middle dot.
- Be honest about state: "Verified", "Unverified", "Partial", never a bare "Done".
- Say what happened and what to do: "This link has expired", not "Error 410".
- Node-supplied text is always rendered as text. `dangerouslySetInnerHTML` is a lint error.
  Node-supplied links go through `safeUrl`, which only allows http and https.

## Brand marks

`SiteMark` (Instagram, Facebook, LinkedIn, Reddit, Medium, X, YouTube, TikTok, Gmail and the
rest) and `AgentMark` (Claude, ChatGPT, Codex) show other companies' marks. They exist for one
reason: to name the site an account is on, or the agent that acted. So:

- Official artwork only, unmodified, in the brand's own colour or plain monochrome. Black marks
  take the foreground colour on the dark theme, which is the brands' own monochrome rule.
- Only beside or in place of the brand's name. Never as our logo, app icon, marketing hero or
  decoration, never combined with our mark, never animated, nothing that suggests a partnership.
- Sources: Simple Icons (CC0), imported one icon at a time so the bundle carries only those, and
  `components/brand/vendored.ts` (Bootstrap Icons, MIT, licence kept in the file) for LinkedIn and
  OpenAI, which Simple Icons dropped. Codex has no mark in a free set, so it is our own drawing (a
  prompt on black) and always sits beside the word Codex. An unknown site is a globe with its
  initial; we never fetch a third-party favicon from the cloud.
- Our own kinds of lane (Blog, Mail, Shell, Terminal) use our own line icons.
- Login pages and screenshots stay wireframes (`src/mocks/art.ts`): marks name things, they never
  imitate a site.

## Product rules the UI enforces

- The rail is the five groups in "The shell": Work (Home, Environments, Runs, Checks), Where it runs (Machines), Who it is
  for (Brand, Accounts), Automations (Routines, Email, Site APIs) and You (Needs you, History, Settings).
  A new place goes into one of those groups.
- The workspace is the top unit. Lanes never appear as a flat list: Home shows one health line
  per workspace, and Cmd-K shows lanes only once you start typing.
- The vault shows names only and has no field for a secret. Tokens are shown once.
- No page imitates a third-party login or has a password field. Mock screenshots are plain
  wireframes for the same reason. The computer's form is drawn, with no field behind it.
- Status colours mean status. Workspace colour fills its small mark at full strength and is
  never a surface, a panel tint or text.

## Data

- `src/api/client.ts` is the one typed client, `hooks.ts` the Query hooks and `types.ts` the shapes.
  The hooks and the shapes live in one file per area (`api/hooks/`, `api/types/`), and `hooks.ts`
  and `types.ts` re-export them, so a screen imports from those two. Every query key is in
  `keys.ts`. Screens import hooks only. A lint rule and a test stop any screen importing `src/mocks`.
- `VITE_API_MODE` is the single switch (`mock` in `.env.development`, `live` in
  `.env.production`). A live build contains no mock code.
- `src/api/http.ts` owns CSRF, the error shape, 401 and step-up. A `403 step_up_required` from
  any route opens the dialog and retries, so no screen handles it.
- `src/api/live.ts` has one path: a real `EventSource` into `applyLiveEvent`, which patches the
  cache. The mock answers the same URL with a real event stream, so nothing branches.
- A mutation whose screen shows its own error sets `meta: { inline: true }`. Otherwise the
  global handler raises a toast.
- The hosted app. A route the cloud does not serve is listed in `src/test/cloud-gaps.json` (kept in step
  by the hosted edition's own tests). Its read is wrapped in `served()` in `client.ts`, so a 404 reads as
  "not here" and `NotLive` says where the part is: "Shown on your computer" on the hosted app, and the
  computer's own wording on a computer. A control that could only reach such a route is not shown: gate it on
  the read's own answer, or on `useProviders().data?.code === true` when there is no read. Nothing is offered
  until the answer is in, and an error state never shows for a route that is not served. `src/app/hosted.test.tsx`
  renders the screens with every such route answering 404.

Mock scenarios for demos and tests, set with `?mock=`: `still` (no background changes),
`loading` (skeletons), `error` (500s), and for Workers `workers-new`, `workers-ready`, `workers-asked`,
`workers-robot`, `workers-offline`, `workers-limit`, `key-refused`, `worker-alone`, and `log-changed` (Proof's log check says
the log was changed). `/juniper-labs` is an empty org for empty states.
Magic links in mock mode are printed to the console. `token=expired` and `token=used` show those
states on `/auth/verify`, and the same words work on `/invite/`.

## Do and do not

| Do | Do not |
|---|---|
| Use one primary button per view | Add a second filled button "for balance" |
| Separate with hairlines, space and the one card lift | Add other shadows, gradients or tinted cards |
| Show a shape and a word for status | Fill a row with status colour, or draw a kind as a dot |
| Put ids, hashes and IPs in mono | Set prose in mono |
| Keep plumbing in an inspect drawer | Put proxy hosts or engines on the main view |
| Keep numbers in text colours | Colour a number unless it asks for attention (failed in red) |
| Use a kind's hue only through `KindTile`, `KindEyebrow`, `Card kind`, `Chip kind` | Use a kind hue for status, selection or decoration |
| Design for 375 first, then widen | Hide a feature on a phone |
| Measure overflow at 375 | Assume a screenshot would show it |

## Checking your work

```bash
pnpm typecheck && pnpm lint && pnpm test && pnpm build
pnpm test:e2e                      # real browser flows, system Chrome
pnpm dev                           # in another terminal, then:
node scripts/screenshots.mjs --sheet
python3 tokens/build-tokens.py --check   # the token set and its contrast and colour-blind checks
(cd .. && make emdash)             # no em dash in text a person reads
```

Open `/$org/design` (the catalog) after touching a primitive. The script writes every route at 1440 and 375, light and dark, to `screenshots/` (git-ignored); set `BASE_URL` to your dev server (never port 5310 on a shared machine).
`--only home,needs` narrows it, and `--sheet` builds contact sheets for reviewing many at once.
Read them against the bar. Look for alignment, rhythm, hierarchy, density and anything that
feels like a template.

## Open assumptions for the API side

The panel needs these and `docs/API.md` does not pin them yet:

- `Node.version` on the list route (only pinned on the detail route).
- `Schedule.next_run`, so Home can show what is up next.
- `principal` on trace events as an object `{ kind, id, thread? }`.
- Reserved org slugs: `login`, `sign-in`, `sign-up`, `check-email`, `auth`, `invite`, `onboarding`, `api`,
  `v1`, `legal`, `computer-form`, `shared`. These are top-level panel routes.
