# openajar design system: Dawn

The app follows **Dawn**, the design system the owner chose on 2026-11-20: the desktop's light brought to
the web app. Dawn keeps Mono's bones (one token source, hairlines, a 4 px grid, shape and word for every
status, tables without a grey band) and adds what the agent's own computer looks like: a night navy, one
violet (the agent's colour), the sunrise picture, glass over it, the "Window a" mark. The app is drawn in
Dawn's **Full** strength (the larger type, pill buttons, the night sidebar, Home's window), with the owner's
two changes: cards less round than the mockup (10, not 14) and the organisation switch at the foot of the
sidebar.

The designer's sheet is [`docs/design/dawn/system.jpg`](../docs/design/dawn/system.jpg): every colour for
light and dark, the violet scale and its rules, type, radii, glass, icons, the wrapper tile, buttons, inputs,
the control pill, the mark, and where the Dawn picture may and may not appear. The UX is in
[`docs/design/app-design.md`](../docs/design/app-design.md), and the table, type and status foundations Mono
set are in [`docs/design/foundations.md`](../docs/design/foundations.md). Where those and this file disagree
about the app, this file is the rule. If you change a rule, change it here in the same commit.

The product is openajar's own: do not copy another product's logo, wording or layouts.

## What changed with Dawn

Said plainly, for anyone who knew Mono:

- **Ink with a trace of blue.** The neutrals carry the night sky's ink blue (`#15172b` text, `#f6f6fa` page,
  `#e7e7ee` hairline). Dark is Dawn's night navy (`#090b16` page, `#0f1222` card), not black.
- **One violet.** `#6d4aff` (violet 600) is the agent's colour on the desktop. In the app it is what is
  selected, the keyboard's focus, the primary button, an agent at work, and the mark. It is never a status, a
  large background or decoration. Links stay ink with an underline.
- **An agent at work is a violet ring that never pulses.** Mono's ink ring pulsed. Dawn's is still; Starting
  is the same ring dashed and empty. Nothing on a page moves all the time.
- **Tests moved from violet to plum**, so no kind of work shares the agent's hue.
- **Every button is a pill.** The primary is the violet pill with white words, once per screen.
- **Full's type, one step larger** for reading: body 15/22, a row's second line 14/20, the lede 17/26,
  sections 18/26, the page title 34/40, Home's sentence 44/50. The old `text-11` to `text-32` are gone
  (2026-11-19, `scripts/codemod-legacy-tokens.mjs`): each is the role of its size.
- **Radius:** cards stay 10, dialogs 12; fields 6, pictures, the live screen and menus 8, Home's window 14,
  buttons full.
- **The night sidebar.** The rail is the night sky in both themes, with a few still stars and the sunrise at
  its foot. The org switch moved to the foot, on glass.
- **Glass and the Dawn picture** are new, and only where the sheet allows them (below).
- **Icons** are drawn at 1.75 (was 1.5), the desktop dock's fine line.

## Tokens

[`tokens/build-tokens.py`](tokens/build-tokens.py) is the one place the values live. It writes
`tokens/tokens.json` and `src/styles/tokens.css`, and from [`tokens/theme.py`](tokens/theme.py) the Tailwind
theme (`src/styles/theme.css`) and its keys as data (`tokens/theme-keys.json`, which the design rules read).
It checks 176 text and mark pairs against WCAG AA in both
themes, plus layered surfaces (white words on each stop of the night sidebar and on the foot's glass over the
glow; text on glass over the Dawn picture's darkest and lightest skies; the lede on the picture under its
scrim). It also checks that the kinds of work stay apart from each other and from status (the violet ring
included) for protanopia, deuteranopia and tritanopia (OKLab distance 0.04 or more).
`src/styles/tokens.test.ts` fails when the two files drift from each other or from the script, or a check fails:

```bash
python3 tokens/build-tokens.py            # write both files and check
python3 tokens/build-tokens.py --check    # what the test runs: write nothing, fail on drift
```

Light is the default. Dark comes from the system (`prefers-color-scheme`) unless `data-theme` on `<html>`
pins one, so "system" still needs no JavaScript. Each theme is a block of plain values (no `light-dark()`),
so studio can read every token.

Three files, in this order, from `src/styles/app.css`:

0. `theme.css`: the Tailwind theme, every class name onto a token (generated from `tokens/theme.py`).
1. `tokens.css`: the Dawn set (generated). Roles, never steps: `--surface-*`, `--line-*`, `--text-*`,
   `--accent-*`, `--action-*`, `--status-*`, `--kind-*`, `--glass-*`, `--dawn-*`, `--sidebar-*`,
   `--terminal-*`, `--ansi-*`, `--diff-*`, `--chart-*`, `--type-*`; and the fixed sets `--violet-50` to
   `--violet-950`, `--tile-*`, `--mark-*`, `--identity-*`.
2. `aliases.css`: the names the screens used before Mono (`--background`, `--foreground-muted`,
   `--border`, `--ok-dot`, `--current` ...), each a reference to a token. It writes no colour of its own
   (a test holds it): the board's wires, Site API fields, the night band and the layout sizes are tokens
   now. New code uses role names; an alias goes when nothing uses it.
3. `primitives.css`: the rules the primitives need real selectors for (table, status shapes, kinds, the
   wrapper tile, terminal, image frame, slider). Screens never write these classes.

`theme.css` (generated) maps tokens to Tailwind and **clears Tailwind's default palette, sizes, radii and
shadows**. A class like `bg-red-500` or `text-sm` does not exist, and the design rules fail on one. Both sets
of names work, and the rules let the old ones only shrink:

| Use | Class | Old class (still works) | Token |
|---|---|---|---|
| The page | `bg-page` | (body) | `--surface-page` |
| Cards, tables, panels | `bg-card` | `bg-bg` | `--surface-card` |
| Wells: code, card footers, segmented tracks, pictures loading. Never a table head | `bg-inset` | `bg-subtle` | `--surface-inset` |
| Menus, popovers, dialogs | `bg-raised` | `bg-raised` | `--surface-raised` |
| Tooltips and the selection bar (toasts are the raised surface) | `bg-inverse`, `text-on-inverse` | | `--surface-inverse` |
| Behind a live screen and its pictures (dark in both themes) | `bg-stage` | | `--surface-stage` |
| Hover, pressed | `bg-hover`, `bg-pressed` | `bg-fill-1`, `bg-fill-2` | `--fill-hover`, `--fill-pressed` |
| Hairline; firm edge; control edge (3:1) | `border-line`, `border-line-strong`, `border-control` | same | `--line-*` |
| Text: primary, secondary, tertiary, disabled | `text-fg`, `text-muted`, `text-faint`, `text-disabled` | same | `--text-*` |
| The primary button: violet, white words | `bg-primary`, `text-on-primary`, `hover:bg-primary-hover` | same | `--action-*` |
| Selected, checked, on: violet | `text-accent-text`, `bg-accent`, `bg-accent-soft`, `border-accent-line` | | `--accent-*` |
| An ink fill with inverse words (a `main` branch, an ink count) | `bg-fg text-on-inverse` | | `--text-primary`, `--text-inverse` |
| Links: ink, underlined | `.link`, `text-blue` on a link | same | `--link-text` |
| Focus: violet | (the ring) | | `--focus-ring` |
| Status words | `text-ok`, `text-warn`, `text-bad`, `text-idle` | `text-green`, `text-amber`, `text-red` | `--status-*-text` |
| Status marks, tints, edges | `bg-ok-dot`, `bg-ok-fill`, `border-ok-line` (and warn, bad, live, idle) | `--ok-dot`, `--ok-soft` ... | `--status-*` |
| An agent at work: violet | `text-live`, `bg-live-dot`, `bg-live-soft` | `bg-live`, `--current` | `--status-live-*` |
| A kind's word, mark, tile, edge | `text-kind-test`, `bg-kind-test-mark`, `bg-kind-test-soft`, `border-kind-test-line` (and design, build, server) | | `--kind-*` |
| Glass on the Dawn picture; dark glass on a picture | `bg-glass`, `bg-glass-dark`, `shadow-glass` | | `--glass-*` |
| The night sidebar's words, lines, fills | `text-side`, `text-side-muted`, `text-side-faint`, `border-side-line`, `bg-side-hover`, `bg-side-selected`, `bg-side-foot` | | `--sidebar-*` |
| The violet scale (marks and tiles only) | `bg-violet-50` ... `bg-violet-700` | | `--violet-*` |
| Terminal | `bg-terminal`, `text-terminal-text`, `text-ansi-red` ... | | `--terminal-*`, `--ansi-*` |
| Diff lines | `bg-diff-add`, `text-diff-add-mark` ... | | `--diff-*` |
| Charts | `bg-chart-bar`, `bg-chart-bar-strong`, `bg-chart-grid` | | `--chart-*` |
| People and business marks (fill only) | `bg-identity-1` to `-8` | `--ws-1` to `--ws-8` | `--identity-*` |

Every text colour passes AA on the page, the card, the well, the raised surface and the violet tint; the pairs
are in the script's output. `text-disabled` is exempt, as disabled controls are, so never use it for something
meant to be read. On a selected row's tint, `text-faint` steps up to `text-muted` (the table does it).

### Violet

One hue, from the agent's pointer on the desktop. A scale (`--violet-50` to `--violet-950`) the roles pick from.

- **For:** the agent (its pointer and name tag on a live screen, an agent at work as a ringed dot, the
  `agent` colour), what is selected (a tab's word and edge, a checked box, a switch on, a pressed chip, a
  selected table row's edge), the keyboard focus ring, the primary button, the count of what waits for you,
  and the mark. One violet thing per row, at most.
- **Never:** a status (done stays green, needs a look amber, failed red), a large background or body text
  (600 and 700 carry words on white at 5.2:1 and 6.4:1; 300 is for words on night), decoration, a gradient on
  a screen (the wrapper tile and the avatar are marks, not screens), a link (ink, underlined), or a kind of work
  (tests are plum).

### Glass and the Dawn picture

The Dawn picture is the desktop's wallpaper (`src/assets/dawn/`, 18 KB and 6 KB WebP made from
`src/openajar/environments/desktop/wallpaper-1920x1080.jpg`). It is one picture, used in a few places, so it
stays special.

- **Yes:** the live screen and pictures of it in lists (`ScreenPicture`); Home's window, with white words on
  its dark side; behind sign-in and first run (`features/auth/layout.tsx`): beside the sign-in form with the
  day's demo in glass on it, as the page under one card (Check your email, a dead link), and as the band an
  invited person's welcome opens on; an empty state, as a small crop with one line under it. The setup wizard's
  step list is the night sidebar's sky (`.rail-night`), not the picture.
- **No:** behind tables, lists or forms; behind body text without a scrim; behind dialogs, menus or settings.
  At night it is dimmed by about a third (`--dawn-dim`), and it never animates.
- **Glass** (`--glass-fill`, a 22 px blur, `shadow-glass`) sits only on the Dawn picture or the stage: Home's
  cards on its window, chips on a screen's picture (`--glass-dark-fill`), the sidebar's foot. Never on a plain
  page, where it would only look grey. Under 768 px Home's cards leave the picture and are plain cards. A person
  who asks for less transparency (`prefers-reduced-transparency`) gets plain cards everywhere.
- **Words on the picture** sit in `.on-dawn`: white, and the roles inside it (a link's underline, the quiet
  words, a skeleton) are redefined for the night. A scrim from the left (`--dawn-scrim`) keeps them AA; the
  token script checks the lede over the picture's skies.
- **Cost.** The blur is checked on a CPU slowed six times: scrolling Home stays at 9 ms a frame at the 90th
  percentile with or without it, and nothing paints while the page rests.

### Type

Geist (`'Geist Variable'`) for words, Geist Mono (`'Geist Mono Variable'`) for numbers, ids, hashes,
commands and branches, with tabular figures. Use a **role**, which sets size, line height, weight and
tracking in one class; never pick a raw size for new work.

| Role | Size / line, weight, tracking | Use |
|---|---|---|
| `text-display` | 44/50, 600, -0.04em | Home's sentence. One per app |
| `text-page-title` | 34/40, 600, -0.032em | The h1 (`PageHeader` does it) |
| `text-page-title-compact` | 26/32, 600 | The h1, and Home's sentence, on a phone |
| `text-headline` | 32/40, 600, -0.035em | The sentence that opens a focused page: sign-in, a list's summary, an amount to pay |
| `text-heading` | 20/28, 600, -0.02em | A heading inside a wide page, a step's question, a figure on a card |
| `text-section-title` | 18/26, 600 | A section, a dialog title |
| `text-lede` | 17/26 | The sentence under a page title, in `text-muted` |
| `text-card-title` | 15/22, 600 | A card's head |
| `text-table-header` | 13/20, 500 | Column heads, in `text-faint` (the table does it) |
| `text-label` | 14/20, 500 | Buttons, tabs, form labels |
| `text-body` | 15/22 | Everything read |
| `text-secondary` | 14/20 | A row's second line, descriptions |
| `text-caption` | 12/16 | Timestamps, a status's word, help that is a footnote |
| `text-number` | mono 13/20 | Numbers in tables and lists |
| `text-stat` | 28/32, 600 | A stat tile's number |
| `text-code` | mono 13/20 | Ids, commands, terminal output |
| `text-code-caption` | mono 12/16 | A command or branch on a second line |
| `text-eyebrow` | mono 11/16, 500, +0.04em, caps | A kind's word and "IF YES". The one caps style on a page (a board's `silk` is the other, below) |
| `text-micro` | 11/16, 500 | Avatar initials and keys only |

The old scale (`text-11` to `text-32`) is gone: each became the role of the same size and line height,
and the design rules refuse it. Weights: 400, 500, 600. Nothing heavier.

### Space, radius, depth, motion

- **Spacing:** a strict 4 px grid. Half steps such as `gap-1.5` are banned and a test fails on them.
- **Radius, one per job** (the owner asked for less round cards than the mockup's 14; everything steps with
  10): 4 (`rounded-sm`) keys, small marks, checkboxes, menu items; 6 (`rounded-md`) fields, selects, a
  picture inside a card; 8 (`rounded-lg`) pictures, menus, popovers, rail items; 10 (`rounded-xl`) cards,
  dialogs, Home's glass cards; 14 (`rounded-2xl`) Home's window; full for every button, chip,
  count, segmented control, branch and the control pill. The wrapper tile is the dock's squircle at its own
  ratio (8 at 28 px, 10 at 36, 12 at 44). Dialogs are the one exception to the steps: 12, as the designer drew
  them, so a dialog reads as a layer above the cards' 10.
- **Depth:** hairlines, plus the card lift (`shadow-lift`: a 1 px shadow in light, a 1 px top light in dark) on
  cards, secondary buttons and tiles. `shadow-popover` and `shadow-dialog` for floating layers,
  `shadow-glass` for glass, and the primary pill's own top light. Selected edges, tab underlines and rings are
  borders or outlines, never box-shadow lines, so studio can see them.
- **Motion:** 100 ms for hover, 150 ms for most things, 200 ms for drawers and a picture fading in, on
  `ease-standard`; `ease-enter` for what arrives. Opacity and 2 px moves only. **Nothing moves all the time:**
  the agent's ring and the old pulsing dots (`animate-pulse-dot`) are still. The one thing that moves is the
  agent's pointer on a live screen, because it stands for the far screen's pointer.
- **Icons:** `lucide-react`, 16 to 18 px, stroke 1.75 (set once in CSS): the desktop dock's fine line.

## Kinds of work, and how they stay apart from status

| Kind | Mark (light / dark) | Icon | `kind` |
|---|---|---|---|
| Tests (`openajar run` suites) | plum `#7a2f7d` / `#c66cc6` | flask | `test` |
| Design (ui_check, ui_diff, a brand change) | pink `#c8337f` / `#f06bb0` | the check frame | `design` |
| Build and CI | teal `#0b6e88` / `#3fc6dc` | box | `build` |
| Dev server | blue `#1e40af` / `#4c7dff` | radio | `server` |
| Machine, push, post, payment, VM, lane, brand, email | none: a neutral tile | its own | `machine` ... |

A kind never reads as a status: different hues (status keeps green, amber, red, and the violet ring for an
agent at work), different shapes (a kind is always a rounded tile with an icon; a status is always a mark and a
word), and different places (on a card the kind sits left and on the 2 px top edge, the status sits right as a
pill). There is no fifth hue: a new kind shares its family's hue and leans on its icon.

## Components

Primitives live in `src/components/ui`. Use them; do not restyle them in a screen: a screen should not need
CSS of its own to look right. Every one is in the catalog, **`/$org/design`** (dev only, `src/catalog/`), in
every state and by group; open it in both themes at 390 and 1440 before you change one. How to build with
them, the groups and the checks: [DESIGN-SYSTEM.md](DESIGN-SYSTEM.md).

- **Button:** every button is a pill. `primary` is the violet pill with white words and its own top light, once
  per view, for the point of a screen (an empty state's next action, a dialog's confirm, Approve). `secondary`
  is the card with a firm hairline and the lift, and is the default. `ghost` for quiet actions in rows.
  `danger` is a red word on a hairline, never a red fill. `inverse` sits on an ink bar. `glass` sits on the
  Dawn picture or the stage. `light` (white, the stage's ink) is the one action in the control pill on the night. `sm` 30 (32 on a phone), `md` 36, `lg` 40. `shortcut="A"` shows the key and sets
  `aria-keyshortcuts`. Pass `loading` rather than disabling by hand. An icon alone uses `IconButton` (round),
  whose `label` is required.
- **Status:** `<Status status={...} />`, a shape and a word. Dot fine, diamond needs a look, square failed,
  a violet ringed dot an agent at work (still, never pulsing; `starting: true` draws it dashed and empty),
  hollow ring idle, quiet solid dot neutral. The word takes its colour only when it asks for attention (failed,
  needs a look, at work). Tones are `ok | warn | bad | live | idle | neutral` (`info` is the old name for live).
  Every enum maps to words and a tone in `src/lib/labels.ts`; add new ones there, never inline. `chip` sets it on
  its tone's soft tint as a 24 px pill, for a table's result column and the state at the head of one thing (an open ask, a run, a check).
- **Badge** (a 24 px pill, a soft tint per tone, or `solid`), **Count** (quiet, `accent` for what waits for
  you, ink, warn, bad) and **Chip** (a filter pill with `aria-pressed`, pressed is the violet tint with violet
  words; with `kind` its square doubles as the legend).
- **AppTile, the wrapper tile** (`tile.tsx`): the desktop dock's squircle, a gradient with a soft top light and
  a white glyph, in seven tones (violet, blue, pink, amber, green, ink, teal; `--tile-*`). For the app's own
  things: a kind of ask on Home, a channel, a settings section. Never for an action (a button is a pill), and
  never for a kind of work (that is `KindTile`). `red` is the danger zone's alone (what deletes for good).
- **KindTile, KindEyebrow:** the kind's tile (`xs` 18, `sm` 24, `md` 28, `lg` 32, `xl` 40 for a detail page's head
  such as one run or one check; `waiting` is dashed) and its word in mono caps. Beside an `xl` tile the eyebrow drops
  its own small tile (`tile={false}`). `Card kind="test"` adds the 2 px top edge.
- **BranchPill:** a mono pill, `main` in ink, full name as a title. **IfYes:** "IF YES" and one line of
  what a yes does, cut with its full text as a title.
- **Card, CardHeader, CardFooter:** radius 10, the lift, a head of 52 px with the title, quiet meta and
  the action, one hairline, no band. **FormCard** for settings. **Section** for a titled block.
- **StatTile, StatStrip:** the stat role in tabular figures, its unit in the tertiary ink; a strip of
  tiles in one card divided by hairlines.
- **Tables** (below).
- **Tabs** (words on a hairline, the selected one violet and semibold with a 2 px violet edge) and
  **SegmentedControl** (a view of the same thing: a pill well, the chosen segment the card; arrow keys move).
  The same well is drawn by `SegmentedTabList` (a page's few big views with a count on each, real tabs) and by
  `Segmented` in `charts/small.tsx` (a range or level switch, real radios). Three behaviours, one look: change one,
  change all three.
- **Dialog** (centred, 460 wide at radius 12, no footer band: the buttons sit at its foot on the raised surface;
  a bottom sheet on a phone), **Drawer** (the inspect surface), **SidePanel** (a picked row's detail beside the
  list from 1024 px, a Drawer under it), **ConfirmDialog** (with `typeToConfirm` when it cannot be undone, see
  "Forms"). The close button is last in the DOM so the first field takes focus; focus returns to whatever opened
  it (`useReturnFocus`).
- **Menu, Popover:** the raised surface, radius 8, items 36 tall at radius 4; a checked item's tick is violet.
- **Tooltip:** the inverse surface, caption size. Every cut line also carries its full text as a title.
- **Toasts** (`app.tsx`): the raised surface at radius 8 with the popover shadow, bottom right; at the top on a
  phone.
- **Banner** (`states.tsx`): one sentence about what is below it, on its tone's tint with its edge (bad, warn, ok,
  neutral), radius 10: the lead words semibold, then the sentence, and one or two buttons at its end (under the words
  on a phone). On the night band it takes the night's tints. A page opens with it as a `status`; a refusal beside a
  control is an `InlineError` instead.
- **Skeleton, EmptyState, ErrorState, QueryView:** an empty state is an icon in a small bordered tile,
  one bold line, one sentence, one action. `dawn` puts the small Dawn crop in the icon's place, for good news
  and a first use (`dawn="grey"` greys it, for "nothing right now"); it is the one way the picture appears in an
  empty state, so a screen never draws its own. An error says nothing changed and offers Try again.
- **PageHeader:** the page title role (26/32 on a phone), an optional `eyebrow`, the lede, actions on the
  right on the first row at every width. A detail page (one run, one check, one account, one workspace) passes
  `lead`, its 40 px mark (an `xl` KindTile, an account's site tile, a workspace's mark) beside the eyebrow and
  title with the actions level with its top, and `aside`, its state as a chip beside the title (`Status chip`
  for a job, `StateBadge chip` for an account). `titleHint` is a long title's full text on hover, and
  `className` sets the space under it (an account's head on the night band).
- **Field, Input, Select, Switch, Checkbox:** 40 tall, a firm hairline on the card; focus turns the edge
  violet with a soft violet halo (an outline); help is a secondary line in the tertiary ink; an error is a red
  edge and red words with a mark. Checked and on are violet with white. The switch is 38 by 22; off is the control grey,
  which holds 3:1 (the sheet's lighter grey would not). How they behave together is in "Forms" below.
- **ScreenPicture** (`components/screen-picture.tsx`), the picture of an environment's screen, and
  **PictureChip**, a chip on it: see "The screen's picture".
- **Kbd** (mono micro on the card), **Meter** (4 px, ink on the chart track, amber at 80%, red at 95%; `progress`
  for a bar where more is better, ink and green once complete),
  **CodeBlock** (a well with a copy button).
- **Terminal:** read-only output, dark in both themes (the stage's night). ANSI colours are parsed into text
  spans (never HTML). Line numbers, `highlight={{ 4: 'bad' }}`, follow mode, wrap. A light status tint never
  goes on it.
- **ImageFrame:** a page screenshot with findings outlined and numbered, boxes in the picture's own
  pixels; pins are buttons named "Finding 2: ..." when they can be picked; Zoom opens it large.
- **BeforeAfter:** a wipe on a range input, or the two side by side.
- **SelectionBar:** the floating ink bar under a table while rows are selected: "2 runs selected", the actions
  (`variant="inverse"`), Clear (Esc).
- **Slider:** a row of cards with the next one peeking; the pager ("1–3 of 5"), arrow, page, Home and End
  keys, a mouse drag and a swipe move it, and it snaps to a card.
- **Swatch, TokenChip:** a colour shown as data (marked `data-studio="ignore"`), a token name as a chip.
- **Logo, MarkGlyph:** the "Window a" mark, drawn from `--mark-*` tokens (a white tile, a violet a), and its
  one-colour glyph (`currentColor`) for small places such as Home's greeting.
- **Avatar:** initials in a circle; `me` (the person signed in) is violet to blue with white initials.
- **States for people:** `StateBadge` (`state-badge.tsx`) for screens a non-technical person reads: a
  shaped glyph and a word for All good, Waiting for you, Stuck, Sleeping and Working now (violet, with a still
  halo). `lib/states.ts` maps every condition to one with one plain sentence. `chip` sets it on its tone's tint as a
  24 px pill with the status mark (Stuck red, Waiting amber, Working now violet, All good green, Sleeping grey), for
  the state at the head of an account's card or page.
- **Words:** `src/lib/vocabulary.ts` holds the everyday words; `principalName` names who did something.
- **Technical details:** `useTechnical()` is one person's switch for ids, logs, the terminal and costs.
- **Identity marks:** `BusinessMark`, `PersonMark`, `SiteMark`, `AgentMark`: fills on small marks only.
- **Grids:** a bare `.grid` has a shrinkable `minmax(0, 1fr)` track (`app.css`); flex children that
  truncate still need `min-w-0`.
- **Focus:** one violet ring from `:focus-visible`, keyboard only (`lib/input-modality.ts`); a field shows it
  on every focus. Inside anything clipped, add `focus-inset`. On the night sidebar and the Dawn picture the ring
  is violet 300 (`--sidebar-focus`), which holds 3:1 there; on a terminal it is the ANSI blue.

### Tables

From `table.tsx`. Real `<table>`, no zebra, **no grey band**: the head is the table-header role in the
tertiary ink on the card over one hairline, sticky in its scroll box (`TableScroll`). Why: the band made
every table look like a spreadsheet and added a third grey.

- **A list page's table** follows the page's title and lede with no heading of its own (the lede already says
  what the list is): first one row of filters on the page (search first, at most 360 wide, then selects, all 40
  tall, then the states as a `SegmentedTabList` with a count on each, and the table's ⋯ menu at the row's end for
  row height), then the card, which holds only the table and its total line. On a phone the states scroll sideways
  in their row. A note about the list ("Routines ... are in What happened") sits under the card. A card's own head
  (`CardHeader`, 15/22) is for a table inside a page about something else (the last hour, a machine's runs).
- **Rows** 52 px (`density="compact"` 40, which hides second lines; `relaxed` 60), head 40, a hairline
  between rows, 20 px gutters at both ends.
- **Sort:** `Th sort="asc" | "desc" onSort={...}` makes the whole head a button, sets `aria-sort`, and
  shows the arrow on the label's far side so the label never moves. The sorted head is ink, the rest tertiary.
- **Figures:** `Td numeric` (and `Th align="end"`): right, in Geist's tabular figures at the body size, so
  "211", "51.4s" and "38 min" line up without looking like code. Units in the head (`Th unit="min"`).
  `<NoValue />` is an en dash; a zero is a zero, in `text-faint`; a count that asks for attention (failed) is red.
- **Money:** `Td money`: right, Geist Mono 13, tabular, so cents line up down the column. Ids, hashes,
  commands, branches and addresses are mono too (`CellTitle mono`, `text-code`), never figures or dates.
- **Dates and times** are words ("2h ago", "Yesterday", "20 Nov, 09:32") in Geist, left aligned, in
  `text-muted`, with the full date and time as the cell's title. A time is the first column when the table is a log
  (Audit log), the last when it is a list of things (Runs, Checks).
- **Status** in a table's result column is `<Status chip>`: its shape and word on the tone's soft tint, as the
  drawings set it (Running violet, Passed green, Failed red, Stopped early grey). Elsewhere it is plain `<Status>`.
  Never a filled row, never a dot alone. Counts by severity are coloured by it (blockers red, majors amber, minors
  ink, a zero faint).
- **Main cell:** `Td main` (or `mainCell`) takes the spare width and truncates; `CellTitle` gives it a
  title with its full text on hover and a second line.
- **Rows that act:** `Tr onActivate` takes a hover fill, a pointer, focus and Enter. `Tr selected` is the
  violet tint with a 2 px violet edge. Row actions go in `RowActions`, which fade in on hover and focus (always
  shown on touch). A checkbox column is the first, 1 px wide with no right padding, and folds away on a phone.
- **Selecting** shows the `SelectionBar` after the table: the ink bar at radius 10 with the popover shadow, sticky at
  the foot of the view (above the phone's tab bar), so choosing a row never pushes the table down. "2 runs
  selected", the actions as `inverse` pills, Clear (Esc). The count is said by a live region that is always in the
  page, so the first choice is announced.
- **0, 1 and many rows.** None at all: the empty state in the card (what would be here, the one next step), with no
  filters above it. None that match: `TableEmpty` keeps the head and says "No run matches" with Clear filters. One
  row: the same table (never a different layout for one). More than the node sends at once (200): the newest 200,
  and a footnote under the table that says so and where the rest are; no pager for a list a node caps.
- **Loading** is `TableSkeleton` (the real head and seven rows); **empty** is `TableEmpty` holding an
  `EmptyState`, with the head kept.
- **On a phone:** `Table stack` turns the rows into a list under 768 px: the main cell gets its own line,
  `Td phone="hide"` cells fold away, `Td label="Tests"` shows a label before its value. An older table
  may still use `hidden md:table-cell` and move values into the main cell; new tables use `stack`.
- Anything inside a fitted column that has no content width of its own needs one (`shrink-0` on a
  `Meter`, `w-max` on a bare `.grid`).

## Forms

Owned with settings (`features/settings/`), and the same everywhere a person types or chooses. Drawn in
`states-forms.png`, `states-dialogs.png` and the settings pictures. The parts are in `components/ui`:
`field.tsx` (Field, Input, AffixInput, CopyField, FieldError), `form.tsx` (FormActions, FormNote, useSaved,
SettingRow, SaveStatus), `choice.tsx` (ChoiceGroup, Choice, CheckCard, RadioRow), `card.tsx` (FormCard),
`dialog.tsx` (ConfirmDialog) and `components/unsaved-changes.tsx`.

**A settings section.** The page title "Settings", the section list on the left (208 px; groups Organization,
Fleet, Account; the open section is violet words, semibold, on the violet tint; the list scrolls inside itself
when the window is short and keeps the open one in sight), and the section in a column of 720 px at most:
`SettingsHeader` gives it a 26/32 title, one line under it in the secondary ink, and its main action at the top
right (the section's one primary, such as Invite someone or New grant). A phone gets the list of sections as a
card of rows, and each section has its way back.

**A form card** (`FormCard`): the title in the section role with one line under it, the fields, and a footer
band in the well with the note on the left and the action on the right. A wrapper tile goes before the title
only when the card is a thing of its own (a channel, the plan, the danger zone); field cards have none. `aside`
puts a switch or a button level with the title. `tone="danger"` is a red edge and a red band. With `onSubmit`
the card is a real form: Enter in a field saves.

**Fields.**
- The label sits above, the help below in the tertiary ink, one line if it can. The label is the field's name
  (`Field` ties label, help and error to the control); a card whose title already names its one field hides the
  label for the eye (`hideLabel`), never for a screen reader.
- Mark the few, not the many: a field that may be left empty says "(optional)" after its label. Required fields
  carry no mark and `required`, so a screen reader still hears it.
- An error is said under the field, in words that say what to do next ("Enter a full email address", "Give
  the organization a name"), with a red mark, a red edge and `role="alert"`. It appears when the person leaves
  the field or presses Save, never while they type, and goes as soon as they change the value. The help stays
  tied to the field while the error shows.
- Fixed words beside what is typed (an address's host, a unit, a currency) are an `AffixInput`: the words in
  the well, one edge round both. Numbers and money are tabular, with the unit after them.
- Something to copy is a `CopyField`: read-only in the well, selected on focus, and Copy beside it, which says
  Copied with a tick for a moment (and to a screen reader). A secret is never typed here: the node serves those
  forms, and `src/test/source-rules.test.ts` fails on a password field or a secret in state.
- Long values cut with an ellipsis and carry their whole text as a title.

**Saving a form** (`FormActions` and `FormNote` in the footer):
- Untouched: Save is the dim secondary, and the note is the card's own line ("Up to 80 characters"), or
  "Nothing changed yet".
- Edited: Save turns into the violet primary, Discard (ghost) puts the fields back, and leaving the page asks
  first (`UnsavedChanges`: "Leave without saving?", Stay or Leave; closing the tab gets the browser's own
  question).
- Errors: the note says "2 things to fix" in red; Save stays pressable and says the errors again.
- Saving: the button says Saving, keeps its look and cannot be pressed twice; the note says "Saving your
  changes".
- Saved: for three seconds the button is "Saved" with a green tick and the note "Saved at 10:42". No toast: the
  eye is already on the footer.
- One primary per screen still holds: when two cards are edited at once both Saves are violet, which is the one
  case two show, because each saves only its own card.

**A setting that saves on its own** (a switch, a select in a row): `SettingRow` puts its name (the control's
label, so a click on it works the switch) and one line on the left and the control on the right. It saves at
once, and a **toast with Undo** says so (`toastChange` in `components/ui/toast.tsx`, drawn in `states-forms.png`):
- The toast says what changed in plain words, never "Saved": "Quiet hours turned on", "Email digest set to
  Weekly", "Tomas Reyes's role set to Admin" (`switched` and `chosen` make the words).
- It carries one action, Undo, and stays about six seconds; it holds while the pointer is on it or it has focus
  (Undo is reachable by keyboard), and a moment more once let go. The stack is a polite live region, so a screen
  reader hears it.
- Undo puts the previous value back through the same save path, so whatever that path asks (a step-up, the
  node's own checks) it asks again. Then the toast says it is back ("Quiet hours turned off again"), or plainly
  that it could not be ("Could not undo, so it stays as it is." and why).
- Undo puts back only what that change touched, so a second change made since stays.
- The card's footer says only Saving and what went wrong, and that the last saved choice is back (`SaveStatus`);
  it no longer says Saved. A form with a Save button keeps its footer feedback (above) and no toast.
- **No Undo for what cannot be undone**: a deletion, a sent invite, a revoked grant, a signed-out device keep their
  confirm dialogs and a plain toast.
- **Undo never loosens what an agent may do without asking.** A change that let more go on its own gets Undo,
  which asks more again: Ask me first turned off, a worker turned on for an account, a weaker yes, answers in chat
  turned on, a higher worker limit. A change the other way gets the toast with no Undo: Ask me first turned on, a
  worker turned off (turning one on goes through its own dialog), a stronger yes, answers in chat turned off, a
  lower limit.
- Not settings and so not here: Technical details and the theme (they change only this browser's view), and a
  view, filter or tab (it saves nothing).

A toast is otherwise for what the page no longer shows (an address changed, a row removed, an invite sent).

**Choosing.**
- One of two to four short answers to a short question: `RadioRow` ("Format: CSV, NDJSON"), real radios side by
  side, arrow keys move.
- One of a few answers that each need a line: `ChoiceGroup` with `Choice` cards (radius 10, the radio at the
  right, or at the top right in a grid with `layout="stacked"`). The chosen card has a violet edge and a soft
  violet ring, never a tinted fill. "Works today", "Coming soon" and "Recommended" are a badge beside the icon
  or the title. One that cannot be chosen is dimmed to 70% and says why.
- Several that can all be on: `CheckCard`.
- A view of the same thing, or a pair such as "A business / Just me": `SegmentedControl`.
- More than five answers, or answers that come from data (workspaces, roles, computers): `Select`. An option that
  cannot be chosen now stays listed, `disabled`, with its `hint` saying why ("Offline").
- A grid of yes or no per person and place (workspace access): a tick per cell, a button with `aria-pressed`
  and a name that says both ("Tomas Reyes sees Aster Coffee"); what cannot change is a quiet tick with a title
  that says why.
- How strong a yes (What needs a yes): a four-step ladder per cell, each step a radio, so a click or the arrow
  keys move it; steps under a row's floor are drawn and not offered.

**Confirming** (`ConfirmDialog`). The verb on the button says what happens ("Change address", "Remove member",
"Delete for good"), never OK; Cancel is the quiet way out, or a clearer word ("Stay", "Keep it going").
- A plain question: the violet primary.
- A careful one (it stops something, or can be redone with work): the danger button, red words on a hairline,
  with an amber tile when it helps (`tile`).
- The dangerous one (it deletes for good): the red tile, the red edge, the person types the name ("Type
  Northwind Group to confirm", the field takes focus), and the button is the product's one red fill, dim until
  the name matches, with "The button wakes up when the name matches." under it.
- A removal that only takes something away from one person (a passkey, a device) still asks, in one sentence
  that says what still works afterwards.

**Keyboard and screen readers.** Tab reaches every control in reading order; Enter saves a form card and
submits a dialog's form; Escape closes a dialog or a menu and gives focus back to what opened it; arrow keys move
inside radios, choice cards, segmented controls and the yes ladder. Every control has a name, every error is
announced, Saved is announced (a status region that is always in the page, or the toast stack's live region).

## The shell

The frame every page sits in (`src/components/shell/`), drawn from Dawn's Full.

- **The rail** runs full height on the left, 248 px from 1024 px, icons only from 768 px, gone on a phone. It is
  the **night sidebar** in both themes (`.rail-night`): the night sky (`--sidebar-sky`) with a few still stars
  and the sunrise glowing at its foot. Inside it the usual roles (`--text-*`, `--line-*`, `--fill-*`, focus) are
  redefined to the sidebar's own white words, lines and fills, so whatever sits in it reads on the night with no
  night version of its own. From the top: **the mark and "openajar" alone**, **Search or run** (the command bar,
  with `⌘K`, on night glass), the menu, and at the foot the **status line**, the **Technical details** switch
  (its hint is the switch's tooltip and description) and **the organisation switch**.
- **The organisation switch sits at the foot**, as the best tools keep a workspace switch at the foot of the
  rail: a glass card (`bg-side-foot`, which keeps the words AA over the glow) with the org's mark (its first
  letter on a square in its identity hue), its name in semibold (cut with the full name as a title) and your role
  under it, and the up and down chevron. Its menu opens upward. On the icon rail it is the mark alone; on a phone
  it is the mark in the top bar.
- **A place** is 36 px, its icon 18 px at 1.75. At rest the words are the secondary white; hover is a faint
  white fill; the place you are on is white, semibold, on a white tint with a hairline.
- **The menu** is the simpler navigation the owner chose (2026-11-21, `nav-proposal.png`): ten places, the
  four everyone needs every day first, then grouped by who they are for, each place with its "g" key:

  | Group | Places |
  | --- | --- |
  | (no heading) | Home (G H), Needs you (G N), Computers (G C), What happened (G Y) |
  | Your business | Accounts (G A), Jobs that repeat (G O), Brand (G B) |
  | For developers | Runs and checks (G R), Site APIs (G I) |
  | (set apart) | Settings (G S) |

  A new place goes into one of these groups (`nav.ts`), with a key no screen acts on by itself. The key
  after g is always swallowed, so "g d" can never deny and "g a" can never approve. The old keys of the lists
  that became tabs still land (G V Environments, G M Machines, G E Email) but are not shown. An approver's menu
  stays Needs you, What happened and Settings, with no group headings and no status line.
- **Three places hold two lists**, as link tabs the shell draws on top of the list's page (`PlaceTabs`, the
  `Tab` words on one hairline, the open one violet): Computers is Environments and Machines (the agents' own
  computers and yours), Jobs that repeat is Routines and Email (sending mail is a job that repeats), Runs and
  checks is Runs and Checks. Each list keeps its own address and its own page, title and actions, so an old
  link opens the right tab, and every state of the page (loading, empty, refused) still shows the way to the
  other list. The place is lit in the rail on every page under it (one machine lights Computers).
- **Hidden until first used.** The two places For developers stay out of the rail and the phone's More until
  they are used, so a person who never starts a run never meets an empty developer page
  (`developer-places.ts`): Runs and checks shows once the organisation has a run or a check, Site APIs once
  it has a Site API, and either shows once the person opens it on this device (the command bar, a link, its
  address). Once shown it stays, remembered on the device. While the node has not answered it stays hidden
  rather than appear and vanish. The command bar, the g keys and the addresses reach both at all times.
- **Counts** sit on the right of a place: what waits for you as a violet pill on Needs you, runs going now and
  open environments as quiet numbers (the secondary white on the place you are on, so they keep 4.5:1). On hover
  or focus the place's keys take the count's spot. On the icon rail a dot stands for the number. Every count
  comes from `counts.ts`; a number the node cannot give yet is left out, never guessed.
- **The status line** says the machines in a few words ("2 online · 1 not answering") beside one bar per
  machine. It opens the system popover: the worst thing first as a sentence, then each machine, the AI and
  today's AI usage, with Freeze for whoever may freeze. On a phone the same popover opens from a dot in the top
  bar.
- **The top bar** covers the page column only, on the page with no edge (it blurs what scrolls under it): the
  breadcrumb on the left (the workspace switch, a slash, the place, then the page's own name, which a page can
  set with `usePageCrumb`), and on the right a Frozen chip while a freeze holds, **Live** while the event stream
  is open, the keyboard list, the theme (one press between light and dark; System stays in the account menu),
  what needs you (the bell, with a violet dot) and the account (your initials, violet to blue).
- **The page frame** is 1200 px at most with 40 px gutters from 1024 px, so Home's window spans 1112 px at 1440.
- **No clock.** The browser's clock is not the machine's, and a number that changes every second pulls the
  eye from the one thing allowed to move.
- **On a phone** the top bar holds the org's mark, the workspace switch, search, the status dot, the bell
  and the account; the tab bar (the page, blurred) holds Home, Needs you, Computers and What happened, with
  the place you are on in violet, and More holds the rest (the developer places only once used): a sheet from
  the bottom that stops above the tab bar (which stays in reach; More again closes it), the places as tiles
  three to a row in the rail's groups (Your business, For developers, then Settings) with the one you are on in
  violet, and the sidebar's foot at its end on the night (the machines' line and the organisation switch, with
  their words).
- **The command bar** lists every place with its keys (with nothing typed, the six most used; typing finds
  the rest, hidden developer places included, and the lists inside a place by their own names: "machines"
  finds Machines in Computers), then what features add, the workspaces, accounts once
  you type, and the actions. A feature adds its own rows by exporting `commands` from
  `src/features/<it>/commands.ts` (see `commands.ts`); it never edits the shell.
- **Addresses.** The places kept their addresses when they were renamed and joined (2026-11-21), so no link
  breaks: What happened is `/history`, Computers opens on `/environments` with Machines at `/machines`, Jobs
  that repeat opens on `/routines` with Email at `/email`, Runs and checks opens on `/runs` with Checks at
  `/checks`. Inside the app the routes keep their engine names (`activity`, `nodes`, `flows`, see
  `app/paths.ts`); the old `/activity`, `/flows`, `/nodes` and `/computers` links open the same pages. Each place's params live in its own folder
  (`features/<place>/search.ts`), so a screen adds a param without touching the router.

## Home

`features/home/`, Dawn's Full. Three answers in the first five seconds.

- **The window** (`home-hero.tsx`): the Dawn picture (mirrored, so the sun rises on the right, away from the
  words) at radius 14, with the greeting and date (and the workspaces) by the mark glyph, **the sentence**
  (`sentence.ts`, real data only) in the display role in white, and the line under it with its links. Under
  768 px the scrim runs top to bottom and the sentence is the compact title.
- **What needs you** sits on the window's lower edge as glass cards: the heaviest three (`waitingOrder`), each
  with its wrapper tile (a kind of ask: money amber, a push violet, a post pink ...), its title, what a yes does
  ("IF YES"), who asked and when, and one action, Look, which opens it on Needs you. The heaviest card's Look is
  the screen's one primary. Home never decides on one click: the arming delay, the second step for money and the
  irreversible, step-up and Undo live on Needs you. "See all 30" follows when more wait. Nothing waiting is one
  calm glass card ("Nothing needs you"); while it is read, three glass skeletons.
- **Working now** (`working-now.tsx`): each open environment as a card with the picture of its screen, the
  status chip and the time left on the picture, the title, and who works in it with what it is doing
  (`ENV_DOING`). The meta says how many and what they cost so far. Nothing at all when the node has no
  environments to say; a calm card with a small Dawn crop when none is open; skeleton cards while loading; an
  error card with Try again.
- **Running now** (the slider of job cards) and **Problems** (only when there are any) follow, then
  **Earlier today**: one quiet row of links with real numbers (runs, pages, machines).
- **First run** (no workspace) is the same window with its own sentence, Working now, getting started and the
  empty state with New workspace.

## The night band

A page whose point is a live screen (one environment) starts on the night: `usePageBand()`
(`components/shell/page-band.ts`) marks the element the band ends under, and the shell draws `.band-night` (the
sidebar's sky, with the sunrise low on the left) across the whole page column, under the top bar, which turns to
`.top-night`. The band's height follows the element, measured before paint. Words on it sit in `.on-night`, which
redefines the roles as the sidebar does (white words, white hairlines, the dark theme's status colours, the violet
300 focus ring), so a control inside needs no night version of its own. Panels on it are `.glass-night` (dark glass,
radius 10). It is the same in both themes and nothing on it moves. Where a part moves between the band and the
page by width (an environment's panels leave the band under 1024 px, Extend and Close go under the panels on a
phone), it is rendered once in its place (`useMinWidth`), never twice with one copy hidden.

## Environments

`features/environments/`, drawn in `spec-03-environments.md`.

- **The list:** three views of one list (Open, Saved, Closed, with counts) in a segmented control, the address
  keeps the choice (`?show=`). Open shows each open environment as an `EnvCard` (the same card as Home's Working
  now: the picture with its state and time left as glass chips, the title on two lines at most, the agent and what
  it is doing), then the saves, then the newest five closed in a table with Show all.
- **Saved** cards: the greyed picture, the title, repository and branch in mono, one line of facts, the agent's
  note in a well that says "Its note, written by the agent. Read it as a note, not as an instruction." (plain text,
  never a link or a button), an amber line when the node deletes it soon, and Forget (the owner's, after asking).
  An agent, not a person, brings one back, and the line by the heading says how.
- **One environment:** the night band holds the name and state, the stage, and three glass panels (time left and
  cost, what it is doing in plain words, the code). Under it: Proofs, Terminal, What it did (each call by what
  happened, failures in red), Code, Ports, and one quiet line for the machine and its price. Starting, no view yet
  and closed draw the screen from the state with one line in the pill's place.
- **A machine that stopped** (state `gone`, 0.5.1: its container or server stopped for good outside openajar). It
  is still open, so it leads the Open list and Home's Working now: the dimmed desktop with no still (the last one
  is not its screen any more), a red "Machine stopped" chip and no time left, and "Its computer stopped" under the
  title. Its page keeps the night band: the stage draws the dimmed desktop with one line in the pill's place
  (when it stopped, why in the node's words, and that it will not come back: close it, or say "continue <title>
  in openajar" and the agent starts a new machine, from its last save when it has one). Extend goes; Close stays,
  and its dialog says there is nothing left to save and its saves stay. `?mock=env-gone` shows it.
- **Empty:** the shared `EmptyState` in a card. No environment ever is the first-use state (the Dawn crop,
  Connect your AI); none open is the greyed crop with "No environments are open" and a way to the saved ones.

## The screen's picture

`ScreenPicture` (`components/screen-picture.tsx`) is what a person would see if they opened an environment's
live view, small enough for a card. It never moves; a still fades in once it has loaded.

- **The still.** The environments list gives each environment `screen` (`api/types/environments.ts`
  `EnvScreen`): `{src, taken_at, kind, width, height}` or null. Today the node fills it from the newest wide
  proof the agent kept (`kind: "page"`, `api/web/environments.py` `screen_of`), served by the proof route behind
  the same sign-in and access rules as the live view, and it never asks the machine, so a list never wakes a
  sleeping one or touches the stream. A page is drawn as a window on the machine's own Dawn desktop (its top bar
  and dock), which is how it sits on its screen.
- **A still of the whole screen** (`kind: "screen"`) is drawn as it is, edge to edge. The node keeps one per
  environment (`openajar/environments/stills.py`): the desktop read with `screen_snapshot`'s own reader, 640 px on its
  longest side, held in memory, taken at most once a minute and only while a page that shows the pictures is
  open (the list and Home read the list with `pictures`, which sends `screens=1`; the rail's counts do not), never
  for a machine that is not up and awake, never while a person has control of that screen (a still from before a
  take over is dropped), and never counted as the agent's use of the machine. It is served at
  `/environments/{id}/screen.png` to whoever may watch that screen's live view. Without one, the page proof above.
- **Without a still** the picture is drawn from the state: Starting shows the desktop with the title and
  "Starting its computer"; Asleep (closing, closed, failed) the desktop dimmed; anything else a terminal window on
  the desktop.
- **Chips on the picture** (`PictureChip`) are dark glass with white words: the status with its mark in its
  night colour, and the time left.

## Mono rules that still hold

The 4 px grid; one token source and no Tailwind palette; hairlines and one lift; a shape and a word for every
status; the kinds of work as tiles with icons; tables without a grey band; terminals dark in both themes; the
one caps style; sentence case; plain words; no em dash; no clock; the states below.

## The instrument layer

This is a computer, and the panel should feel like looking into a machine room you own. Calm
comes from hierarchy, not from showing less. The layer lives in
[`src/styles/instrument.css`](src/styles/instrument.css), on top of everything above.

- **The current.** `--current` (class `live`) is the agent's violet in Dawn (it was pink, then ink in Mono), and it
  means exactly one thing: an agent is acting right now. It is never decoration and never a resting state, so the eye
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
  board, `Heartbeat` on a node. With `text-eyebrow` it is one of the two caps styles. Never for a sentence.
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
  answering), doing now (the running `openajar run` command with its kind tile and who started it,
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
  another (`openajar vm setup`, `openajar connect root@my-server --install`,
  `openajar controller join-token`). Mock it with `?mock=one-machine`; twelve rows with
  `?mock=machines-many`.

### Node pages

About This Mac, Activity Monitor and Console in one. The pattern for any deep object page:

- **The head, as drawn (`machine.png`).** `PageHeader` with the machine's neutral `xl` tile, its name with its
  state as a chip, one plain sentence generated from data (`describeNode`: "Runs 18 accounts for ..., holds the
  anthropic-api brain, and has 3 schedules that fire 2 times a day") and one line of facts in plain type (system,
  cores, memory, its time). Freeze is the page's action; Remove machine waits in the ⋯ menu beside it.
- **About it and Load** sit beside the tab's content from 1280 px (under it before that): the state, the last report
  (or "Away since", in red), up for, size, what it runs and its time; then busy, memory, disk and warm accounts as
  meters, and how often it was away in 30 days. The tabs that need the whole width (Schedules, Logs, Installed,
  Resources) take it, and the two cards step aside. Nothing in the head moves: the old heartbeat strip and its beat
  are gone, and the Resources tab draws load over time.
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
- **An environment's screen is a stage** on the night band (`ControlFrame` and `LiveView` with `stage`,
  `lane/stage-frame.tsx`): the screen is a picture (`rounded-lg`, a faint white edge), and one row under it: the
  **control pill** (`.control-pill`, the stage's night in both themes) says who has control by name ("Claude Code
  has control", from the environment's agent, never a guess) with its one action as the white `light` button (Take
  over, Hand back, T on the keyboard); then the screen's own tools (the keyboard field while it is yours, moving
  the picture once zoomed, back to fit while zoomed); full window; and **⋯**, which holds Stop agent (rare, final,
  and it asks first). On a phone Full window joins Stop agent in ⋯ so the pill keeps its words. While it is yours
  the picture gets a 2 px white outline outside a 2 px gap, the pill says "You have control since 19:22" and the
  agent's pointer goes. Someone who may only watch reads "Claude Code has control · you can watch" and has nothing
  to press. The agent's pointer is one size (`POINTER_SCALE`) whatever the picture's scale. An account's page uses the
  same stage; the login queue keeps the bar over the view.

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

### Accounts

`features/home/accounts.tsx` and `cards.tsx`, drawn in `spec-09-accounts.md`.

- **The list:** the states as a segmented control with a count on each (All, Need a hand, Working now, Sleeping),
  the workspaces as chips, then each workspace as a heading (its mark, its name as a link to the workspace, how many
  accounts) and its accounts as cards in three columns (two from 768 px, one on a phone).
- **What needs a look is said once, above the cards:** one banner per workspace, red when an account is stuck and amber
  when one waits, in a sentence with that account's one action ("Reddit is switched off for safety: it is signed in as
  someone else.", See what happened). Two or more are counted, and each card says its own.
- **A card** is the site's mark on its tile, the name and the handle (mono when it is a handle), the state as a chip,
  one line on how it is, what it did last, the Ask me first word, and the one action when there is one (Sign in, Do
  the check, See what happened, Watch). Stuck has a red edge, waiting an amber one. The whole card opens the account.
  No picture: the account's screen is on its page. The Ask me first switch is on the account's page too, where a
  person has read what they change; the card only says the word.

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
- **It opens on the night band, as an environment does** (`spec-09`, `account.png`): the site's tile, "Aster Coffee ·
  Instagram" with its state chip, one line of facts, and Put to sleep, Freeze and Technical details at the right. Then
  what holds it, each as a banner across the band (missing, frozen, a check or a hold, red when it is stuck; "Asks first
  for now" in amber with Clear it), then the live screen as a stage with the control pill under it (who has control by
  the agent's name from the account's claim, Take over, full window, and Stop agent in ⋯), and beside it three glass
  panels: who is using it with what waits for a yes, the last five things done, and whether it is safe to use. Under
  1024 px the panels are cards under the band.
- **Doing now, then needs you, then what it did.** The page reads in that order: the live view, who is using it and what
  waits for a yes, then What happened. Pace, limits, sites, sign-in upkeep, files, watch link, standing approvals and routines
  sit behind one closed Settings disclosure under the band, so every one is reachable and the page stays calm.
- **The state is the same everywhere.** An account's word and sentence come from `lib/states.ts` (a failing check makes it
  Stuck, "Not safe to use right now"), so Accounts, the business line, Machines and this page agree.
- **What happened names what the person did.** The owner's own changes (who has control, a freeze, a pace, limits, worker and
  outside-text choices) appear in the account's What happened panel in their words ("You froze it"). Reads of the page are not history.
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
- **A routine's page leads with the job** (`routine.png`). Its business mark, its name as the title and its sentence
  as the lede, where each phrase lights its step. Change steps and Run now (the page's one primary). Where it stopped
  is a red banner with See what happened. Then the steps as a column in one card (Starts, Does, Decides, Tells, each
  with its tile and what it came to last time; the failed step's tile has a red edge), with Ask me first as one
  quiet strip at the card's head; beside it, what it really made as rows (the amount, what it was, Checked or Not
  checked) and its runs. Technical facts appear only with Technical details.
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

- **One Site API's head** (`site-api.png`): the violet braces tile, its name with its state beside it, its sentence,
  and one line of the business, the site in mono and its calls this week; Pause is its action.
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
  and paused are quiet dotted wires, needing a person veils the page, and a read in progress runs the current (violet) down
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
  short way: You, your computer, your mail, with openajar Cloud to one side and no wire a password could take.
- **The computer's form is a drawing.** It stands outside the panel at `/computer-form`, says plainly that a
  computer serves it, and its box is dots with no input behind it. A test keeps it that way.
- **One picture says every state.** Looking runs the current (violet), the password is a key that moves, and a failure breaks
  the exact wire: read, send, or the door. Each failure has plain words, says nothing was saved, and gives one
  next step, usually a note a person can copy for whoever can fix it. A certificate that does not check out
  has no way past it.
- **Jargon waits to be asked for.** "Found Gmail", then Show the server names. A low-trust result is the one
  place names lead, because a person is being asked to judge them, and the part that differs is marked.
- **Never imitate Google.** Steps are our own words with a link to Google's own page. Its mark only names it.
- **A mailbox is a card** (`email.png`). Its business mark, the address (cut, whole as a title), the business and
  provider, its state as a chip, then sent today against the daily limit as the shared `Meter` with what is planned
  in words, what came in today (a link to New mail), the routines that use it and the computer it runs on. One in
  trouble comes first with a red edge, what happened in plain words and its one next step, which is then the page's
  primary. The page is titled Email with one sentence under it, and the three views follow as the segmented well.
- **New mail** (`email-new-mail.png`) is one card of rows, answers to your emails first: who, a chip for what kind
  (Reply, Unsubscribed, Automatic), the subject and its first line, and the time. The open one is selected and read
  beside the list. A mailbox that cannot be read is an amber banner above it. The drawing's held-back message (mail
  that tries to give orders to an AI) needs a field the API does not send, so nothing marks one yet.
- **A stranger's words have their own edge.** `stranger` is a hatched margin with Written by someone else. Mail
  is text, a picture is a hatched note that says what was not fetched, a link is its text and address and cannot
  be pressed, and an attachment is listed as not opened. What openajar itself says never sits inside that edge.
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
- **No timeline of its own.** What a worker did sits in What happened (its steps in the story, "with the browser
  worker" on the row), on the account (one quiet strip, only while it has something to say), and in Needs you when
  it asks (how it got there, above the question). The Workers page shows one thing, now or last, and links on.
- **The step line is the signature** (`step-line.tsx`). One thing done, a tick for each step. The rows are who
  thought, in the same order as the jobs, so most of it stays on the top row and a step that needed help falls out of
  it, as far as it had to go, the way a stop falls on What needs a yes. It is step by step where that is a day.
- **The rail is the limit.** It is as long as the most steps allowed on one thing, so all step lines share a scale
  and a step past the limit cannot be drawn. The current (violet in Dawn) is the step being taken now and nothing else: done, asked and
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
  what it will do: the strip above and What happened say what it did.
- **Its posts have no switch of their own.** They go by the account's Ask me first, and only that strip names the worker,
  only while the worker works there. So `NEVER` and the step words never promise a yes on an account that runs on its own.
  And a post that follows a page asks on a computer that says so (`after_a_page_asks`, the default), which the worker
  always does, so there "Runs on its own" still reads as asking you. Never say a post goes alone until it can.
- **Limits are steps, not money. Tiles use the usual rows, the title is the answer ("Inside its limits today"), and
  the meter colours are the usual ones. Money, model names and milliseconds wait for Technical details.
- **Only what can do a job is offered.** A service that never writes is not a choice for Writes text.
- **The hosted panel holds no key, ever.** No field, no type, no handler, and a test reads the source. Adding a service
  asks who (the services as radio cards, the first chosen to begin with, "On a computer" on one already added) and which
  computer (a select; an offline one is listed and cannot be chosen) on one page, with Cancel and Continue on the
  computer at its foot, then hands over as Email does: `key-route.tsx` shows You, the computer and the service
  in a line with openajar Cloud to one side, and the form at `/computer-form/ai-key` is a drawing. A computer with no
  screen says so and shows its one line. Otherwise `openajar brains add` waits behind Technical details.
- **AI models answers first too.** One sentence on how many are ready, then each service as what it is for, where it
  is kept and what uses it. The try order and its arrows are for agents, so they wait for Technical details.
- **What is coming is said calmly.** The Android and desktop workers are a line of words at the foot: no button, no
  dead switch, and what each computer lacks.

### Proof, in What happened

The record behind a story (`features/activity/proof/`, words in `words.ts`). One account's day is replayed step by
step, and each step says in words whether it is signed, not signed yet, from before the log began, or changed.
"The log is intact" is said only when the node checks the whole log, and only the owner is asked. A read-only link
(`/shared#token=`, `features/shared/`) opens with no sign-in and no app around it. It shows what the link allows,
with no controls and no arguments, and says plainly when the link is not valid. A token is shown once, never listed.

- **The page** (`proof.png`): Back to What happened above the title and Share this day beside it, whether the log is intact as a
  banner (green, red when a row no longer matches, quiet when it cannot be checked), the day as a card with its account
  and day controls in the head and the result and the record as chips, then the read-only links as a card.
- **Sharing and checking are the owner's.** The node refuses anyone else, and the screen never asks them.
- **Stopping a link is one confirm.** It stops at once, and the list says what each link did (opened, ended, stopped).

### Work handed to a model

What an agent or worker gave an AI model is listed under AI models for the owner (`features/workers/handed-to-models.tsx`).
It reads its own state again every few seconds while anything is still going, and stops once nothing is. Stopping asks
first and says what stays done. Carrying on is offered only where the node can resume the task.

### Overrides, the business queue and devices

- **Overrides** (a safety check a person turned off, and why) show on an account's Ask me first, under Technical details,
  to the owner alone.
- **A business** (`/workspaces/$ws`) lists its accounts for anyone who sees it, as small cards (the site's tile, the
  handle or what is wrong, the state chip). The logins still to do are the owner's: a red banner per account to look at
  (Open the site, Open the account) and one amber banner for those still to log in, with the login queue.
- **The login queue** (`/logins`) is two columns: how far along it is (a `Meter progress` per business, green once
  complete) above the logins to do, and the open one as a card with its own browser and the promise ("openajar never
  sees it") with Sign in as the one primary. Under 1024 px the list folds away and the open one stays.
- **Signed-in devices** of a member show to an admin, from Members, and an admin can end one after a confirm. Their own
  devices stay on their profile.

### How a person says yes

Most things never ask. This is about the few that do (`features/confirmations/`, words in
`lib/confirm.ts`, the rules in docs/CONFIRMATIONS.md). Concept stage: on the mock only,
until the owner has steered it.

- **Words, never levels.** A person reads On its own, Just a tap, A tap in openajar and Prove it's you, each
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
- **A yes on the record says who, where and how strong.** A yes in chat reads like any other, and says openajar
  wrote the question and the agent could not answer it.
- **The agent never holds the pen.** In the chat concept the words are openajar's own and are never a message.
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
- **Empty:** an icon (or the Dawn crop, see `EmptyState`), what would be here, and the next action. Never "No data".
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

- The rail is the four groups in "The shell": Home, Needs you, Computers and What happened first; Your business
  (Accounts, Jobs that repeat, Brand); For developers (Runs and checks, Site APIs, each hidden until first used);
  and Settings. A new place goes into one of those groups.
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
| Keep violet for the agent, what is selected, focus and the primary | Use violet for a status, a link, a large surface or decoration |
| Put glass and the Dawn picture only where "Glass and the Dawn picture" allows | Put glass on a plain page, or the picture behind a table, a form or a dialog |
| Keep a page still at rest | Pulse, breathe or loop anything (the agent's pointer is the one thing that moves) |
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
