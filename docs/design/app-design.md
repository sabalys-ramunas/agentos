# The app design (W2): Workbench, calmer, on one token set

> The prototypes under `panel/prototypes/` were removed with the classic panel on 2026-11-14. They are
> in git history (commit 59634500 and earlier); the commands below run only on such a checkout.

Status: design, 2026-11-12. The owner chose Workbench as the base ("machines is better in workbench,
same with brand, dev task, sidebar is better too") and asked for a calmer Home with running jobs as
a slider with pictures, a Machines screen that needs no scrolling, and more focus on UX. This is
that design, and the one the real web app will be built from.

The prototype is
`panel/prototypes/dashboard/app/index.html`: plain
HTML, CSS and JS, no build step. Open the file, or serve `panel/prototypes/dashboard/` (it loads
`../tokens.css`). Workbench, Canvas and foundations.html are left as they were.

| File | What it is |
|---|---|
| `app/index.html`, `app.js`, `app.css` | The app. Every colour, space, radius and type value in its own interface is a token from `../tokens.css`. |
| `app/data.js` | The Acme Shop workspace: jobs, queue, machines, approvals, checks, findings, brand. |
| `app/shop.js` | The Acme product page, drawn as HTML at its real width (another brand, on purpose). |
| `app/fonts/` | IBM Plex Sans and Mono, and DM Sans for the drawn shop, latin subset, OFL. Local, so the page works from disk and studio measures the same face every time. |
| `app/shoot.py` | Images of every screen and state at 375, 768 and 1440, light and dark. |
| `build-tokens.py` | Still the one place token values live. Section 4 lists what changed. |

States: `?state=loading`, `?state=error`, `?state=empty` on every screen (also the "Prototype state"
control at the foot of the rail, or the command bar). `?many=1` gives 20 running jobs, 12 machines
(one with a 59-character name) and 12 approvals. `?jobs=1` and `?jobs=0` show Home with one job and
with nothing running. `?run=done` opens the test run already finished. `?f=3` on a check opens that
finding.

## 1. Home

### The first five seconds

A person opening Home wants three answers: what needs me, what is running, is anything wrong. The
screen now gives itself to those three and nothing else.

1. **One sentence** says the state: "5 jobs running on 4 machines. A test run failed, and Claude
   Code is already fixing it." Under it, one line in the secondary colour with links: "The product
   page has a blocker. 6 things need you, the oldest for 2 h." It is Canvas's opening sentence, at
   the page title's size (24 px) instead of Canvas's 46, so it reads as this tool's voice and not a
   poster. It counts what is true: with 20 jobs it says "5 test runs have failures, and their
   agents are on them"; with nothing running, "Nothing is running right now."
2. **Running now**, a slider of job cards (below).
3. **Needs you** (left, 7 of 12 columns) with the 3 oldest approvals, Deny and Approve on each, and
   "All 6". **Problems** (right) lists what is wrong, worst first: a failed test with expected and
   received, a page with a blocker (with a crop of the element), a machine not answering. Each
   says who is on it and has one link. Empty: "Nothing is wrong".
4. **Earlier today**, one quiet row of three links: runs finished and failed, pages checked and
   findings open, machines online and the month's VM cost.

At 1440 by 900 the sentence, the slider and the top of both panels are on the first screen. The
page is 1,232 px tall, down from 1,837. On a phone the order is the sentence, the slider, Needs
you, Problems, the links.

### What left Home, and where it went

| Was on Workbench's Home | Now |
|---|---|
| Five numbers (running, tests today, open findings, on the design system, VMs this month) | Running is in the sentence. Tests today is the Runs page's summary line. Open findings and on the design system are the Checks page's summary line and the "Pages" link. VMs this month is the "Machines" link. |
| The last hour on every machine (timeline) | Machines, under the table, and Runs. It explains a machine's hour; on Home it needed learning before it said anything. |
| Running now as a 6-row table with live last lines | The slider. The table form is the Runs page's "Running now". |
| Design checks, 3 cards with drawn pages | Checks. The check that is running is a slider card; a blocker is a Problem. |
| Machines, 5 rows with sparklines | Machines. A machine not answering is a Problem; the rail's fleet bars show load at a glance. |
| Finished today, a 6-row table | Runs ("Earlier today" links there). |

### Running now: the slider

One card per running job: a test run, a build, a design check, a dev server. Each card has a picture
when it earns one (the rule is below), what kind of job it is with its command in mono, a plain
title, the agent and the machine, progress in its own terms (319 of 440 tests, 2 failed; 4 of 6
screens, 1 blocker; building 64%; online on :3000), elapsed time, and one action that names what you
will see: "See the 2 failures", "See the findings", "Watch the output", "See the machine". A card
with failures has a 2 px red top edge. The job most in need of attention comes first.

- **Moving through it.** The track scrolls sideways with snap points. Arrow keys move between cards
  (focus goes to the card's action, which is the card's one tab stop), Home and End jump. A mouse
  drags it (a drag never fires a click). A finger swipes it. The pager says "1–3 of 5" and has
  previous and next buttons that disable at the ends. Dots show the page and jump to it.
- **The next card peeks.** Cards are sized to 3.3 per row at desktop width, 2.25 at tablet width
  and 84% of the width on a phone, so a cut card always says there is more.
- **1 job:** one wide card, the picture on the left, the facts on the right, no pager.
  **2 or 3:** they share the row with no peek. **6:** peek and pager. **20:** filter chips with
  counts appear (All 20, Failing 5, Tests 11, Checks 5, Builds 3, Dev servers 1), the pager counts
  "1–3 of 20", and dots give way to the count once there would be more than 8. **None:** a calm
  line that says when the last run finished and how it went, with the command that starts one.
- **What is waiting** is the last card, "Up next", text only: the queued jobs with their command,
  machine and why they wait.
- **Live.** Terminal pictures take new lines every second, progress and elapsed time tick, a build's
  percentage climbs. Nothing else moves. Under reduced motion the live dot is a still ring and
  scrolling jumps instead of gliding.

### The rule for pictures

**A picture appears only when it answers faster than a sentence: what a page looks like, or what a
command that is still running is printing right now.** A finished run, a queued job, a number, a
machine or a yes or no question gets words.

How it plays out on every screen:

| Where | Picture | Why |
|---|---|---|
| Slider: test run, build | The last lines of output, as a small terminal | Still moving; the lines say more than "64%" |
| Slider: design check | The page being checked, with its finding marks | The finding is about how the page looks |
| Slider: dev server | The page it serves, in a browser frame with its address | The thing a person wants to see is the page |
| Slider: Up next (queued) | None | Nothing has happened yet |
| Problems: blocker on a page | A crop of the element, outlined | "The wishlist button" is found in one look |
| Problems: failed test, machine not answering | None | Expected 41.99, received 42 is the picture |
| Checks table | A small thumbnail per page | A path alone does not tell pages apart |
| Runs, Needs you, Machines tables | None | Rows are compared on facts, not looks |
| A check, the brand proposal, before and after | The real page, large | The page is the subject |
| Machines side panel | A live load graph; lanes as small icons | A trend, not a number |

## 2. Machines

One screen with no scrolling at 1440 by 900 with 5 machines (the page is exactly 900 px tall), and
still one screen with 12 (rows go from 48 to 44 px and the timeline steps away).

- **A sentence on top** answers the usual question: "4 online, 1 not answering. vm-cx22-a41 is the
  busiest at 88%, and deletes itself in 38 min."
- **The table**, one row per machine: the machine (kind icon, a dashed one for a disposable VM,
  name, place and size), status as a shape and a word (Online; Busy at 85% CPU or more, a diamond
  in amber; Not answering, a red square), what it is doing now (the first running command and its
  agent, "+1" for more), CPU as a sparkline of the last minute and a number (amber when busy),
  memory as a bar, browsers, terminals and waiting jobs as counts, and a footer with the totals.
- **Detail on demand.** Click a row (or Enter) and a panel opens beside the table at
  `#/machines/<id>`: status, uptime or time to deletion, size, lanes, a live CPU and memory graph,
  the VM's price and deletion time, every lane with what it does and whether it is working, and
  what waits to run there. The panel is as tall as the window and scrolls inside, so the page
  never does. The table drops columns by its own width (container queries), never sideways
  scrolling. Esc or the X closes it. On a phone the panel comes first, above the list.
- **The last hour** sits under the table: one bar per job, hover for what it was.
- "Add a machine" is the one primary button. It opens a sheet with the three ways (your own server,
  this Linux machine, a disposable VM), each one command, and the steps after.

## 3. Brought from Canvas, kept from Workbench

- **Every finding as measured, expected and fix**, on every card, not only the selected one, with
  the check id and a severity shape and word. The selected finding adds the screens it shows on
  and **Copy for the agent**, which copies one block an agent can act on: the finding, measured,
  expected, where (the selector) and the fix. Findings are ordered worst first.
- **The brand proposal on the real page.** The proposal now leads the Brand page, full width,
  because it is the thing waiting. It shows the Acme product page at 360 px twice, cropped to the
  price, the "In stock" badge, the sizes and Add to cart: as it is (v4) and as it would be (v5),
  with the badge outlined red (4.4:1, under 4.5) and green (5.3:1, passes). Beside it: the token
  change as a small table, the two contrast results, what it changes, Decline and **Approve version
  5** (the screen's one primary, also the A key).
- **Kept from Workbench:** the cause sentence over the before and after, the slider and the
  Before, Slide, After control, and under them the cause and effect as a table (what changed, the
  element, its role: cause, effect, unchanged). The rail, the run page, the measuring bench with
  rulers, the brand's swatches, type, spacing, guidelines and references, the command bar and every
  shortcut.

## 4. On the foundations' token set

The app links `../tokens.css` and nothing else for its values. Where Workbench's own choice was better
for this design I changed the value in `build-tokens.py`, so there is still one generated set
(`tokens.json` and `tokens.css`; the script also refreshes the contrast table and token count in
foundations.html, the only change there). All 88 contrast pairs still pass.

| Token | Was (D1) | Now | Why |
|---|---|---|---|
| `font-family.sans`, `.mono` | Geist, Geist Mono | IBM Plex Sans, IBM Plex Mono, then Geist | Workbench's typeface, the character the owner picked. Geist stays next in the list, so foundations.html (which ships only Geist) renders as before. |
| `color.accent.*`, `focus.ring`, `chart.bar-strong` | Blue `#0a66d6` / `#2b7de6` | Ultramarine `#3442e0` / `#8790ff`, text `#2a36c2` / `#a3aaff` | Workbench's one accent. |
| `color.status.live.*` | Pink `#db1f78` | The accent's ultramarine | Workbench uses one colour for "live and selected". Pink stays the agentos mark's. The shape (a ringed dot) still tells live from selected. |
| `color.neutral.0` to `12` | Pure grey | Warm paper in light (`#f4f4f1` page), graphite in dark (`#0d0e10`) | Workbench's surfaces; text stays a cool ink. |
| `color.terminal.bg`, `.text` (dark) | `#0c0c0c`, `#e4e4e4` | `#0b0c0e`, `#dcdde2` | Matches the graphite. |
| `type.body` | 14/20 | 13/20 | Workbench's density; a phone raises body to 14. |
| `type.secondary` | 13/20 | 12/16 | A second line one step under 13 px body. |
| `type.number`, `type.code` | sans 14, mono 13 | mono 12 | Numbers in Plex Mono with tabular figures read as instrument output, and 12 px mono matches 13 px sans in weight. |
| `type.page-title-compact` | (new) | 20/28 semibold | The page title on a phone. |
| `type.lede` | (new) | 16/24 regular | The sentence under a page title. Both were raw scale steps before, which studio rightly counted as off the system. |

Taken from foundations as they are: the table rules (sentence-case 12/16 head on the inset band,
36 px header, sort on the whole head with the arrow on the far side and `aria-sort`, numbers right
aligned in tabular figures, units in the head, an en dash for no value, the 2 px accent bar on a
selected row drawn with a pseudo-element, not a shadow), the status shapes (dot fine, diamond needs
a look, square failed, ringed dot live, hollow ring idle or waiting; the word takes its colour only
when it asks for attention), radius, rows, control heights, motion, layers, identity hues for the
agent marks, and lines as borders or outlines, never box-shadow. Workbench's uppercase tracked
table heads and eyebrows are gone; sentence case everywhere.

**system_check against tokens.json** (375 and 1440, light and dark, run from a scratch HOME):

| Screen | Colour | Space | Radius | Size | Weight | Line height | Family | Border |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Runs | 100% | 100% | 100% | 100% | 100% | 100% | 100% | 100% |
| Machines | 100% | 100% | 100% | 99% | 100% | 99% | 100% | 100% |
| Needs you | 100% | 100% | 100% | 99% | 100% | 99% | 100% | 100% |
| Home | 56% | 55% | 52% | 67% | 59% | 95% | 98% | 46% |
| A check | 36% | 33% | 36% | 49% | 42% | 91% | 95% | 29% |
| Brand | 59% | 67% | 52% | 76% | 69% | 94% | 99% | 62% |

The two findings left on the first three are the phone's 14 px body (a scale token, not a role
token; see the studio list) and the native select of the prototype-state control. Home, the check
and Brand are low because they show the drawn Acme page, which is another brand on purpose, and
Brand's own swatches and type specimens; every finding listed for them outside those pictures was
fixed. Studio cannot be told "this subtree is a picture" (see below), so the number is honest about
the page and says little about the app.

## 5. UX, measured

Five everyday jobs, counted on the prototype at 1440 by 900: clicks (or key presses), screens of
scrolling, and what has to be read.

| Job | Workbench | App |
|---|---|---|
| **See why a test run failed and what the agent did** | 1 click (the sentence's link, or find the row with "2 failed" among 6 rows). Run page: failures on screen, the agent's steps 1 screen down. Read: a 30-word sentence, five numbers, a 6-row table. | 0 clicks for the gist: Problems shows expected 41.99, received 42 and "Claude Code is already fixing it". 1 click ("See the 2 failures", the first card) for the run page. Read: one 17-word sentence. |
| **Find the worst finding on a page and copy it for the agent** | Scroll 1.5 screens to Design checks, 1 click on the card, the finding is selected; there was no way to copy a finding (the header button copied a ui_check command). A dead end: select the text by hand. | 1 click ("The product page has a blocker" in the second line, or "See the finding" in Problems) opens the check with finding 1 selected; 1 click on "Copy for the agent". 0 scrolling. |
| **Approve a brand change** | 2 clicks (Look, then Approve version 5 in the right column). Read the proposal's 70 words and a generic button drawn twice. | 2 clicks (Look, Approve version 5, or A). The proposal leads the page; the real page in both versions, with the failing badge outlined, says it before the words do. |
| **See which machine is overloaded** | 1 click to Machines, compare five CPU numbers (nothing marks a busy machine). The page was 1,136 px: the add-a-machine panel needed a scroll. | 1 click. The sentence names the busiest machine; its row says Busy with an amber diamond and an amber number; the rail's fleet bars turn amber from any screen. 0 scrolling. |
| **Start a check on a page** | 2 clicks (Checks, Check a page), copy the request: 3. Or ⌘K. | The same 3, or ⌘K, "Check a page". Not shorter: a prototype can only hand the agent the words. The real app could run studio from here (open question). |

**Keyboard only** (checked with a script): Tab reaches the skip link, the rail, the top bar, then
every link and button in order; the slider is one tab stop per card and Left, Right, Home and End
move between cards and scroll them into view; Enter opens; G then H R C M B N jumps; J and K walk
table rows and findings; Esc closes a machine's panel or a sheet; ⌘K and ? work everywhere. Focus is a
2 px ring in the accent, offset 2 px, also drawn around the whole card that holds the focused action.

**At 375 wide:** one column, a tab bar (Home, Runs, Checks, Machines, Needs you, with a dot when
something waits), 40 px controls, the slider swipes at 84% card width, tables drop to their main
column and status, a machine's CPU and lanes move into its row, findings and the brand crops stack.
Nothing scrolls sideways except the slider and the brand tabs, which are meant to.

**What I looked for, and fixed:** more than one primary on a screen (Home had accent Approve
buttons on every approval; they are ordinary buttons now, with a green check, and each screen has at
most one solid ink primary); labels that needed explaining (Workbench's "Design checks" in the rail
and "Checks" in the tab bar are one word now, "Checks"; "Lanes" as a column group gave way to
"Browsers" and "Terminals"); two words for one thing (a "job" is anything running: a run, a check, a
dev server; "Runs" are what `agentos run` starts, and the Runs page lists only those); dead ends (the
finding you could not copy; the machine detail that pushed the add panel off screen); and every
state: loading skeletons with the real layout, the error banner that says nothing was lost, empty
screens that give the one command to start, 20 jobs, 12 machines and 12 approvals, and long names
(a 59-character machine, long branches and titles truncate with a title and wrap on a phone).

## 6. Where it bends web/DESIGN.md, and why

| Rule | Here | Why |
|---|---|---|
| Geist Sans and Geist Mono | IBM Plex | The owner picked Workbench's look; the token set carries it. |
| `live` is pink | Live is the accent's ultramarine, with its own shape | One accent, as Workbench; pink is the mark's. |
| Body 14 px, rows 48 px | Body 13 px, rows 48 (36 compact, 56 relaxed) | Workbench's density with the foundations' rows. |
| Page and cards share one colour | Page and cards differ by one step | The foundations' rule: cards hold their edge without a shadow. |
| `primary` once per view | Kept, strictly: Approve on approvals is an ordinary button | Workbench had an accent button on every approval. |
| Nothing in the page casts a shadow | The screenshot on the check's bench has the popover shadow | A page lying on a bench reads as a picture. |
| Icons from lucide-react | Inline 24 px, 1.5 stroke paths | No build step. |
| Agent marks are official artwork | Initials on an identity hue | Placeholders; the real app uses `AgentMark`. |

## 7. Studio, from a scratch HOME

`agentos studio up --port 7481 --no-skill` with `HOME` set to a scratch folder, the prototype served
over http, the tools called over MCP. `ui_check` on every screen at 360, 768 and 1280 in light and
dark, again with `since` after each round of fixes; `system_check` with `tokens=<tokens.json>` at
375 and 1440; `ui_image` on findings.

| Screen | First run | Last run | What is left |
|---|---:|---:|---|
| Home | 10 | 10 | Inside the drawn pages in the cards (the shop's own faint text and stars, which the check page is about), and the shop's text clipped at the picture's edge. None in the app's own interface. |
| Machines (and with the panel open) | 1 | 1 | A place and size line cut with an ellipsis and a title at 768. |
| Runs | 1 | 1 | A live output line cut with an ellipsis and a title. |
| Checks | 2 | 0 | |
| Needs you | 0 | 0 | |
| A check | 21 | 21 | All inside the drawn pages and the stacked before and after pictures. |
| Brand | 6 | 6 | Inside the drawn page (stars, the struck-out size 40) and a layout shift as the cropped pictures size themselves. |
| A run | 9 | 9 | Output lines scrolled out of the terminal, reported as covered. |

Real problems studio found, fixed:

1. **Text hidden while fonts load.** The local fonts used `font-display: block`; in studio's fresh
   browser some screens were captured blank ("loaded but shows nothing"). Now `swap`.
2. **A run-away layout.** An aspect ratio on cropped pictures made a grid item size itself from its
   own height; the page grew to 33 million px and the capture crashed (`TargetClosedError`). Found
   because system_check failed on Home; removed.
3. **A truncated time** ("· 3 min ago") in the phone form of the Checks table, and a path cut with no
   way to read it. The time now has its own column; long cells wrap on a phone.
4. **Phone rows in Machines** cut "58% CPU · 4 browsers · 3 terminals"; it wraps now, and the place
   and size line steps away on a phone (the panel has it).
5. **Swatch values cut** ("#EEF4F") at tablet width: the ramp goes to two rows of five there.
6. **A finding pin cut by a picture's edge** in the slider: in small pictures the pin sits inside its
   outline.
7. **Off the system:** raw 16 and 20 px sizes (now the `lede` and `page-title-compact` roles), a 1.5 px
   padding in the live mark and a 64 px padding (now `space-px` and `space-16`).

My own eye found the rest: the slider showed exactly 3 cards with nothing peeking (a class bug), the
dev server card said its time twice, the slide labels counted the Up next card, a design blocker was
counted as a test failure in the sentence, the Problems list said "still running" when nothing was,
machine details opened below the list on a phone, 20 dots on a phone, the swatch chips collided with
the filter chips' class, the findings panel sat 16 px lower than the bench, the diff's change box
washed out the heading it pointed at, a `kbd` hint on a phone button, the timeline's "now" on top of
13:54 at 375, "3 opens", and the empty space under the Machines panel that made the page scroll by
40 px.

### Where studio was wrong or awkward

1. **Pictures are judged as page.** The drawn pages are `inert` with a text alternative; their text
   still counts for contrast, cut text and covered text, and for system_check. On Home, the check
   and Brand that is every finding left, and the token percentages fall to a third. A
   `data-studio-skip` or "treat `inert` and `role=img` subtrees as pictures" would make the verdict
   useful on any page that shows other pages. (Third design job to report it.)
2. **Scrolled-out lines are "covered"** by whatever sits where they would be (the run's terminal).
3. **Stacked before and after pictures** are "covered" by each other.
4. **`text.truncated` ignores a `title`** and a visible way to read the rest (the machine panel).
5. **Raw scale tokens count as hard-coded.** `font-size: var(--font-14)` is a token, but the
   provenance probe moves only the role tokens and reports "did not move when the tokens changed".
   Either probe every token, or say "a scale token where a role exists".
6. **Live pages confuse the probe.** On Home, elements the page replaces every second (the progress
   row, terminal lines) were reported as hard-coded in dark mode, values that are `var()` in the CSS.
   A "this page changes on its own" note, or measuring after pausing timers, would avoid it.
7. **`ui_image` crops are tiny**: 34 by 9 px for a pin on a card picture. A margin and the screen with
   the box drawn would answer "what is this" in one call. (Reported before.)
8. **A blank capture's cause hides in the coverage text.** The real cause of two blank screens was my
   test server dropping connections when studio loaded six screens at once
   (`ERR_CONNECTION_RESET`); a default `python -m http.server` is not enough for studio. The verdict
   said "the app may have failed to render"; leading with the console error would point at it first.
9. **`wait_for` failed on one screen in ten seconds** (fonts held the text invisible), and the coverage
   said "check the selector" when the selector was fine.
10. **Finding ids follow the DOM path**, so a delta says "8 worse" for the same picture text seen on
    more screens once a capture that failed before rendered.
11. **The agent token is only in the banner** of `studio up`, so a script reads the log. (Reported
    before.) Firefox and WebKit are reported missing under a scratch HOME.
12. **The Python MCP client in this repo's environment** names it `streamable_http_client` with an
    `httpx2` client for headers; the studio docs' examples assume the older `streamablehttp_client`.

## 8. What I am unsure of

- **Home's sentence at 24 px.** Calm, and it fits the tool; the owner liked Canvas's bigger one. It
  is one token (`type.page-title`) to try larger.
- **Live in the accent colour.** On Home most things are live, so the accent is everywhere there;
  with 20 jobs the cards' ringed dots repeat a lot. A quieter live mark on cards, keeping the
  colour for the card you focus, might be better.
- **Cards with pictures for 20 jobs.** The slider handles it, but a person with 20 jobs may want the
  list. The Runs page is that list; a "List" switch on Home is cheap if it is missed.
- **Starting a check from the app.** Today a person copies a request for their agent. If the panel
  could call studio itself, job 5 drops to 2 clicks.
- **The proposal crop at 360 px** is small inside a 1440 page. A 768 crop would show the button
  larger but the badge less clearly.
- **Plex and the warm paper** are the easiest choices to undo: two lists in `build-tokens.py`.

## 9. Images

Every screen and state at 375, 768 and 1440, light and dark, in this checkout (git-ignored, 32 MB):
`panel/prototypes/dashboard/app/shots/`. `<screen>-screen@<width>-<theme>.png` is the first screen as
the window shows it (1440 by 900, 768 by 1024, 375 by 812), which is what "no scrolling" is judged
on; `<screen>@<width>-<theme>.png` is the full page. Screens: `home`, `home-one-job`,
`home-nothing-running`, `home-many`, `home-loading`, `home-error`, `home-empty`, `runs`, `run`,
`run-build`, `checks`, `check`, `check-finding-3`, `machines`, `machine-open`,
`machine-not-answering`, `machines-many`, `machines-empty`, `brand`, `needs`, `needs-empty`. Make
them again with:

```
uv run python panel/prototypes/dashboard/app/shoot.py               # everything
uv run python panel/prototypes/dashboard/app/shoot.py home machines # some screens
WIDTHS=1440 THEMES=dark uv run python panel/prototypes/dashboard/app/shoot.py
```

## 10. Building web/ from it

The order in foundations.md section 5 holds: tokens and the Tailwind map first, then the table and
status primitives. Then, for this design: the job card and the slider (one component: picture slot,
kind, title, who and where, progress, one action; the track with keys, drag and paging), the
Problems list (fed from failed runs, blocking findings and unreachable nodes), the machines table
with its side panel on `/machines/:id`, and the finding card with "Copy for the agent". The job
pictures need real sources: `lane_job_read` for the last lines, studio's captures for the page
pictures, and a decision on what a card shows before the first capture exists (the prototype would
show the kind's icon on the inset surface).
