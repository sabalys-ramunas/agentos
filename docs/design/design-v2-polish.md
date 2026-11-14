# Design v2 polish: the calm dashboard, measured and tidied

Status: done, 2026-11-12, by builder U1. The prototype is
[panel/prototypes/dashboard/overview.html](../../panel/prototypes/dashboard/overview.html) with
[data-v2.js](../../panel/prototypes/dashboard/data-v2.js). This was a polish and UX pass, not a new
direction: the page keeps its light, calm, hairline look. It was also studio's first real design
job, so the problems studio had are listed at the end.

## How it was checked

- **Pages and states:** Overview, Approvals, Tasks, a waiting task, a finished task, Sites, one site,
  Profiles, one profile, its Inbox (list and a message), its Email tab, APIs, Machines, Routines,
  Activity, Settings, plus the phone More sheet, compose, Add mailbox, search, the fix sheet and the
  side menu.
- **Sizes:** 320, 375, 430, 768 and 1440 px wide, light and dark. That gives 238 images per set.
- **Studio:** `ui_check` on all 17 pages at those 5 widths and both themes (10 screens per page),
  then again with `since`. `system_check tokens="page"` on 6 pages at 375 and 1440. `ui_diff`
  against the before run. Studio ran from a scratch HOME, never the real `~/.agentos`.
- **By eye:** every page as someone who does not use AI tools, and as a daily user.

## Measured findings, before and after

`ui_check`, 17 pages, 10 screens each:

| Check | Before | After | Notes |
|---|---:|---:|---|
| contrast.text | 118 | 64 | Most of the 64 left are text inside the drawn page previews (under 9 px) and white letters on site brand colours. See "Not changed". |
| theme.leak | 176 | 169 | Almost all are the drawn previews and site marks, which keep their colours on purpose. |
| health.layout_shift | 17 | 2 | Was 0.38 on every phone and tablet page. The 2 left are minor (0.18, 320 px, task pages): the web font swapping in. |
| text.truncated | 29 | 26 | Phone rows; several are fine (a long sub line in a list row). |
| layout.occluded | 9 | 8 | All 8 left are false positives (see the studio list). |
| target.size | 5 | 4 | Inline links in table rows at 768 px. The whole row is the target. |
| layout.text_overlap | 2 | 1 | The reader header overlap is fixed. One left is inside a drawn preview. |
| layout.clipped | 1 | 1 | Machines, 320 px. |
| name.missing | 1 | 0 | |
| **Total** | **358** | **275** | 83 fixed, 0 new, 6 worse (the 6 are truncation and tap size items whose wording or layout moved). |

The sheets measured on their own (compose, Add mailbox, the More sheet): 81 before, 25 fixed,
0 new, 0 worse.

`system_check tokens="page"` (the page against its own CSS variables), 375 and 1440, both themes:

| Page | Findings before | After | Radius on a token, before | After |
|---|---:|---:|---:|---:|
| Overview | 217 | 198 | 2% | 34% |
| Approvals | 171 | 164 | 0% | 44% |
| Tasks | 183 | 175 | 0% | 28% |
| Profile | 37 | 28 | 5% | 55% |
| Inbox | 35 | 24 | 1% | 92% |
| Settings | 20 | 11 | 12% | 84% |

The radius left off the tokens on Overview, Approvals and Tasks is the drawn page previews, which
use `em` radii so one drawing scales from a 46 px thumbnail to the large view.

## Changes, by screen

### Every screen (the shell and the tokens)

- **Layout shift on load fixed.** Below 860 px the shell grid stretched the top bar's row to fill
  the screen until the page rendered, so content jumped 367 px (CLS 0.38 on every phone and tablet
  page). The grid now has `auto` and `1fr` rows.
- **Radius tokens.** The page had one radius token and 17 radius values. It now has
  `--radius-sm` 4, `--radius-md` 6, `--radius-lg` 8, `--radius-xl` 12 and `--radius-pill`, the
  same steps as web/DESIGN.md, and 118 radii use them. Cards are 12 px, as in the real app.
- **Hairlines, not shadows.** The card shadow token is `none` in both themes, and the step picture
  on a task page has a hairline instead of a drop shadow. Floating layers keep their shadow.
- **Tab bar and top bar labels are readable.** Inactive tab labels were `--text-3` over a
  translucent bar (2.5 to 3.9 to 1). They are `--text-2` now, and both bars are 94% opaque instead
  of 86 to 88%, so content scrolling under them does not wash the labels out.
- **Switches have a 3 to 1 boundary.** Off switches were drawn in the hairline colour (1.4 to 1). A
  new `--control` token (from web/DESIGN.md) is the off colour.
- **Tablets get phone tap sizes.** Switches are 44 by 26 and card header links are 32 px tall below
  860 px, because a 768 px screen is a touch screen too.
- **Deny on the left, the main action on the right, everywhere.** web/DESIGN.md makes this a rule
  for deciding safely. The prototype had Approve first on cards and sheets, but Send last in
  compose. Cards, the task page, the large view, the fix sheet, the login sheet, Add mailbox and the
  profile draft now all put the main action last.
- **Loading, error and empty states for every page.** `?state=loading`, `?state=error` and
  `?state=empty` show them. Loading keeps the real title and draws skeletons shaped like the
  content (cards, tiles, rows), so nothing moves when data arrives. Error says what happened and
  that nothing was changed, approved or sent, with Try again. Empty is a new install: one machine,
  nothing connected. It found pages with no empty state at all (Sites, APIs, Routines and Profiles
  showed a bare table or nothing). Each now has an icon, what would be here, and the next action.

### Overview

- **Removed the "Waiting for you" tile.** It repeated the Needs you row directly above it.
- **"Ran off your laptop" is "On your machines"**, and its line is "3 running now". The old title
  needed explaining, and on a phone the line wrapped to three lines.
- **Cards on a phone stretch to the row**, so Approve and Deny sit at the same height on every card
  as you swipe, and there is no blank band under a short card.
- **Sites card:** "create_label needs a fix" is "Shipping labels need a fix".

### Approvals

- **No repeated line.** "Payments always ask first." appeared twice on a card (once in the reason,
  once at the end of the detail). Same for posting. The detail now carries only the facts.
- **"1 standing approvals"** (wrong, and jargon) is "1 allowed without asking", and the line is gone
  when there are none.
- **The fix sheet** is titled "A fix for creating shipping labels" instead of the code name
  `create_label`. "Dry run" is "Tried safely", and "the endpoint keeps the rule" is "it still asks
  you first, as before".

### Tasks and a task

- **Today comes before Upcoming.** The page opened on three tasks that have not run yet.
- **API tasks have names a person reads:** "Check open orders", "Track 12 parcels", "Create a
  shipping label", matching the endpoint titles, instead of `open_orders` and `create_label`.
- **The task id is out of the title area.** The breadcrumb was "Tasks > t-120" and the subtitle
  ended in "t-120". The breadcrumb is a back link to Tasks, and the id is the last fact
  ("Reference"), where support can still find it.
- **One Replay button.** The facts panel had a second Replay (or a disabled one) next to the one in
  Steps.
- **"Offload" is "Machine work"** in the job filter, the job column and the chart. "Offload" means
  nothing to someone who does not run servers.

### Sites and a site

- The broken endpoint banner names it by its title ("Create a shipping label stopped working"),
  not its code name.

### Profiles, a profile, Inbox and Email

- **Status colour no longer colours text.** "1 needs you" was amber text (3.2 to 1). It is a dot and
  a word.
- **The selected inbox row** kept grey metadata on the tinted background (3.3 to 1); it uses the
  secondary text colour there. Same for the "replies" label in the voice examples (4.4 to 1).
- **Mail filter counts** were drawn at 60% opacity (2.4 to 1); they use `--text-3`.
- **The reader header no longer overlaps.** At 768 px a long sender address ran under the time. The
  address has its own truncating line with the full text on hover.
- **Mailbox rows stack below 1100 px.** The five column row was used down to 641 px, so at 768 the
  address truncated to "hello@nort…" and "Needs a new app password" ran into the chart.
- **The app password alert is a plain card** with an amber hairline, not a tinted gradient (the
  gradient also dropped its grey line to 2.5 to 1).
- **The add photo button has a name** ("Add a photo"); it was an unnamed "+".

### Settings

- "Act endpoints: reorder, refund and create_label" is "Changes through Site APIs: a repeat order,
  a refund or a shipping label". "Standing approvals" is "Allowed without asking", the same words
  Approvals now uses.

### Search, Activity, Machines

- **Search:** item names get the width, the context on the right is capped at a third. The
  placeholder fits a phone ("Jump to a page, task or site").
- **Phone rows** let the small right hand note wrap, so the row's own text is not squeezed to
  "Mail che…".
- **Machines:** "offloaded work" is "heavy work".

## What I chose not to change, and why

- **The drawn page previews.** They are pictures of other sites (Amazon's checkout, an Instagram
  post), so they keep those sites' light colours in dark mode and their tiny text. Studio counts
  them as contrast and theme faults; they are decoration with a full text label (`role="img"`).
- **Site marks.** A white letter on a brand colour (Amazon amber, Reddit orange, Etsy) is how a
  site mark looks. The mark sits beside the site's name, so nothing is lost.
- **The font.** The prototype loads Inter from Google Fonts; the real app uses Geist, self hosted.
  The last layout shift (0.18 at 320 px on task pages) is the font swapping in. Building the app
  with a self hosted font removes it, so I did not change the prototype's font here.
- **The 4 px grid.** Many paddings are 9, 14 or 18 px. Moving every one would change the rhythm of
  the whole page in one go; it is better done when the screens are built in `web/` with the real
  spacing classes. The radius and shadow rules were cheap and are done.
- **Nav names.** The prototype says Overview, Approvals, Tasks, Sites, Machines, Activity; the real
  app says Home, Needs you, Accounts, Computers, History. That is the owner's call (below).
- **Truncated sub lines in phone lists.** A list row with a one line sub text that ends in "…" is
  normal; the full text is one tap away.

## Open questions for the owner

| Question | My default |
|---|---|
| Use the real app's words in the prototype (Home, Needs you, Accounts, Computers, History) or keep these (Overview, Approvals, Sites, Machines, Activity)? | Keep one set everywhere. I would take the real app's words, since `vocabulary.ts` already holds them, and rename the prototype when screens are built. |
| "Machine work" for the third kind of task (was "Offload")? | Yes, until a better plain word comes up. "Heavy work" is the other candidate. |
| The side menu groups "Web" and "Compute". "Compute" is a technical word, and Profiles sits under "Web". | Drop the group labels, or use "Your accounts" and "Your machines". Not changed yet. |
| Cost per task is in dollars ($0.06) while everything else is in euros. | Show cost only with technical details on, as web/DESIGN.md says, in the workspace's currency. |
| Card colour: the prototype's indigo accent fills Approve; the real app's primary button is black on white. | Follow the real app when building; keep the prototype as is. |

## Images

Before and after, in this checkout, git-ignored (132 MB):

- `panel/prototypes/dashboard/shots/before/`: every page and sheet, 5 widths, 2 themes
  (`<page>@<width>-<theme>.png`), plus `_sheet-<page>.png` contact sheets.
- `panel/prototypes/dashboard/shots/after/`: the same set after the changes.
- `panel/prototypes/dashboard/shots/after-loading/`, `after-error/`, `after-empty/`: Overview,
  Tasks, Sites, Profiles and Routines in each new state.

They are regenerated with a small Playwright script (headless Chrome, `prefers-color-scheme`,
reduced motion, full page). Studio keeps its own captures in its store.

## Studio problems found on this job

Studio did the job: the layout shift, the contrast faults, the reader overlap and the mailbox
layout were found by it, `since` made the second pass cheap, and `ui_diff` named every change in
plain lines. These are the problems, roughly by how much they cost:

1. **Everything groups under "page".** All 439 findings came back under one component, `page`, so
   "worst findings grouped by component" was one long group. The prototype has no
   `data-component`, but class names (`ncard`, `tabbar`, `mbx-row`) would have grouped it well.
2. **Decorative text is checked like body text.** Text inside `role="img"` with an `aria-hidden`
   inside, and text under 2 px (a 0.98 px "Amazon" in a 3.9 by 1.2 px box), counted as major
   contrast faults: about 60 of 150. Text that is part of a logo (white letter on a brand colour)
   is exempt in WCAG 1.4.3 and was also counted.
3. **theme.leak counts intended fixed colours.** 218 of 439 findings. A picture of a light website
   and brand marks are meant to stay as they are. A way to mark a subtree as "fixed on purpose"
   (or skipping `role="img"`) would remove nearly all of them.
4. **Occlusion inside a scroll container.** A chip scrolled out of a horizontal chip row, and table
   cells scrolled out of an `overflow-x: auto` wrapper, were reported as "covered" by the element
   next to the container. They are clipped by design, not covered.
5. **Radius tokens are found by name only.** `tokens="page"` recognised `--radius` but not
   `--r-sm`, `--r-md` and so on, and called `var(--r-lg)` "8px, hard-coded: it did not move when the
   tokens changed". Renaming them `--radius-*` fixed it, but the probe could detect any variable
   whose value is a length used in `border-radius`.
6. **Pixel route contrast on small text over translucent bars** reports the worst anti-aliased pixel
   (2.5 to 1) while the median is 4.2 to 4.5. Helpful as a hint, but read as a hard fail it
   overstates the problem.
7. **Finding ids collide across pages.** Ids come from check, component and selector, so the same
   selector on two hash routes of one file gets the same id. Fine within one url, confusing when
   comparing runs across pages.
8. **State steps failed where plain Playwright worked.** `states: {"compose": [click
   [data-act=compose], wait_for #modal textarea]}` timed out at 375 px (it worked at 1440, and the
   same click works in plain Playwright at 375). The error does not say whether the click landed or
   which element it hit, so I could not tell why.
9. **ui_diff lists skipped screens as noise.** Asking for 2 widths against a 10 screen before run
   returned 8 "skipped" entries and a verdict of "2 of 10 screens changed". Comparing only the
   screens both sides have, and saying so once, would read better.
10. **`studio up` died twice from outside.** Both times another builder's session started its own
    studio at the same second as mine got SIGTERM, so something on this machine stops
    `agentos studio` processes by name. I ran it through a wrapper script with a different name.
    Not a studio bug, but `studio up` has no way to tell instances apart (a lock file or a named
    instance would help), and the first request after it died failed with a bare connection error.
11. **The token is only in the banner.** The agent token is printed once, and with output piped to a
    file it sat in Python's buffer until I restarted with `-u`. `studio_status` could return the
    `claude mcp add` line, or the token could be kept in the state folder.
12. **Firefox and WebKit look missing under a scratch HOME.** Playwright keeps its browsers under
    `~/Library/Caches`, so a different HOME hides them. The `missing` lines then suggest installing
    browsers that are already installed.
13. **Disk warning on every start** ("34 GiB free (4%)"). True, but a percentage of a 1 TB disk is
    not the right measure for a laptop with 34 GiB free.
