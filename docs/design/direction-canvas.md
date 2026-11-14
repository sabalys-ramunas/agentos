# Direction: Canvas

Status: exploration, 2026-11-12. One of two directions for the new centre of the product (the
other is Workbench). The prototype is
[panel/prototypes/dashboard/canvas/index.html](../../panel/prototypes/dashboard/canvas/index.html):
open it straight from disk, no build. Mock workspace: Acme Shop, a web shop (acme-web, acme-api)
and its iOS app (acme-ios), three agents (Claude Code, Codex, T3 Code) and four machines.

## The idea in three sentences

Show the work as what it looks like: the page an agent is building, the output of the test it
is running, the before and after of its change, the machines as a map of what runs where. Every
piece of work sits on a drafting canvas as a frame with a small label above it, the way a
designer lays out screens, and an agent at work right now draws a pink ring around its frame.
Words explain the picture in one sentence; numbers, ids and commands are one look away, never
the first thing you see.

## The first five seconds

"My agents are busy, and I can see what they are making." Then, before reading anything: one
pink frame is a test run streaming green ticks, one frame is my checkout page with five
numbered marks on it, and one test run failed but is already being fixed. The headline says it
in a sentence a non-technical person reads: "3 agents at work. A test run failed, and Claude
Code is already fixing it." What needs me is on the right, small and calm, with Deny and
Approve.

## Screens

| Screen | Route | What it shows |
|---|---|---|
| Home | `#/` | Headline, status chips, Live now (4 frames: a design check at 3 widths with numbered problems, the live test run, a before and after with the cause sentence, a release build on a disposable VM), Today (table), Needs you, Machines |
| A dev task, open | `#/runs/job-3b77aa10` (failed run 1), `#/runs/job-4c1e9a2f` (run 2, live) | Sentence of what happened, attempts strip (Run 1 failed, the agent, Run 2 live), terminal, test files table with time bars, what failed (expected and received, the source line), what the agent did next (timeline), ran on (branch, change, files sent, machine, lane, CPU during the run) |
| A design check, open | `#/checks/chk-checkout` | 6 screens (3 widths, light and dark) as thumbnails with counts, the chosen screen in a device frame with problems outlined and numbered, each problem with measured, expected and fix (hover a card and its mark lights, click one seen elsewhere and the screen switches), on the design system (ring, bars, a table of values that only look like a token), what this change did (slider, side by side, changes) with the cause sentence |
| What a change did | `#/checks/chk-product` | The product page diff: "The heading wrapped to 2 lines, which moved 17 elements down by 40 px." |
| Machines | `#/machines` | A rack on the canvas: agents on the left, wires to the lane each one is using now, one wide card per machine (load, meters, what it runs, its lanes as pictures), the VM's time left and cost, Add a machine (dialog with the three real commands), the work queue (table) |
| Brand | `#/brand` | acme/shop: an agent's proposal for version 4 (swatches before and after, the real button before and after with contrast, why, Deny and Approve), colour tokens as light and dark swatches and as a table, type specimens, spacing, corners, widths, guidelines, references |
| Needs you | `#/needs` | Approvals as one part: a brand change, a new lane, a VM that costs money, a post |
| Runs, Checks | `#/runs`, `#/checks` | Runs as a grouped table, checks as a gallery of page frames |

States: `?state=loading`, `?state=error`, `?state=empty` on every screen; `?many=1` adds 18
runs with long branch names, 7 problems, 10 approvals and two more machines (one away, one with
a very long name). `?theme=dark|light` forces a theme; the moon and sun button switches it and
remembers the choice.

Interactions that work: navigation, hover and keyboard focus on every row and card, the screen
picker, finding to mark highlighting, the before and after slider (drag, or arrow keys, Home,
End), the three compare modes, approve and deny (the card leaves, a toast says what happened),
approving the brand change (the mark, swatch and table turn to the new orange, a banner says v4
is live), Add a machine with a copyable command, the live test count and streaming output.

## The choices that carry it

- **Frames on a canvas.** A dotted drafting sheet holds the live work. Each frame has a
  Figma-style label above (icon, name, the tool and time) and one plain sentence below with the
  agent's mark. The same frame is used for pages, terminals, diffs and builds, so the eye learns
  one shape.
- **Real pictures, drawn.** The shop pages are real HTML at their real width (360, 768, 1280),
  scaled into the frame, so a problem's outline sits exactly on the element it is about and a
  dark screenshot stays dark inside a light app. Terminals are real output. No grey boxes.
- **Colour means one thing each.** Pink: an agent acting now (the ring, the wire, the dot), as
  in web/DESIGN.md. Red marks and amber marks: major and minor problems on a picture. Blue:
  what changed, links, focus. Green: passed. Everything else is warm greys, so the pictures are
  the colour on the page.
- **Type.** Geist for the interface, Geist Mono for commands, ids, branches and machine names.
  A large, tight headline (46 px, weight 600) on Home that says the state in a sentence; 32 px
  page titles; 14 px body.
- **Density.** Generous on Home and on the pictures, tight in tables and in the problems list.
  A developer reads a table at a glance; a designer reads the picture.
- **Motion that explains, nothing else.** The live ring's dot pings, the test count and its bar
  climb, new output lines arrive, light travels along a wire from the agent to the lane it is
  using, a decided card slides away. All of it stops under reduced motion (the wire becomes a
  steady line).
- **Tables designed as the product.** One component everywhere: a sunken 34 px header in
  12 px medium text, sentence case, sort affordance that appears on hover and stays on the
  sorted column; 52 px rows with a picture of the work in the first cell (page thumbnail or
  terminal), a title and a mono sub line; status as a shape and a word with the result under
  it; numbers right aligned in tabular figures; group rows ("Running now 2", "Failed 1") inside
  the table; a 3 px pink or red edge for running and failed rows; on a phone each row becomes a
  stacked card with the main cell on top and the secondary columns folded away.

## Where it bends web/DESIGN.md, and why

| Rule | What Canvas does | Why |
|---|---|---|
| "Nothing in the page casts a shadow" | Frames and machine cards cast a soft shadow on the canvas | They are objects on a sheet; the shadow is what makes the canvas read as a surface and the frames as things you can open. Panels, tables and lists keep hairlines only. |
| "Add shadows, gradients or tinted cards: do not" | The board has a dotted background; a live lane tile is tinted pink at 12 to 15% | The dots are the canvas. The pink tint is the one place "acting now" needs to be found across a rack of lanes at a glance. |
| Status colour "never a panel" | The proposal card has a 3 px amber top edge | It is the one thing on the brand page waiting for a yes. An edge, not a fill. |
| `text-32` is "rare" | Home's headline is 46 px | The headline is the screen's summary, read first by everyone. It is the one big moment per screen. |
| Rail words: Home, Needs you, Accounts, Computers, History | Home, Needs you, Runs, Checks, Machines, Brand | The centre moved to dev and design work. "Runs" and "Checks" are what the tools call them (`agentos run`, `ui_check`). "Machines" over "Computers" because the owner and the docs say machines and nodes; open question below. |
| Agent marks are official artwork | Initials on a muted colour (CC, Cx, T3) | Placeholders in a prototype. The real app should use `AgentMark`. |
| Primary button once per view | Needs you cards each carry Approve | Each card is its own decision; this already holds in the current app's Needs you. |

Kept as is: Deny on the left and Approve on the right everywhere, a shape and a word for every
state, mono for ids, tabular numbers, the 4 px grid (studio pushed me back onto it, see below),
the radius steps, sentence case, no em dash.

## How it was checked

- **Images:** every screen at 375, 768 and 1440, light and dark, full page, plus the first
  phone screen with the tab bar where it sits (`<screen>-screen@375-<theme>.png`), plus the
  loading, error, empty and many states.
- **Studio** from a scratch HOME (`agentos studio up --port 7491`): `ui_check` on Home, a run,
  the checkout check, Machines and Brand (6 screens each), `ui_check` again with `since` on
  Home (27 fixed), `system_check tokens="page"` on Home and Machines, and `ui_image` on one
  finding.

What studio found that was real, and is fixed:

1. Terminal text in the release build log and in table thumbnails took the page's ink colour:
   1.06 : 1 on the terminal background. A real bug, invisible in dark mode.
2. Rail links had no name at tablet width, where the labels are hidden and only icons show.
3. White numbers on red marks (3.55 : 1), on light blue in dark mode (2.39 : 1), on pink in
   dark mode (2.88 : 1), and the Claude mark (4.35 : 1). New pin colours, dark text on pink in
   dark mode, a darker Claude hue.
4. A "seen on another screen" mark drawn at 55% opacity (2.37 : 1). It is now a ring.
5. Secondary text a hair under 4.5 : 1 in places (the dark theme's tertiary grey); raised.
6. The phone "All" link was 20 px tall.
7. Long terminal lines were cut with an ellipsis; the big terminal now scrolls sideways.
8. Lane titles cut to "Review br…" at 1280.
9. Spacing: `system_check` measured 1% of spacing on tokens, and many values off the 4 px grid
   (6, 7, 9, 10, 11, 14, 15, 22, 26 px). Every gap, padding and margin is now on the grid and
   uses `--space-*` tokens: 43% on tokens by studio's count (the rest are 1 to 3 px nudges and
   the drawn shop pages, which are another brand).

My own eye found the rest: the drawn phone checkout overflowed its 360 px (a grid column sized
to a no-wrap label) and its pay bar fell below the picture, a before and after crop that missed
the heading (so its mark landed on the caption), wires running under machine cards (the rack
layout fixes it), a table too wide for its panel at 1440, a "Product page page" title, blank
lines in every terminal (whitespace rendered as `pre`), the empty state's steps breaking apart,
and "pnpm test329 of 414" at 768.

Not changed on purpose: the drawn shop pages keep their faults (they are the findings the
check screen is about) and their own colours in dark mode; the Acme mark is a white letter on
the brand's orange (a logo).

## Studio: where it was wrong or awkward

1. **Text in pictures marked `aria-hidden` is still judged.** Thumbnails and terminal previews
   are `aria-hidden="true"`; their 5 px text still counted for contrast, truncation and
   occlusion. Same as problem 2 of the last design job, still open.
2. **No way to say "this subtree is another brand".** The drawn shop is a different design
   system on purpose. `system_check tokens="page"` judged its colours against the app's tokens
   (41 of 97 findings), and `theme.leak` judged its fixed light colours. A
   `data-studio-ignore` or "fixed on purpose" marker would remove most of the noise.
3. **Content clipped by `overflow: hidden` is reported as covered by whatever sits there.**
   The shop's "Street" field, hidden below the picture's edge, was reported as covered by the
   findings card under the picture. `ui_image` with that finding's crop showed the findings card,
   which is how I found out. Scrolled-out content in a horizontal scroller (the phone's frames
   carousel) did the same and measured 1 : 1 contrast.
4. **Sticky elements in the full-page capture.** "Live now" at 360 was measured against the
   orange of the sticky top bar's mark (3.16 : 1), and the machines page's sticky agents column
   was reported over a lane's status. The capture seems to paint sticky elements over content
   that is far from them on a real screen.
5. **`ui_image` crops are tiny and without context.** 65 by 30 px for a finding: too small to
   see where on the page it is. A margin around the crop, or the screen with the box drawn,
   would answer "what am I looking at" in one call.
6. **Finding ids follow the DOM path.** When a table's rows re-ordered between runs, the same
   drawn-shop findings came back as 15 "new" and the old ones as "fixed". `since` reads better
   when ids follow text and check rather than `tr:nth-of-type(5)`.
7. **Rings counted as shadows.** `box-shadow: 0 0 0 1px var(--line)` is a border drawn as a
   shadow (common, it does not change layout). `token.shadow` reported 23 of them.
8. **Radius tokens are found by name only** (still): `--r-md` was not seen, `--radius-md` was. I
   renamed mine.
9. **The summary shows 6 findings, `detail="full"` re-runs the whole capture.** Asking for the
   full list after a summary cost a second 6-screen run. Reading the full list of the run just
   made (`finding_get` per id, or a `run` read) would be cheaper.
10. **The token only appears in the banner**, so a script has to scrape the log. Same as
    last time.
11. **Pixel contrast on anti-aliased text over the dotted canvas** reported 2.4 to 3.2 : 1 as
    the worst pixel where the median was 4.6 to 5.1. A real hint once (the grey was a little
    light), noise after.

## What I am unsure of

- **Is it too much picture for a developer?** The live frames take most of Home's first
  screen. A developer with ten runs may want the table first. A density switch (Canvas or
  List) on Home might be the answer; I did not build it.
- **Pink as the only "live" colour** works on Home (two frames) but would get loud with eight
  agents. The rule might need "the ring only on the frame you last looked at".
- **The drawn pages are a prototype's luxury.** The real thing needs studio's captures as the
  pictures (they exist: capture keeps images per screen). How fresh they are, and what a frame
  shows before the first capture, needs designing.
- **"Machines" or "Computers".** The real app says Computers. I would pick one word with the
  owner.
- **The rack on a big fleet.** Four machines are generous. Twenty would need the cards to
  fold to one line each, with lanes as small tiles.

## With another day

- A Canvas or List switch on Home, and a frame that can be pinned ("always show the checkout
  page at 360").
- The run page's terminal with search, jump to the first failure, and the failing test's line
  linked to the source.
- A check page for the iOS app (device frames, Dynamic Type sizes), since Acme ships one.
- Keyboard: `j` and `k` through problems, `[` and `]` through screens, `Cmd-K` search.
- Rebuild on D1's foundations (tokens and tables) once they land, so both directions compare
  on the same base.

## Images

In this checkout, git-ignored (26 MB): `panel/prototypes/dashboard/canvas/shots/`. Every screen
as `<screen>@<width>-<theme>.png` for widths 375, 768, 1440 and themes light, dark (full page);
`<screen>-screen@375-<theme>.png` is the first phone screen as it looks in the hand. Screens:
`home`, `run`, `run-live`, `runs`, `checks`, `check`, `check-diff`, `machines`, `brand`, `needs`,
and the states `home-loading`, `home-error`, `home-empty`, `home-many`, `check-many`,
`machines-many`. Make them again with:

```
uv run python panel/prototypes/dashboard/canvas/shoot.py              # everything
uv run python panel/prototypes/dashboard/canvas/shoot.py home check   # some screens
WIDTHS=1440 THEMES=dark uv run python panel/prototypes/dashboard/canvas/shoot.py
```
