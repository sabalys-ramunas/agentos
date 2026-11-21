# Style take "style-mono" (W3): precise and monochrome

> The prototypes under `panel/prototypes/` were removed with the classic panel on 2026-11-14. They are
> in git history (commit 59634500 and earlier); the commands below run only on such a checkout.

Status: design, 2026-11-13. One of two style takes on the approved app design
([app-design.md](app-design.md)); the owner picks one. The owner's words on app/: "sections and cards
need to be more scannable ... each can have a colour accent to visually show different task groups.
Problems and Needs you not well designed, tables look not good at all ... more modern like Vercel or
Resend ... I also think Brand is missing components ... Biggest issue is design style, it's dated."

This take answers in the spirit of Vercel: true black and white with a cool neutral ramp, Geist Sans
and Geist Mono, tight headings, hairlines that almost disappear, generous space, colour only for
meaning, crisp small radii, restrained motion. The structure and UX of app/ stay as approved: Home
with the slider of running jobs, Machines on one screen, the dev task, the design check.

The prototype is `panel/prototypes/dashboard/style-mono/index.html`.
Plain HTML, CSS and JS, no build step; it opens from disk. It is a copy of app/ with its own token
set (`style-mono/build-tokens.py`, which writes `style-mono/tokens.json` and `tokens.css`; the shared
`build-tokens.py` and app/ are untouched). Every state from app/ still works (`?state=loading`,
`error`, `empty`, `?many=1`, `?jobs=0`, `?run=done`), plus `?sel=2` (two runs selected),
`?density=compact`, `?n=n3` (open one request on Needs you) and `?at=b-comp` (Brand at its components).

## 1. What I learned from the references

I drove a browser over vercel.com, vercel.com/geist (introduction, colours, button, table, badge,
empty state) and resend.com and its docs, in light and dark, and read the computed styles. The
screenshots are in `style-mono/shots/ref/` (git-ignored), with `probe.json` holding the measured values.

- **Type.** Geist everywhere. Headings are tight: the Geist page title is 40/48 at 600 with
  -2.4 px of tracking (-0.06 em), section heads 24/32 at -0.04 em, the Vercel hero 64/64 at weight
  400 with -0.06 em. Body is 14/20 in every component, never 13. Code and numbers in Geist Mono at
  13 to 14. Resend uses a serif display face only for marketing heroes; its docs are a plain sans.
- **Neutrals.** Hue-free greys, not warm or blue: page `#fafafa`, card `#ffffff`, hairline
  `#ebebeb`, secondary text `#4d4d4d` to `#666`, ink `#171717`. Dark is true black: page `#000`,
  cards `#0a0a0a`, borders around `#1f1f1f` to `#2e2e2e`, text `#ededed`, secondary `#a1a1a1`.
  Resend's dark is the same idea (`#000` page, `#f0f0f0` text, `#a1a4a5` secondary).
- **Borders and surfaces.** One hairline, so light it nearly disappears on the page colour. Cards
  are white on an off-white page, held by the hairline and, in light, a 1 px shadow. No band behind
  a table head: the Geist table head is 14 px at 500 in the secondary grey over one line, rows 40 px
  with no lines between them, the hover a rounded fill.
- **Radii.** Small and consistent: 6 px on buttons, inputs and inline code, 8 to 12 px on cards,
  full pills only for badges and marketing buttons.
- **Buttons.** 32 and 40 px. The primary is ink (black in light, white in dark), never a hue. The
  secondary is the surface with a hairline. Weight 500, 14 px.
- **Badges.** Pills, 20 px tall, 11 to 12 px at 500, a soft tint with darker text per hue
  ("Blue-Subtle", "Green-Subtle"), and a solid version for emphasis.
- **Colour.** Almost none on the dashboard surfaces. Colour turns up only where it means something:
  status (Ready green, Error red, Building amber), a focus ring in blue, a link on hover.
- **Light and depth.** The memorable moments are light on black: Vercel's glowing triangle, Resend's
  lit cube. On a working screen that becomes a dot grid fading out, a glow behind the headline, a
  1 px light on the top edge of dark cards instead of a shadow.
- **Motion.** Short (100 to 200 ms), opacity and small translations, nothing bouncing.
- **Empty states.** An icon in a small bordered tile, one bold line, one sentence, one action.

What I did not take: layouts, the triangle, the wordmark, Resend's serif, marketing scale. openajar
keeps its rail, its sentences and its mono-caps kind labels, which neither product has.

## 2. The style in five rules

1. **Ink, paper, one hairline.** Surfaces are white and `#fafafa` in light, `#000` and `#0a0a0a`
   in dark, with nothing in between that carries a hue. Every edge is `line.default`
   (`#ebebeb` / `#262626`); a card adds `--lift` (a 1 px shadow in light, a 1 px top light in dark)
   and nothing else. No grey bands: not on table heads, not on card heads.
2. **Colour means something, or it is not there.** Hues are kept for two things only: the kind of
   work (`kind.*`) and status (`status.*`). What is selected, what is primary, what is running and
   every chart that is not a status is ink. Focus is the one blue, and means only "the keyboard is
   here".
3. **Geist, tight at the top, plain at the body.** Display 40/44 at -0.045 em (Home's sentence
   only), page titles 28/36 at -0.035 em, card titles 14 or 16 at 600, body 14/20. Numbers, ids,
   commands and branches in Geist Mono with tabular figures. One caps style, the mono eyebrow
   (11 px, +0.04 em), only for a kind's word and "If yes".
4. **Small radii, one per job.** 4 for marks and checkboxes, 6 for buttons and inputs, 8 for
   pictures and previews, 10 for cards, full pills for status pills, counts and branches.
5. **Motion explains or stays still.** 100 to 200 ms, opacity and 2 px moves: a row action fading
   in, an arrow nudging on hover, the selection bar rising. The live dot pulses; under reduced
   motion it is a still ring and nothing else moves.

## 3. The kind of work, and how it stays apart from status

| Kind | Where it shows | Mark, light / dark | Icon |
|---|---|---|---|
| Tests (`openajar run` suites) | slider cards, Runs rows, a run's page, Problems, Needs you (a new lane) | violet `#9a5bd6` / `#b18cfa` | flask |
| Design (ui_check, ui_diff, a brand change) | check cards and pages, the Checks table, Brand's proposal, Problems | pink `#c8337f` / `#f06bb0` | the check frame |
| Build and CI (builds, installs, lint and types, migrations) | slider, Runs rows, a run's page, Needs you (push, VM) | teal `#0b6e88` / `#3fc6dc` | box |
| Dev server | slider, Runs rows | blue `#1e40af` / `#4c7dff` | radio |
| Machines, pushes, posts, payments | Problems, Needs you | none: neutral tile | its own icon |

**One device, everywhere a kind shows:** a 20 px tile in the kind's soft tint with the kind's own
icon in its mark colour, and the kind's word in mono caps in its text colour ("TESTS", "DESIGN
CHECK"). A job card also takes the mark as a 2 px top edge, so a row of cards reads as purple,
pink, teal, blue before a word is read. The filter chips over the slider carry the same colours as
small squares, so they double as the legend.

**How it never reads as a status:**

- Different hues. Status keeps green (passed), amber (needs a look) and red (failed); running is
  ink. The kinds take violet, pink, teal and blue, none of which status uses.
- Different shapes. Status is always a shape and a word in the foundations' language (dot, diamond,
  square, ringed dot, hollow ring). A kind is always a rounded tile with an icon. Nothing that is a
  kind is a dot, and nothing that is a status is a tile.
- Different places. On a card the kind sits left with the top edge; status sits right as a pill
  ("2 failed" in red on its tint). The red top edge app/ used for failing cards is gone, so the top
  edge can only mean a kind.
- **Checked by the script.** `build-tokens.py` here simulates protanopia, deuteranopia and
  tritanopia (Machado 2009) and measures, in OKLab, the distance between every kind mark and every
  other kind and status dot, in both themes. It fails under 0.04. The closest pairs now: light
  protanopia, design and build, 0.045; light deuteranopia, design and green, 0.070; dark protanopia,
  tests and dev server, 0.077. With the first palette I tried (plain Tailwind violet, pink, cyan,
  blue) tests and dev server were 0.004 apart for deuteranopes: identical. Each kind's word passes
  4.5:1 on every surface and on its own tile, and its mark 3:1 on cards and the page (112 pairs).

## 4. The four weak places

### Home: scannable in two seconds, and Needs you and Problems redesigned

The sentence is now the one display line (40/44, -0.045 em) on a dot grid that fades from it, with
a soft light in dark: the screen's one moment of light. Under it the slider, with the kind accents
and the chips as a legend, and a status pill only where something is wrong.

**Needs you** was a plain list with Deny and Approve. Each request now says **what happens if you
say yes**, in one line fitted to its kind, after a mono "IF YES": a brand change shows the two blues
as swatches and "4.4:1 → 5.3:1"; a push "3 commits into main +33 −5"; a VM "€0.02 at most, deletes
by 16:21"; a lane its limits; a post its length; a payment its amount. The decision sits on the
right; the first request you can approve is the one primary button on Home and carries its key (A).
The header counts in an amber pill with the age of the oldest.

**Problems** was a second list. Each problem now has what broke, its kind's tile, the **evidence**
as a two-line diff (received 42 in red, expected 41.99 in green, as a diff tool would draw it),
**who is on it** with the agent's mark ("Claude Code is fixing it on fix/cart-total-rounding"), and
one link. The header shows a red count and a severity bar, one tick per problem.

### The Needs you page

A queue on the left (each row: icon or kind tile, the request, the short "if yes", its age) and the
request you are deciding on the right, as a large card: the question as a title, why it asks, and
under "IF YOU SAY YES" a preview fitted to its kind:

| Kind | The preview |
|---|---|
| Push to main | `fix/cart-total-rounding → main` as two branch pills, the 3 commits on a line with their hashes and +/−, files changed, and that the cart tests passed on hel-1 |
| Disposable VM | three big numbers (€0.0060 an hour, €0.02 at most, deleted by 16:21), the cost along 2 hours, size, place, what it runs, the month so far |
| Brand change | the real product page at 360 px as it is and as it would be, the badge outlined red (4.4:1) and green (5.3:1), and a link to the whole proposal |
| New lane | lane, machine, CPU, memory and network as a spec strip, then the first commands in a terminal |
| Post | the post as it will look on X, with its picture, and its length against 280 |
| Payment | the amount large, the registrar's page drawn with the total and the card, and the lane it pays from |

The decision is a footer bar with Deny (D) and a primary that names the act ("Approve the push",
"Rent it", "Make the lane", "Post it", "Pay €14.00", "Approve version 5") and answers to A. J and K
(or the arrows in the list) move; after a decision the next waiting one opens on its own and a
toast offers Undo. On a phone every request is one card with its preview, in order.

### Tables

The Runs history is the showpiece, and Checks and Machines follow the same rules.

- **No grey band.** The W2 head sat on `surface.inset`. Here the head is 13 px medium in the
  tertiary ink on the card, over one hairline: the names step back and the rows lead. I could not
  defend the band: it made every table look like a spreadsheet and added a third grey.
- **Rhythm.** Rows 52 px (40 compact, 60 relaxed), header 40, a hairline between rows that almost
  disappears. 20 px gutters at both ends, matching the card head.
- **Numbers.** Geist Mono, tabular, right aligned, units in the head, an en dash for no value, a
  zero in the tertiary ink so a real count stands out, failures in red.
- **Status** as shape and word; **kind** as the tile beside the run's name; **branch** as a pill.
  "Agent" and "Machine" became one column, "Who and where" (mark, agent, on, machine), so the run's
  name keeps its width at 1440.
- **Hover** is a fill; **selection** is a checkbox that appears on hover (or X on the focused row),
  a tinted row with a 2 px ink edge, and an ink selection bar over the table with Run again, Copy
  ids and Clear (Esc).
- **Row actions** (Run again, copy the logs command; on Checks, copy a new check) fade in on hover
  and focus and stay out of the way otherwise.
- **Density**: a Comfortable / Compact switch in the table's head; compact drops second lines.
- **Loading** is a skeleton shaped like the table (head and seven rows); **empty** is the
  foundations' empty state inside the table.
- **On a phone** the table keeps the run, its result and when; the rest is on the run's page.

### Brand: components

Brand now opens on **Components**, the screen a design system team opens first. A summary strip:
492 real component uses on 9 checked pages, 30 hand-rolled look-alikes, 94% on the system, 1 change
waiting. Then one card per component (Button, Input, Badge, Select, Size chip, Product card, Order
row), each with:

- the component **rendered live in Acme's own tokens** (DM Sans, Acme's blue) on a dotted stage, in
  its variants and states (Button: Primary, Secondary, Ghost by Rest, Hover, Focus, Disabled;
  Input: Rest, Focus, Error with its message, Disabled; Size chip: Rest, Selected, Sold out);
- the **tokens it uses** as mono chips;
- **where it is used**: the count, and a bar per page pattern (`/products/*` 96, `/cart` 41, ...);
- its **look-alikes**: how many times, which selector, on which page and what is off
  ("6× `a.cta-big` /cart, padding 10px, radius 7px"), with an amber status in the card's head;
- an **agent's proposal** where there is one: the Badge card shows version 5's darker blue beside
  today's, tinted in the design accent, and links to the proposal at the top of the page.

The Acme renderings keep Acme's raw values on purpose (they are another brand) and are marked
`data-studio="ignore"` so studio does not judge them as the app.

### Machines, a dev task, a design check, Home on a phone

They take the same rules with no change to their structure: the machines table without a band,
the run page with its kind label over the title and its dark terminal, the check page with the
design label and ink selection on the screens. Home on a phone keeps the order sentence, slider,
Needs you, Problems; requests stack their buttons full width at 40 px.

## 5. Tokens: what changed from the shared set

`style-mono/build-tokens.py` is a copy of the shared script; every change is marked "W3 mono".
It still checks every text pair (112 now, all pass AA) and adds the colour-blind check above.

| Token | Shared (W2) | style-mono | Why |
|---|---|---|---|
| `color.neutral.*` | warm paper `#f4f4f1` / graphite `#0d0e10` | hue-free: `#ffffff #fafafa #f5f5f5 ... #525252 #2e2e2e #171717`; dark `#000000 #0a0a0a #111111 ... #a1a1a1 #d4d4d4 #ededed` | True white and black, Vercel's grey scale |
| `surface.card` (dark) | step 2 | step 1, `#0a0a0a` on a `#000` page | Cards sit one step above black |
| `surface.inset` | the table head band | a well only (code, pictures loading, card footers) | No grey band |
| `line.default` | `#e0e0d9` / `#26282d` | `#ebebeb` / `#262626` | The hairline that almost disappears |
| `accent.*`, `chart.bar-strong` | ultramarine | ink (`neutral.12`), soft `neutral.3`/`4` | Colour is kept for meaning |
| `focus.ring` | ultramarine | blue `#006bff` / `#3d8bff` | The one blue: only focus |
| `status.live.*` | ultramarine | ink, with the ringed pulsing dot | Running is the normal state; hues stay free |
| `terminal.*`, `ansi.*` | light in light | dark in both (`#0a0a0a` / `#050505`) | Output is the one thing that looks like a screen |
| `shadow.card`, `color.highlight.card` | (none) | a 1 px shadow in light, a 1 px top light in dark | Cards lift a little; see section 6 |
| `kind.{test,design,build,server}.{mark,text,soft,line}` | (none) | section 3 | The accent per kind of work |
| `font-family.*` | IBM Plex | Geist, Geist Mono | web/DESIGN.md's faces, and the reference |
| `type.display` | (none) | 40/44 600 -0.045 em | Home's sentence |
| `type.page-title` | 24/32 | 28/36 -0.035 em | Tighter and larger, like Geist |
| `type.page-title-compact` | 20/28 | 24/32 | A phone title that still reads as a title |
| `type.body`, `label` | 13/20 | 14/20 | Vercel's body; also ends the phone's raw 14 px override |
| `type.secondary` | 12/16 | 13/20 | A second line one step under 14 |
| `type.table-header` | 12/16 | 13/20 medium, tertiary | Head on the card, not a band |
| `type.number`, `type.code` | mono 12 | mono 13 | Geist Mono at 13 matches 14 px sans |
| `type.stat` | 24/32 | 28/32 | |
| `type.eyebrow` | (none) | mono 11/16 medium +0.04 em caps | The one caps style: a kind's word |
| `radius.xl` | 12 | 10 | Crisper cards |
| `row-height.*` | 36 / 48 / 56, head 36 | 40 / 52 / 60, head 40 | 14 px body needs the air |
| `duration.*` | 120 / 150 / 180 | 100 / 150 / 200 | |
| `font.28`, `font.40`, `line-height.36`, `line-height.44` | (none) | added | The new roles |

## 6. Where it bends web/DESIGN.md and the foundations, on purpose

| Rule | Here | Why |
|---|---|---|
| Nothing in the page casts a shadow | Cards, buttons and tiles take `--lift`: a 1 px, 4% shadow in light, a 1 px top light in dark | Without it, white cards on `#fafafa` with a `#ebebeb` line wash out at a glance; it is the lift every reference uses, and it is one token |
| One accent | No hue accent at all: selection and the primary are ink, and four kind hues carry meaning | The owner asked for a colour per kind of work; an accent hue for "selected" would be a fifth meaning |
| `live` is a hue | Running is ink with the ringed, pulsing dot | So no status shares a hue with a kind |
| Sentence case everywhere | One caps style: mono 11 px for a kind's word and "IF YES" | It reads as a label, not as text, and the caps are short |
| The table head sits on the inset band | No band | Section 4, Tables |
| Terminals follow the theme | Dark in both themes | The pictures of running output are the strongest thing on Home; light terminals looked like text boxes |

## 7. Checked with studio

`openajar studio up --port 7492 --no-skill` under a scratch HOME, the prototype served over a
threaded local server, the tools called over MCP. `ui_check` on eight screens (360, 768, 1280,
light and dark), fixed, then again; `system_check` against `style-mono/tokens.json` at 375 and 1440;
`ui_image` on findings.

| Screen | ui_check first | last | What is left |
|---|---:|---:|---|
| Home | 4 | 1 | A layout shift studio puts on the slider at 1280; a fresh Chrome I drove records none (below) |
| Runs | 3 | 2 | Two cut lines that carry the full text as a title (a long branch, a live output line) |
| Needs you | 4 | 1 | A "·" cut at 768 in a row whose full text is its title |
| Brand | 4 | 1 | A 0.128 shift as the cropped pictures size themselves |
| Machines | 1 | 0 | |
| Checks, a check | 0 | 0 | |
| A run | 3 | 0 | |

`system_check` (share of values on a token, against `tokens.json`):

| Screen | Colour | Space | Radius | Size | Weight | Line height | Family | Border | Shadow |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Home | 99% | 100% | 100% | 96% | 100% | 100% | 100% | 100% | 100% |
| Runs | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| Machines | 100% | 100% | 100% | 98% | 100% | 99% | 100% | 100% | 100% |
| Needs you | 98% | 100% | 100% | 100% | 100% | 99% | 100% | 100% | 100% |
| Brand | 90% | 98% | 96% | 98% | 99% | 98% | 100% | 93% | 100% |
| A check | 99% | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% |

W2 measured Home at 56% colour. Most of the gain is studio's new `data-studio="ignore"`, which
this take puts on every picture of another product (the drawn shop, Acme's components, the
registrar's page, Acme's swatches); the rest is real (raw 11 and 14 px sizes moved onto roles).
What is left on Brand is Acme's specimen values inside the guideline rows and the brand proposal.

Real problems studio found, fixed:

1. **Light status tints on the dark terminal.** A failed line in the run's output used
   `status.bad.fill` (light pink) under light text on black: 1.12:1. Terminals and code blocks now
   tint from the ANSI colours (`color-mix` of `ansi.red` and `ansi.green`). This came straight from
   making terminals dark in both themes; I would not have seen it on the screens I looked at.
2. **The unit in a table head** ("last minute") was dimmed with opacity to 3.5:1. Now it is the
   tertiary ink.
3. **The runs count in the rail** on the selected item: 4.47:1 on the pressed fill. Now secondary.
4. **"See all 6"** was a 20 px tall target next to the phone's tab bar. Links in a card head are 28 px.
5. **Cut text without a way to read it**: the "if yes" line in the Needs you queue was clipped
   mid-word. It now ends in an ellipsis, and every cut line in the app gets its full text as a title.
6. **Off the system**: an 11 px font size on agent marks (now the micro role), the phone's raw
   14 px body (gone: body is 14), and a range input left in the browser's colours.

My own eye found the rest: the Runs table squeezed the run's name to a word per line on a phone,
the Badge card stood alone in its row, the post's picture was a giant crop, the push preview's
branch pill was cut at "fi...", Machines' "The last hour" title wrapped when the side panel was
open, the VM's cost ticks collided on a phone, and Home's two panels fell below the first screen.

### Where studio was wrong or awkward

1. **A wrapped inline link "covers" the link before it.** "6 things need you" wraps on a phone; its
   bounding box spans both lines and overlaps "The product page has a blocker", so studio reports a
   covered control (major). The two links do not overlap. Hit-testing the link's line boxes
   (`getClientRects()`) instead of its bounding box would avoid it. I made the links not wrap.
2. **A layout shift I cannot reproduce.** Studio reports 0.314 on Home at 1280, first "100% while
   web fonts loaded", then, after I preloaded the fonts, on the slider. A fresh Chrome I drove with
   a layout-shift observer records none. The finding gives selectors but not the before and after
   rectangles; with them I could tell font swap from slider setup.
3. **A title on the element is not read as "a way to read the rest".** Studio's fix says to put the
   full text in a title; with titles on every cut line it still reports them, as minor. Either
   honour the title or stop suggesting it.
4. **A swatch is data, not style.** Colour chips that show another brand's value count as
   off-system colours. `data-studio="ignore"` handles it, but each swatch needs it; a rule that an
   element whose only job is its background (`role=img`, or an inline style on an empty element)
   is shown data would do it once.
5. **`ui_image` crops are good now** (the finding outlined with the page around it); that fixed the
   most-asked item from W2.
6. **The Python MCP client in this repo** returns two values from `streamable_http_client`, not
   three, and `Tool` is dumped with `inputSchema`; studio's examples unpack three.
7. **Tool arguments are strict**, which is right, but an unknown argument fails the whole call;
   a script that adds its own key (I added `_out`) learns this only by failing.

## 8. Judged against the references and app/

`style-mono/shots/compare-home-light.png` and `-dark.png` put app/'s Home, this Home and vercel.com
side by side at 1440. app/ reads as a careful tool from a few years ago: warm paper, Plex at 13 px,
every block the same bordered box, an ultramarine in five places. This one reads as this year's:
black and white, the type tight and large where it matters, colour only on the kinds and the
statuses, the terminals as the one dark object on a light page. It does not have Vercel's single
striking picture; the dot grid and the light behind the sentence are as far as a working screen
should go.

## 9. What I am unsure of

- **Four kind hues are the limit.** A fifth kind (say "deploy") has no room left that is apart from
  green, amber, red and the four; it would have to share a hue with its family and lean on its icon.
- **Violet and blue for colour-blind viewers.** They pass the check in both themes, but protanopes
  see tests and dev servers closest of all pairs in dark (0.077). The icon and the word carry it.
- **Running in ink.** It frees every hue, but "something is happening" is less eye-catching than a
  colour. The pulse does the work; under reduced motion it is only a ring.
- **The 1 px lift breaks a foundations rule.** It is one token (`--lift`) to switch off.
- **Dark terminals in light mode** are the strongest objects on Home. If they pull the eye too far
  from Needs you, `terminal.bg` in light goes back to `neutral.2` in one line.
- **The display size.** 40 px on Home pushed both panels to the bottom of the first screen at
  1440 by 900; they start there, but only just.
- **Needs you as master and detail** costs a click on desktop compared with app/'s list of buttons.
  It pays back with the preview and the keyboard (J, K, A, D with auto-advance), but someone who
  approves pushes all day may want a "list only" switch.

## 10. Images

Every screen at 375, 768 and 1440, light and dark, in this checkout (git-ignored, 57 MB):
`panel/prototypes/dashboard/style-mono/shots/`. `<screen>-screen@<width>-<theme>.png` is the first
screen as the window shows it (1440 by 900, 768 by 1024, 375 by 812); `<screen>@<width>-<theme>.png`
is the full page. Screens: `home`, `home-one-job`, `home-nothing-running`, `home-many`,
`home-loading`, `home-error`, `home-empty`, `runs`, `runs-selected`, `runs-compact`, `runs-loading`,
`run`, `run-build`, `checks`, `check`, `check-finding-3`, `machines`, `machine-open`,
`machine-not-answering`, `machines-many`, `machines-empty`, `brand`, `brand-components`, `needs`,
`needs-vm`, `needs-brand`, `needs-lane`, `needs-post`, `needs-pay`, `needs-many`, `needs-empty`. The
references are in `shots/ref/`, the comparison in `shots/compare-home-*.png`. Make them again with:

```
uv run python panel/prototypes/dashboard/style-mono/shoot.py               # everything
uv run python panel/prototypes/dashboard/style-mono/shoot.py home needs    # some screens
WIDTHS=1440 THEMES=dark uv run python panel/prototypes/dashboard/style-mono/shoot.py
python3 panel/prototypes/dashboard/style-mono/build-tokens.py               # the token set and its checks
```
