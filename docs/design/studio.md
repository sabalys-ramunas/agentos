# Studio: tools that help agents build and design

Status: plan written 2026-11-12. Waves 1 and 2 are built and on main (the review browser,
capture, the checks, tokens, product profiles with a brand, the diff, the MCP tools, learning a
brand, the `agentos-studio` skill and its eval). Waves 3 and 4 are not built: the panel, audience
flows, the report screens, the bench and the tool eval. Neither are device simulators: studio has
phone widths and emulated devices, not real iOS or Android. Owner's words: "agents os, helping devs and
designers build", with "less hand holding".

Read with: [studio-ax.md](studio-ax.md) (the agent experience rules every tool follows),
[../research/2026-11-ui-check-tools.md](../research/2026-11-ui-check-tools.md) and
[../research/2026-11-design-bench.md](../research/2026-11-design-bench.md).

## 1. What it is

Agents write the code. agentos gives them a computer to run it on, tells them whether the
result is good, and remembers who the work is for.

Three kinds of work sit around one object, the profile:

```
                 learns from sites, runs and the owner's choices
                                   |
                                   v
                          +----------------+
                          |    PROFILE     |   the owner approves every change
                          | identity voice |
                          | brand rules    |
                          | references     |
                          | accounts keys  |
                          +-------+--------+
                                  | an agent works as it (profile_use)
          +-----------------------+-----------------------+
          v                       v                       v
        BUILD                   DESIGN                 PUBLISH
   tests on the node,      ui_check, ui_diff,      posts and replies
   the dev server in a     the panel, the bench    on account lanes
   browser, keys by name   (studio, this doc)      (exists)
   (mostly exists)
          +-----------------------+-----------------------+
                                  v
                    a record of every run, per profile
```

Browser automation stays the base: agents need real, logged-in browsers for many tasks, for
testing other sites, and to collect reference screens from other products.

## 2. The parts, and who uses them

Each part is built once and used by everything above it.

| Part | What it is | Used by |
|---|---|---|
| Review browser | A plain browser for measuring: no stealth, no proxy, pinned fonts and time, any width and theme, Chromium, Firefox or WebKit. Runs on the owner's own computer or on a node. Not a lane. | everything below |
| Capture | The same screens (states x widths x themes) rendered the same way every time, kept as images plus a layout manifest | checks, diff, panel, bench |
| Measured checks | Layout faults, contrast, tap targets, names, page health, read from the page | ui_check, panel gate, bench metrics, CI |
| Token and component matching | A computed value mapped back to the design system, and a UI element to a known component | system_check, diff wording, brand learning |
| Diff | What changed between two captures, with the cause named | ui_diff, panel rounds, pull requests |
| Blind pairs | Two unlabelled screens, a judge picks one | panel, bench taste column |
| Profile | Brand, rules, references, reviewers, audiences | defaults for every call |
| Record | Runs, findings, cost | reports, bench raw data |

## 3. The three loops

1. **Self-check** (seconds). An agent changes UI, calls `ui_check`, fixes what is measured,
   calls it again with `since` and reads only what changed. `ui_diff` says what the change did.
2. **Panel** (hours). Self-check must be clean first. Then reviewers judge blind pairs (this
   round against the last, and against frozen references), the agent fixes what two or more
   reviewers raised or a measurement confirms, and the loop repeats until a round no longer
   beats the one before beyond the margin of error. Every few rounds the owner picks a
   direction; the choice is saved to the profile.
3. **Bench** (a day). Fixed tasks, several models, with and without the profile's context.
   Same capture, same checks, same blind pairs.

Each loop is the one before plus one part.

## 4. Decisions

These were settled while planning. A builder who finds one wrong says so in the hand-back.

1. **A review browser is not a lane.** The lane manager is one engine per node, tied to Xvfb,
   x11vnc and a pinned window (`node/lanes/manager.py`, `node/display.py`). Studio has its own
   small browser pool on plain Playwright, headless, with no display. So it runs on macOS, and
   the stealth rules for lanes (no main-world evaluation, no init scripts,
   `docs/design/stealth.md`) do not apply to it. A studio call takes a URL, not a lane id.
   Checking a page inside a logged-in lane is a later step with a reduced, read-only set.
2. **Local first.** One command on a Mac or Linux laptop gives an MCP server whose studio tools
   use a real browser. No node, no proxy settings. A node is for long or heavy runs.
3. **Chromium is the default engine**, Firefox and WebKit on request. Chromium has layout shift
   and long task measurement; the others report those as skipped.
4. **Package and names.** Code in `src/agentos/studio/`, one toolset `studio`, one skill
   `agentos-studio`, state under `$HOME/studio/`. New install extra `studio` (playwright,
   Pillow). The measured tools are ordinary tools (no plugin to enable). The panel and the
   bench spend model money, so they are a plugin a person turns on.
5. **Judges are agents, tickets keep them blind.** agentos does not call vision models itself
   (brains take no images today, except one typed API). The agent running a panel hands each
   judge a one-use ticket. A ticket reveals two unlabelled images and a brief, and takes one
   answer. The judge never sees which is which, the round, the goal or other answers. agentos
   builds the pairs, tallies and keeps the cost record. A node-side judge is a later option.
6. **Profile kinds.** `person` and `business` exist. New: `product` (a thing being built, with
   a brand), `reviewer` (a judging job an agent does; named reviewer because "role" already
   means member roles), `audience` (a kind of user: screen, language, network, goals).
7. **Brand lives in the profile**, versioned and owner-approved like the rest of it. Tokens are
   stored in the Design Tokens format (DTCG 2025.10) so they import and export. Secrets stay
   in the vault; a profile names them.
8. **Agents propose, the owner approves.** This already holds for profiles (an agent saves a
   draft, only the owner approves) and stays the rule for brand, guidelines and reviewers.
9. **Findings are measured, heuristic or vision, and never mixed.**
10. **Existing dev features are reused, not rebuilt**: `agentos run` (tests on the node),
    `agentos expose` (the dev server in a lane), `browser_console` and `browser_network`.

## 5. Module layout and ownership

```
src/agentos/studio/
  contracts.py        shared types (written by the lead; change only through the lead)
  browser/            S1  pool, engines, pinned environment, targets, local run, studio_status
  store/  capture/    S2  artifact store, screen sets, capture, layout manifest
  checks/             S3  measured checks, grouping, stable ids, delta
  tokens/             S4  token sets, matching, provenance probe, components, adherence
  diff/               S6  structural and pixel diff
  tools.py            S7  the MCP surface, result shaping, toolset, skill, howto, AX eval
  panel/              S9  pairs, tickets, tally, drift check, stop rule, report
  bench/              S12 tasks, runner, results
src/agentos/profiles/ S5  kinds, brand, guidelines, references, profile_use
                      S8  learn a brand from a live site
```

### Waves

| Wave | Builders | Needs |
|---|---|---|
| 1 | S0 front door (README, vision), S1 browser, S2 capture, S3 checks, S4 tokens, S5 profiles, H1 chores | contracts.py |
| 2 | S6 diff, S7 tools and skill, S8 learn a brand, H2 small-model usability runs | wave 1 merged |
| 3 | S9 panel, S10 audience flows, S11 report screens in web/ | wave 2 |
| 4 | S12 bench and results site, S13 tool eval for all of agentos | wave 3 |

In wave 1 the four studio builders work against `contracts.py` and do not import each other.
S2, S3 and S4 open pages through a tiny local helper in their own tests (plain Playwright,
system Chrome, as `tests/unit/test_siteapi_chrome.py` does) until S1 lands. S7 joins them.

## 6. Contracts between builders

The types are in `src/agentos/studio/contracts.py`. In words:

- **S1 gives** `ReviewBrowser.page(target, screen)`: an async context manager that yields a
  Playwright `Page` already at the target, in the screen's width and theme, in the named
  state, settled (fonts ready, network quiet, motion off, time pinned). It raises a studio
  error when the page did not load, came back blank or the theme did not apply (AX rule 6).
- **S2 gives** `capture(browser, target, screens) -> Capture`: one `Shot` per screen (image
  blob, manifest blob, ok or the reason), kept in the store by content hash. And the manifest
  builder: `build_manifest(page) -> list[Element]`.
- **S3 gives** `run_checks(page, ctx) -> CheckOutput` for one screen and
  `merge(outputs) -> CheckRun` across screens (grouping by component and check, stable ids,
  `seen_at`), plus `delta(before, after)`.
- **S4 gives** `load_tokens(source) -> TokenSet`, `match(value, category, tokens) -> Match`,
  `system_check(page, tokens, components, ctx) -> CheckOutput` (same finding shape as S3) and
  `adherence(page, tokens, components) -> Adherence`.
- **S5 gives** `ProfileService.context(profile, task) -> dict` behind the tool `profile_use`,
  and `Brand` on a product profile, from which S7 builds a `TokenSet` and component rules.

## 7. Rules for every builder

1. Read `CONTRIBUTING.md` and `docs/design/studio-ax.md` first. The agent experience rules are
   requirements.
2. Stay inside the files your brief owns. If you need a change elsewhere (a shared type, a
   registry list), make the smallest one and name it in the hand-back.
3. Do not change `contracts.py` on your own. Propose the change in the hand-back, with the diff.
4. No em dash (U+2014) in anything a person or an agent reads: tool descriptions, errors, docs,
   UI. `make emdash` checks it.
5. Every new error class needs an entry in `src/agentos/core/error_catalog.toml` and a status
   in `src/agentos/core/http_errors.py`. Every new setting needs a description in
   `core/config_report.py`. Every new tracked doc or top-level directory needs a line in
   `scripts/public-export.toml`.
6. Keep `import playwright` and `import PIL` inside functions (a test checks playwright).
7. Tests: write unit tests for what you add and run only those (`uv run pytest -q
   tests/unit/test_<yours>.py`). Do not run the full suite; CI does. A test that needs a real
   browser follows `tests/unit/test_siteapi_chrome.py`: system Chrome, headless, pages served
   through `page.route`, skipped when Chrome is missing. No network.
8. Ground truth: every measured check ships fixture pages with the fault planted and pages
   without it, and a test for both. A check with no clean-page test does not merge.
9. Commit on your branch with subjects in the house style (`Studio: what changed, in plain
   words`). Do not push, do not open a pull request, do not merge.
10. Hand back: the branch and head commit, what works, what does not, what you could not
    verify, the tests you ran with their result, and any contract change you want. State
    plainly anything you skipped.

## 8. What is deliberately later

- Checks inside logged-in lanes (reduced set, isolated world).
- Hot reload through `agentos expose`.
- A node-side vision judge.
- Storybook sweep, a pull request check, a second design system for the bench.

## 9. Open questions, with the default taken

| Question | Default |
|---|---|
| Fonts for pinned rendering | A small bundled set plus the project's own web fonts; system fonts reported as "not pinned" in coverage |
| Where local mode keeps state | `~/.agentos/studio/` on a laptop, `$HOME/studio/` on a node |
| Image library | Pillow, in the `studio` extra only |
| Source hints (file and line) | `data-source` and `data-component` attributes when present; a small build plugin later |
