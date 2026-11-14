# Style take: style-glow (W3)

> The prototypes under `panel/prototypes/` were removed with the classic panel on 2026-11-14. They are
> in git history (commit 59634500 and earlier); the commands below run only on such a checkout.

Status: design, 2026-11-13. One of two style takes on the approved app design
([app-design.md](app-design.md)). The owner said the app's style is dated and asked for something
"cool and modern" like Vercel or Resend, a colour accent per kind of work, better tables, a better
Needs you, and components in Brand. This take is dark first and atmospheric: near-black surfaces
lit by soft light, translucent edges, Inter with tight tracking, softer corners. The light theme
is designed on its own terms, not inverted.

The prototype is
`panel/prototypes/dashboard/style-glow/index.html`:
plain HTML, CSS and JS, no build step. Open the file, or serve `panel/prototypes/dashboard/`. The
structure and UX of app/ are kept (Home with the slider, Machines on one screen, the run page, the
check page). It follows the system theme until you pick one with the switch or `?theme=dark`.

| File | What it is |
|---|---|
| `style-glow/app.css` | The style. Every value in agentos's own interface is a token from `style-glow/tokens.css`. |
| `style-glow/app.js` | app/'s screens, plus the kind device, the new Home blocks, the Needs you inbox, the table features and Brand's components. |
| `style-glow/data.js` | app/'s Acme Shop workspace; each request now says what happens on yes. |
| `style-glow/build-tokens.py` | A copy of the token builder with this take's values, the kind colours and a colour-blind check. The shared `../build-tokens.py` is untouched. |
| `style-glow/fonts/` | Inter (variable, with optical sizes) and JetBrains Mono, plus DM Sans for the drawn shop. OFL. |
| `style-glow/shoot.py` | Images of every screen and state. |

## 1. What I learned from the references

I opened vercel.com, vercel.com/geist (introduction, colours, table, button, badge, empty state) and
resend.com in a browser I drove, took screenshots in dark and light, and read the computed styles.
What I took is the craft, not a layout or a mark.

**Surfaces are layered by one or two steps, not boxed.** Vercel's dark page is `#000`, a card is
`#0a0a0a`, a hairline is `#1f1f1f` and an input edge `#2e2e2e`. Light: page `#fafafa`, card `#fff`,
hairline `#ebebeb`. Resend's page is `#000` with text `#f0f0f0` and secondary `#a1a4a5`. Neither
uses grey header bands or thick borders; depth comes from a step in lightness and one hairline.

**Type is confident and tight.** Geist's page titles are 40/48 semibold at -2.4 px (-0.06 em),
section titles 24/32 at -0.96 px (-0.04 em), body 14/20 at normal tracking. Resend's h2 is 56 px at
-0.05 em. Big type is tracked hard; small type is left alone. Numbers are tabular, in the sans.

**Tables have no band.** Geist's table head is 14 px weight 500 in the secondary grey on the card
itself, with one hairline under it. Rows have no borders; a row lifts on hover with a rounded fill.
Numbers right aligned. It reads as a list you can scan, not a spreadsheet.

**Buttons.** Vercel's app buttons: 6 px corners, the primary is solid ink (white in dark), the
secondary a 1 px edge on the surface. Resend's are softer: 12 px corners, a translucent fill and a
lit edge. Marketing buttons go full pill.

**Light explains.** Resend's hero lights its object from behind; the selected item in its SDK row
gets a violet glow and border, the others stay dark. Vercel's hero is one radial light. Neither
paints colour on surfaces; colour arrives as light on an edge or behind a thing, and only where
something is chosen or happening.

**Badges** in Geist come in solid and subtle pairs per hue, full radius, 12 px weight 500. **Empty
states** are a quiet icon tile, one sentence, one action.

**Motion** on both is slow and soft: things rise a few pixels and fade, nothing bounces.

## 2. The style in five rules

1. **Colour is light, not paint.** Surfaces are neutral steps. Colour appears only as a lit edge (a
   card's 2 px top line), a glow behind something that matters (a focused card, a failure on its
   left edge), a tile around an icon, or a progress fill. Never a coloured card background.
2. **Two meanings own all the hue.** What kind of work a thing is (`kind.*`: a tile with its icon and
   its word) and how it is going (`status.*`: a shape and a word). The accent itself is ink: what is
   selected, focused or primary is simply the brightest thing on a dark screen (darkest on light).
3. **Edges are translucent and lit.** Every hairline is white or black at 7 to 14% so it reads on any
   surface and through glass; cards, buttons and keys carry a 1 px highlight along their top
   (`line.highlight`). In light, depth is a soft shadow (`shadow.card`); in dark, the lit edge.
4. **Type is Inter, tight when large, plain when small.** 40 px display at -0.045 em for Home's
   sentence, 28 px page titles at -0.035 em, 14 px body at -0.008 em, 12 px heads and captions at 0.
   Numbers in Inter's tabular figures; mono (JetBrains Mono) only for code, commands, branches, ids.
5. **Corners soften with size; motion explains.** 6 px for small marks and keys, 8 for controls, 14
   for cards, 20 for sheets and the command bar. Motion only for arrival (rise 6 px and fade), live
   (a breathing ring) and hover lift (2 px). Reduced motion turns all of it off.

## 3. The accent for each kind of work, and how it stays apart from status

| Kind | Where | Hue | Tokens (light / dark mark) |
|---|---|---|---|
| Tests | `agentos run` with a test runner | cyan | `kind.test.*` `#07799a` / `#19a9d1` |
| Build | builds, lint and types, CI, a push to main | blue | `kind.build.*` `#2148d0` / `#3d6dff` |
| Dev server | `agentos expose`, a service lane | lilac | `kind.server.*` `#9b62f5` / `#cdb4ff` |
| Design | `ui_check`, `ui_diff`, a brand proposal, Checks and Brand | pink | `kind.design.*` `#d42a82` / `#f569b0` |
| Machine | nodes, VMs, new lanes, Machines | steel | `kind.machine.*` `#7d8899` / `#8e9db3` |
| Outside | posts and payments | none (neutral) | `kind.outside.*` |

Each kind has four tokens: `text` (4.5:1 on every surface and on its own fill), `mark` (3:1 on card
and page), `fill` (the tile) and `glow` (the light, decoration only).

**The device** is one thing used everywhere: a 20 px tile with the kind's icon in its hue, then the
kind's word (`.ktag`), or the tile alone in a list (`.ktile`). A job card adds a 2 px top edge in the
hue and a faint glow under it that brightens on hover and focus; its progress bar fills in the hue.
It shows on the slider cards, the Runs list and table, the run, check and Needs you pages, Home's
Needs you and Problems rows, the "Earlier today" links, the rail (Checks pink, Machines steel) and
the slider's filter chips.

**Kept apart from status, four ways.** (1) Hues: kinds avoid red, amber and green entirely, which
are status. (2) Shape: status is always a mark (dot fine, diamond needs a look, square failed,
breathing ring live, hollow ring idle); a kind is always a rounded tile with an icon, never a dot.
(3) Place: on a card the kind is the top edge and the label; failure is a red flag on the picture
("2 failed") and a red segment in the bar. A failing test card keeps its cyan edge and gains a red
flag, so "this is a test run" and "this failed" never share a device. (4) Words always travel with
both. Posts and payments get no hue on purpose: the orange I tried sat on top of amber warnings for
people with protanopia and tritanopia (below), and they are not the work people watch.

**Colour-blind check.** `build-tokens.py` now simulates protanopia, deuteranopia and tritanopia
(Machado 2009, full severity) and fails if two kind marks come closer than ΔE 8 or a kind mark
comes closer than ΔE 12 to an ok, warn or bad dot, in both themes. My first values failed 5 of 8:
blue builds and violet dev servers merged under protanopia (ΔE 3.3), and orange "outside" sat on
amber warnings. Builds went deeper, dev servers lighter (so they part by lightness when hue is
lost), tests darker, the outside hue was dropped. Now the nearest pair of kinds is ΔE 9.3 (light,
protan, tests and design) and the nearest kind to a status is ΔE 13.1. All 8 pass, and all 130
contrast pairs pass AA.

## 4. The four weak places

### Home: scannable in two seconds

Image: `shots/home-screen@1440-dark.png` (Home, dark).

- **The sentence** is 40 px display type, tracked tight, lit from above (it fades from ink to a
  softer ink) over a faint field of dots that fades out. This is the screenshot moment.
- **The slider** keeps app/'s content and behaviour. Each card now says its kind in the first place
  the eye lands (the coloured top edge), then the tag, then the title at 16 px. Failures moved from
  the top edge (which is the kind's now) to a red flag on the picture.
- **Needs you** became a block with its count in large figures ("6"), each row a kind tile, the
  question, and **what happens on yes** in one line ("2 commits to main, lint and tests passed", "At
  most €0.02, deletes itself in 2 h"), with Deny and Approve on the row. "Decide all" opens the inbox.
- **Problems** has its count in red. Each problem is lit from its left edge by its status (red
  square for a failure or a blocker, amber diamond for a machine not answering), says its status and
  its kind side by side, shows expected and received as a two-part chip, says **who is on it** (with a
  live mark when an agent is), and has one way in.

### The Needs you page: an inbox of decisions

Image: `shots/needs-push-screen@1440-dark.png` (Needs you, a push).

The queue on the left, the request you are deciding on the right, at `#/needs/<id>`. Each request
shows **what happens if you say yes**, drawn for its kind:

| Request | Preview |
|---|---|
| Push to main | The branch's commits on a rail into main, each with its +/−, the move from one commit to another, the checks that passed, and that a push is undone only by another push. |
| Rent a VM | The most it can cost in large figures, the rate, the size, a bar to the minute it deletes itself, and the month's VM budget. |
| New lane | The sandbox (CPUs, memory, no network, which machine) and its first command as a terminal line. |
| Brand change | The real product page at 360 px before and after, the failing badge outlined red and green. |
| Post | The post as it will read: the account, the text, the picture, 212 of 280 characters. A plain card, not the network's look. |
| Payment | The amount, to whom, for what, from which card, and the page it would be paid on. |

The decision bar names the answers in the request's own words ("Post it" and "Do not post", "Pay
€14.00") and says what happens if you never answer ("After 1 hour it is told no and keeps the
branch"). Approve is the screen's one primary. **Keyboard:** J and K move between requests, A says
yes, D says no, and after an answer the next open request comes up. On a phone the queue is one
screen and a request is the next; the answers stay pinned above the tab bar.

### Tables: the showpiece

Image: `shots/runs-selected@1440-dark.png` (Runs, two rows selected).

- **No grey band.** The head is 12 px medium in the tertiary text on the card itself, one hairline
  under it, sticky. app/ had a 36 px band in `surface.inset`; the band made the head the heaviest
  thing in the table, which is backwards.
- **Rhythm.** 52 px rows (36 compact), 20 px side padding, and the row hairlines stop 20 px short of
  the card's edges, like a ruled page.
- **Numbers** are Inter's tabular figures, right aligned, units in the head; a zero is quiet
  (tertiary) so the non-zero values stand out; an en dash for no value; a failure count in red.
- **Status** is a shape and a word; **kind** is a tile beside the run's name.
- **Hover** lifts the row and floats its actions over the end of the row on a small raised pill (run
  again, copy the command, open), so actions cost no column. On touch they step away; the row opens.
- **Selection.** A checkbox per row and a select-all with an in-between state. A selected row gets
  the ink edge with light and a lifted fill, and the head's tools give way to a bulk bar ("2 runs
  selected · Run again · Copy ids · ×") that rises into place.
- **Density** is a switch in the head (comfortable or compact). **Loading** is the real layout in
  quiet shapes with light passing over; **empty** is an icon tile with light under it, a sentence and
  the one command. Runs, Checks (severity counts as shapes, thumbnails) and Machines (sparklines,
  row actions) share all of it.

### Brand: components

Image: `shots/brand@1440-light.png` (Brand, components).

A Components section leads Brand, after the proposal, because it is what a design system team
opens first. A summary row (8 components, 93% of controls on checked pages are a component, 11
hand-rolled look-alikes on 4 pages, 1 proposal waiting), then a card per component:

- rendered **live in Acme's brand** (DM Sans, brand blue, its corners), in both themes, with its
  variants and states: Button (primary, secondary, ghost, destructive by default, hover, focus,
  disabled, loading), Input and select (empty, typing, error, a select open, disabled), Badge (five
  variants), Size and colour picker, Product card, Table row;
- the **tokens it is made of**, as chips with the colour swatch;
- **where it is used** on the checked pages, with counts and a bar of component uses against
  hand-rolled ones;
- the **look-alikes** studio found instead of it, each with its selector and page;
- and on Button, **an agent's proposal**: Codex proposes replacing 3 hand-rolled buttons with Button
  / Secondary, before and after, Decline and Approve (secondary buttons: the page's one primary is
  still "Approve version 5").

The specimens are marked `data-studio="ignore"` with the reason, so studio does not judge Acme's
brand as agentos's.

### Across the product

Machines, a running test (the dev task), a design check and Home on a phone are in the new style
too; images for every screen are listed in section 9.

## 5. Tokens

One generated set, in `style-glow/tokens.json` and `tokens.css`, from `style-glow/build-tokens.py`.
203 tokens. The script checks 130 contrast pairs (88 before, plus the kind colours) and the 8
colour-blind cases; it exits 1 if any fails. What changed against the shared set:

| Token | Was (app/) | Now | Why |
|---|---|---|---|
| `font-family.sans`, `.mono` | IBM Plex Sans and Mono | Inter (variable, optical sizes), JetBrains Mono | Plex at 12 and 13 px is what read as dated. |
| `color.neutral.*` | warm paper, graphite `#0d0e10` | cool near-black `#050507` page and `#0e0e12` card; cool paper `#f7f7f9` | Light needs a dark room to read as light. |
| `color.line.default`, `.strong` | solid ramp steps | white or black at 7.5% and 13% | Translucent edges work on any surface and on glass. |
| `color.line.highlight`, `fill.glass`, `atmos.a/b/c`, `shadow.color-3` | (new) | | The lit edge, the glass bar, the atmosphere, the card's lift. |
| `color.accent.*`, `focus.ring`, `chart.bar-strong` | ultramarine | ink (`text.primary`) | Hue is spent on kinds and status; the accent is brightness. |
| `color.status.live.*` | ultramarine | ink with a breathing ring | app-design.md worried live was everywhere on Home; it is calm now. |
| `color.status.*` (ok, warn, bad) | | slightly brighter in dark | They glow on near-black. |
| `color.kind.*` | (new, 24 tokens) | section 3 | The accent per kind of work. |
| `type.display` | (new) | 40/44 semibold, -0.045 em | Home's sentence. |
| `type.page-title` | 24/32, -0.024 em | 28/36, -0.035 em | Confident titles. |
| `type.body`, `.secondary` | 13/20, 12/16 | 14/20, 13/20 | Inter at 13 reads small; Geist and Resend sit at 14. |
| `type.number` | mono 12 | Inter 14, tabular | Numbers read as the table's values, not as code. |
| `type.table-header` | 12/16 medium, secondary | 12/16 medium, tertiary, no band | The head steps back. |
| `type.lede`, `.stat` | 16/24, 24/32 | 15/24, 32/36 | |
| `radius.*` | 4, 6, 8, 12 | 6, 8, 10, 14, and 20 (`2xl`) | Softer. |
| `row-height.default`, `.relaxed` | 48, 56 | 52, 60 | Room for the tile and two lines. |
| `duration.base`, `.slow`, `ease.enter` | 150, 180 ms | 180, 260 ms, a softer enter curve | Arrival reads, nothing snaps. |
| `shadow.card`, `shadow.lift` | (new) | | Depth in light; a hover lift. |

## 6. Where it bends web/DESIGN.md and foundations, and why

| Rule | Here | Why |
|---|---|---|
| Geist / Plex | Inter | The owner called the style dated; Inter with tight tracking is today's tool face, and not Vercel's own. |
| Nothing in the page casts a shadow; lines are borders, never box-shadow | Cards lift on `shadow.card` in light; edges are lit with an inset highlight; glows are box-shadows | Depth and light are the style. Lines that carry meaning (selection, a failure's edge) are still drawn as their own element. |
| One accent colour | The accent is ink; six kind hues | The owner asked for a colour per kind of work. Kinds have rules (section 3) so they never read as status or as the accent. |
| Table header on the inset band | No band | Section 4. |
| `live` has its own colour | Live is ink with a breathing ring | Calm when most things are live. |
| Approve is an ordinary button | On Needs you, Approve is the screen's one primary | On that page the decision is the screen's purpose. |

## 7. Studio, from a scratch HOME

`agentos studio up --port 7494 --no-skill` with HOME in a scratch folder, the prototype over
`python -m http.server`, the tools called over MCP. `ui_check` on Home, Runs, Checks, a check,
Machines with a panel open, Brand and a request on Needs you (360, 768, 1280, light and dark), again
with `since`; `system_check` against `style-glow/tokens.json` at 375 and 1440; `ui_image` and
`finding_get` on what looked wrong.

| Screen | ui_check, first | last | system_check on tokens: colour, space, type size, radius, shadow |
|---|---:|---:|---|
| Home | 2 | 1 (false alarm, below) | 100%, 97%, 94%, 92%, 51% |
| Runs | 2 | 2 minor | 100%, 90%, 95%, 87%, 76% |
| Checks | 0 | 0 | |
| A check | 2 | 0 | 99%, 99%, 99%, 98%, 24% |
| Machines | 2 | 1 minor | 100%, 97%, 98%, 93%, 59% |
| Brand | 3 | 3 minor | 93%, 98%, 98%, 89%, 68% |
| Needs you | 1 | 1 minor | 100%, 98%, 96%, 100%, 33% |

For comparison, app-design.md measured Home at 56% colour and 55% space; the drawn shop pages are now
marked `data-studio="ignore"` and the app itself is on its tokens. Shadow is low by design: the glows
are built from colour tokens but their geometry is not a token (section 8).

**Real problems studio found, fixed:** the dot field behind Home's sentence made the page 16 px wider
than a phone; the request titles on Home were 20 px tall links (under 24); the "last minute" unit in
the Machines head was 3.8:1 (it had an extra opacity); the screen chips on a check were 4.4:1 on a
selected finding; the glass tab bar fell to 1.9:1 over a bright drawn page (the glass is more opaque
now); a look-alike's page and two queue lines were cut with no way to read them; Acme's colour
swatches were judged as off our tokens; raw 9 px, 5 px, 2 px and 1 px values in marks and gaps.

**Left, on purpose:** two truncated cells with a `title` (a long branch, a live output line), the
cropped pictures sizing themselves on Brand (a layout shift of 0.13 at 768), and Brand's `#EEF4FF`
swatch label at 360.

### Where studio was wrong or awkward

1. **A contrast false alarm from a sticky bar.** "Running now" at 360 dark was reported 4.44:1 with
   `bg_at_worst #3f76a0`. `ui_image` of the place shows white on near-black; that blue is the
   workspace mark in the sticky top bar, sampled at another scroll position. Full-page captures with
   sticky or fixed elements need those hidden or measured in place.
2. **`text.truncated` still ignores a `title`.** Reported before.
3. **Scale tokens count as hard-coded.** `font-size: var(--font-15)` on a phone's body and
   `var(--font-16)` on Home's lede are reported "hardcoded, match exact". Reported before.
4. **Glows have no token shape.** A box-shadow built from `var(--kind-test-glow)` is reported as an
   off-token shadow. A DTCG shadow can name a colour token but not "this geometry with any of these
   colours"; studio could accept a shadow whose colour is a token and whose geometry matches a
   declared shadow token.
5. **"Fonts fell back: JetBrains Mono -> ui-monospace"** on Needs you at 360 does not say which text,
   so I could not tell whether it was a missing glyph (the − sign is outside the latin subset) or a
   late load. Naming the characters would answer it.
6. **`system_check` returns no `adherence` key** although its description and the docs talk about
   adherence; the shares are under `system.on_tokens`. A one-word mismatch that cost a round trip.
7. **The Python MCP client** yields two values from `streamable_http_client` in this environment,
   not three as older examples show. Reported before for the name.
8. **Studio's light and dark** come from the system setting, so a page that pins its own default
   theme cannot be checked in both. I first made this take default to dark, and studio checked dark
   twice; it now follows the system until the person picks.

## 8. What I am unsure of

- **The ink accent.** It is the cleanest way to give hue to kinds, and it matches how Vercel and
  Resend work, but focus and selection are now white in dark. If the owner misses a brand colour,
  `accent.*` and `focus.ring` are five lines to change.
- **Six kinds is near the limit.** Tests and design are ΔE 9.3 apart under protanopia in light. A
  seventh kind would need a new device (a pattern or a different tile shape), not a new hue.
- **Glows in light.** The light theme carries depth on shadows and a dawn wash; the kind glows are
  faint there by design. It is considered but quieter than dark, which is the lead.
- **The display sentence** at 40 px is the screenshot moment; with a long sentence (20 jobs) it is
  three lines. It drops to 28 px under 1100 px wide.
- **Glass.** The top bar, the tab bar and the scrim blur. Cheap on a laptop; to measure on low-end
  phones before it ships.
- **Shadow tokens for glows**, so system_check can count them (section 7).
- **Needs you as an inbox** changes the page's UX (approved as a list in app/). I think it is the
  improvement the owner asked for; the list form is one CSS rule away if not.

## 9. Images

Every screen and state at 1440 and 375, light and dark, and the main screens at 768, in this
checkout (git-ignored, about 52 MB): `panel/prototypes/dashboard/style-glow/shots/`.
`<screen>-screen@<width>-<theme>.png` is the first screen as the window shows it;
`<screen>@<width>-<theme>.png` is the full page. Screens: `home`, `home-one-job`,
`home-nothing-running`, `home-many`, `home-loading`, `home-error`, `home-empty`, `runs`,
`runs-selected`, `runs-loading`, `runs-empty`, `run`, `run-build`, `checks`, `checks-error`, `check`,
`check-finding-3`, `machines`, `machine-open`, `machine-not-answering`, `machines-many`,
`machines-empty`, `brand`, `brand-empty`, `needs`, `needs-push`, `needs-vm`, `needs-lane`,
`needs-post`, `needs-pay`, `needs-many`, `needs-empty`, `home-light-many-needs`. Make them again with:

```
uv run python panel/prototypes/dashboard/style-glow/shoot.py               # everything, 375 768 1440
uv run python panel/prototypes/dashboard/style-glow/shoot.py home needs-push
WIDTHS="1440 375" uv run python panel/prototypes/dashboard/style-glow/shoot.py
python3 panel/prototypes/dashboard/style-glow/build-tokens.py               # tokens, contrast, colour-blind check
```
