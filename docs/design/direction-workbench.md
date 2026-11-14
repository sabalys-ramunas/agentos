# Direction: Workbench

Status: exploration, 2026-11-12. One of the directions the owner chooses from. The prototype is
[panel/prototypes/dashboard/workbench/index.html](../../panel/prototypes/dashboard/workbench/index.html)
(plain HTML, CSS and JS, no build step; open the file or serve the folder). Images of every
screen are in `panel/prototypes/dashboard/workbench/shots/` (git-ignored, 16 MB, rebuilt by
taking the screenshots again).

## The idea in three sentences

agentos is the bench your agents work at, so the web UI is a precise tool that shows the work
itself: the test run with its output, the page with its findings drawn on it, the machine with
its load. Dense but calm, keyboard first, with type and alignment doing the work, monospace
wherever output is output, one accent and clear status. Everything moves only because the work
moves: numbers tick, output streams, the last hour fills in.

## The first five seconds

On Home, a person should feel "my agents are busy, things are in hand, and I know the two
things that need me". The first sentence says it in plain words (5 jobs running on 4 machines,
2 tests failed and Claude Code is on it, the product page has 1 blocker, 6 things need you).
Under it, the last hour on every machine as one picture, then what is running now with its
live last line, then what needs a yes. A developer recognises a serious tool; someone who has
never run a test can still read the sentence and the colours.

## Screens

| # | Screen | Route | What it shows |
|---|---|---|---|
| 1 | Home | `#/` | Status sentence, five numbers, the last hour per machine, running now (live output line, progress, time), needs you, design checks with drawn pages and markers, machines with live load, finished today |
| 2 | A dev task, open | `#/runs/job-7f3a2c91` | `agentos run -- pnpm vitest run` on hel-1: a map of all 440 tests filling in, the streaming output with filter, wrap and follow, the 2 failures with expected and received and the code line, what the agent did and does next (it arrives step by step when the run ends), the change it ran on |
| 3 | A design check, open | `#/checks/run_8d2f` | The product page at 360, 768 and 1280 in light and dark; findings outlined and numbered on the drawn screenshot on a measuring bench with rulers; each finding with measured, expected, where and the fix; how much is on the design system and the values that only look like a token; what the change did as before and after with a slider and the cause sentence |
| 4 | Machines | `#/machines` | A laptop (studio only, a Mac cannot be a node), a VPS, a spare box, a disposable VM with its price and delete time, and one not answering; load, lanes (browsers, terminals, services), what runs, the queue; a side panel with a live graph and every lane; three ways to add a machine and an install sheet |
| 5 | Brand | `#/brand` | The acme/shop product profile: the blue ramp as swatches, what each colour is for in light and dark, type set in the brand's own font, spacing, corners, checked and advisory guidelines, references drawn as small pictures, and Claude Code's proposal for version 5 with the Add to cart button before and after, the token diff and Approve |
| 6 | Home on a phone | `#/` at 375 | The same Home in one column with a bottom tab bar; check cards scroll sideways |
| | Also | `#/runs`, `#/checks`, `#/needs` | Runs table (sortable), checks table, the full approvals list |

States: the "Prototype state" control at the foot of the rail (or the command bar) switches
any page to loading, machine not answering (a stale banner, nothing lost), and empty (what to
run first, as commands to copy). `?run=done` opens the run already finished.

Interactions that work: navigation, `⌘K` command bar with search over pages, runs, checks and
machines, `G` then `H R C M B N` to jump, `J`/`K` and Enter in lists, `A` to approve the first
thing waiting (and Approve version 5 on Brand, with Undo), `/` to filter output, `F` follow,
`W` wrap, `1` to `6` to pick a screen in a check, `T` theme, `?` the shortcut list; hover and
selection on rows; picking a finding outlines it, switches to a screen where it shows and
scrolls the bench to it; the before and after slider (drag, or the range under it, or the
Before / Slide / After control); the theme switch; sortable table headers.

## The choices that carry it

**Type.** IBM Plex Sans for words and IBM Plex Mono for output, ids, branches, numbers and
commands. Plex has the engineered, slightly technical character of a measuring instrument and
is not what every other dev tool uses (Geist, Inter), so the product reads as its own. The mono
has tabular figures and a slashed zero. Base size 13 px, labels 11 px uppercase with tracking,
headings 20 px at weight 600. The drawn shop uses DM Sans, so the product and the pages it
checks never look alike.

**Colour.** One accent, ultramarine (`#3442e0` light, `#8790ff` dark), used for what is live
and what is selected: a running job, the current row, focus, the now line. Status has its own
colours and its own shapes, so it never depends on colour alone: a round dot for fine, a
diamond for failed, a triangle for waiting on you, a hollow ring for idle, a hollow square for
queued, a pulsing dot for running. Surfaces are warm paper in light (`#f4f4f1` page, white
panels) and graphite in dark (`#0d0e10`, `#131417`), each set chosen, not inverted. Identity
marks (an agent, the workspace) keep their colour in both themes.

**Density.** 40 px table rows, 13 px text, panels with hairlines and no shadows. A screen holds
the whole story without scrolling at 1440 (run page: facts, test map, output, failures and next
steps side by side). On a phone everything grows to 14 px text and 36 to 52 px targets.

**Tables.** Designed as product, since the owner named table headers: a sunken header band,
11 px uppercase labels tracked at 0.055em in the tertiary colour, units in mono after the label
(`LOAD cpu`), group headers over related columns (`TESTS` over Passed and Failed, `FINDINGS`
over Blocker, Major, Minor) with a hairline under them, numbers right-aligned in mono with
tabular figures, a dash for no value, a sort control that shows on hover and stays with an
arrow on the sorted column, the selected row with an accent bar on its left edge, a footer row
with totals, and a main column that takes the spare width and truncates with a title.
Secondary columns step away at 1400, 1100 and 760 px and their values move into the main cell.

**Motion.** Only where it explains: the live dot pulses, a new output line flashes once in the
accent, the agent's next steps rise in one by one, the running bar end blinks on the timeline,
markers glow when picked. All of it is off under reduced motion; values still update.

**Pictures.** Wherever a picture is faster than words: the drawn shop page (HTML at its real
width, scaled down, with markers placed from the drawing itself), the 440 squares of the test
map, the last hour as bars per machine, live load sparklines, the brand's swatches and its
button before and after, small drawings of reference sites. No grey boxes.

## Where it bends web/DESIGN.md, and why

| Rule in web/DESIGN.md | Workbench | Why |
|---|---|---|
| Geist Sans and Geist Mono | IBM Plex Sans and Mono | A character of its own for an instrument; Geist reads as Vercel. If the owner keeps Geist, every other choice here still stands. |
| Body 14 px, table rows 48 px | 13 px, rows 40 px (52 on a phone) | The audience reads output and tables all day; 48 px rows show too little of a run. Phones keep large targets. |
| Table header in `text-faint`, plain | Sunken band, 11 px uppercase tracked, units, group headers, sort affordance, footer | The owner said table headers need a lot of work; a header is the table's ruler and should look like one. |
| `live` and `--current` are pink (`#d3186e`) | Live is the one accent, ultramarine | One accent instead of brand pink plus blue links. Pink stays the logo's. |
| Status as a dot and a word | Dot and word, plus a shape per state | Failed, waiting and idle must differ without colour, also at 8 px. |
| Technical details off by default | Commands, branches, lane ids and job ids are shown, in mono, secondary | The users are developers and designers running agents; the command is the plain name of a run. A non-technical reader still gets the sentence and the title first. |
| `primary` once per view | Approve uses the accent and appears once per waiting item | Approving is the point of each item in Needs you. |
| Nothing casts a shadow | Unchanged; the drawn screenshot on the bench has one | A page lying on a bench reads as a picture, not as UI. |
| Icons from lucide-react | Inline strokes in the same 16 px, 1.5 style | No build step in a prototype. |

## What studio measured

Studio ran from a scratch HOME (`agentos studio up` on port 7461), never the real `~/.agentos`,
over MCP: `ui_check` on Home, the run, the check, Machines and Brand at 360, 768 and 1280 in
light and dark, then again with `since`; `system_check tokens="page"` on Home and Machines at
375 and 1440; `ui_image` to look at a finding.

| Page | First run | Last run | What is left |
|---|---:|---:|---|
| Home | 30 | 8 | All inside the drawn shop page, plus identity marks (see the studio list) |
| Run | 14 | 8 | Output lines scrolled out of the terminal reported as covered; long values with an ellipsis and a title |
| Design check | 57 | 32 | Almost all inside the drawn pages and the stacked before and after layers |
| Machines | 20 | 3 | Two ellipses with a title, one identity mark |
| Brand | 10 | 1 | The 4.4:1 "New colour" chip, shown on purpose as the problem the proposal fixes |

Real problems studio found and that are fixed: the run name column shrank to 24 px at 768
(`ui_image` showed it), the machines table scrolled sideways under the side panel at 1280,
class names from the drawn shop leaked into the UI (a thumbnail label went 2.1:1 in dark),
findings "not on this screen" faded with opacity until unreadable, agent chips and finding
numbers at 3.4 to 4.3:1, line numbers in the terminal at 2.6:1 on the failure highlight,
timeline bars as 4 px links (now a picture with a text alternative), output lines cut with no
ellipsis, translucent tab bar text, the separator and clock in the top bar, an invisible range
input laid over the before and after picture (now a visible slider under it, and drag on the
picture). `system_check` found 75% of colours and 0% of shadows on the page's own tokens on
Home; identity colours, severity colours and ring shadows are now tokens. It was not measured
again after that change.

### Where studio was wrong or awkward

1. **Text inside a picture is checked.** The drawn shop page is `inert` and `aria-hidden`, and
   its text still counts for contrast, theme leaks, cut-off text and covered text. On the check
   page this is most of the 32. A way to say "this is a picture" (skip `aria-hidden` subtrees,
   or a `data-studio-skip`) would make the verdict useful on any page with previews.
2. **Identity marks are reported as theme leaks.** White letters on an agent's or workspace's
   colour are the same in both themes on purpose.
3. **Scrolled-out content is reported as covered.** Terminal lines scrolled above the visible
   output are "covered by" the facts row above the panel; the measuring bench the same.
4. **Stacked layers are reported as covered.** A before and after slider is two pictures on
   top of each other, clipped; every word of the lower one is a finding.
5. **Content hidden by a parent's overflow is "covered by" the next sibling** (the slider
   under the clipped picture).
6. **`text.truncated` ignores a `title`.** Its own fix says "end it with an ellipsis and a
   title", and the finding stays after both are there.
7. **Pixel contrast on thin glyphs.** A `/` separator and a light mono clock measure 3.3 to 3.7
   while their colour is 5.3:1 by value. Reading the computed colour for plain text on a plain
   background would avoid it.
8. **No way to mark an exhibit.** The brand proposal shows the failing 4.4:1 chip as "now";
   studio cannot be told that this is the point.
9. **`system_check` and shadows.** A ring made from a colour token (`0 0 0 1px var(--line)`)
   counts as an off-system shadow, and the nearest-token suggestions cross meaning (an agent's
   orange to `--warn`, a site's brand blue to `--accent`).
10. **`ui_image` crops are too tight.** The crop is the element's own box (24 by 20 px here),
    with no margin, so you see "Si..." and must guess where it is.
11. **Reduced motion is applied but not said.** The capture shows the reduced-motion style (a
    still ring instead of a pulse); the result should say so.
12. **Reading every finding is slow.** `more.ids` plus one `finding_get` each took minutes;
    `detail="full"` is the only practical way, so it may as well be the hint in `more.how`
    first.
13. **The verdict counts are dominated by noise** on pages with pictures (57, 56 of them
    "major"), so the number alone does not tell an agent whether the page got better.

## What I am unsure of

- **The font.** Plex gives character, and it also departs from the real app's Geist. It is
  the easiest choice to undo.
- **Is it too dense for the owner's non-technical reader?** The sentence on Home, the words on
  every status and the pictures are meant to carry them; the tables do not try to.
- **The last-hour timeline.** It is the most "screenshot" element and also the one a
  newcomer needs one look to learn. It might need a one-line caption the first time.
- **Five numbers on Home.** "Tests today" and "On the design system" earn their place; "VMs
  this month" may belong on Machines only.
- **A light terminal in light mode.** Honest to the theme; some will expect dark output always.
- **Approvals as one panel among others.** It follows the brief; a person who only approves
  may want Needs you first on a phone.

## With another day

- Wire the screens to the real API shapes (`lane_job_read`, studio runs, `profile_show`) and
  replace the scripted run with a recorded one.
- A "contact sheet" for a check: all six screens side by side with their markers, the fastest
  way to see where a finding shows.
- Rulers that stay put while the bench scrolls; zoom on the bench.
- Every table keyboard-complete (`J`/`K`, Enter, `X` to select several, `S` to sort) and the
  table spec written up for the designer rebuilding the base tokens.
- A diff view per changed element, with its own crop, and a pixel heatmap layer.
- The run page on a phone as its own layout (failures first, output on demand).
- Measure again with studio after the token pass, and the same checks with a pinned font.
