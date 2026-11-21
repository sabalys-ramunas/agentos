# Building a screen with the design system

The short guide. [DESIGN.md](DESIGN.md) is the deep reference (every rule and why); this page is how to
use it. Most screens here are written by coding agents, so the system is built to make the right way the
easy way, and the wrong way a failing test that names the fix.

The bar: Vercel-level refinement, information-rich screens that show the job itself (the command, the
branch, the live browser, the cost), never a generic admin template. Colour means something and only
that. Hairlines, not tinted panels. Everyday words from `src/lib/vocabulary.ts`. No em dash.

## A screen in five steps

1. **Look first.** Open the catalog, `/$org/design` (`pnpm dev`, then `/northwind-group/design`), and find
   the parts your screen needs. Read the group index for each (below).
2. **Compose, do not draw.** `PageHeader` on top; `Card` with `CardHeader` for each block; `Table` for rows;
   `StatTile` for numbers; `Status` for state; `QueryView` for loading and errors; `EmptyState` for nothing
   yet; `InlineError` for a refusal. Reuse the openajar composites (live view, terminal list, proofs) as they are.
3. **Style only with tokens.** Classes on roles (`bg-card`, `text-muted`, `border-line`, `text-bad`), type
   roles (`text-body`, `text-secondary`, `text-caption`), the 4 px grid. No hex, no `p-[7px]`, no `style={{ color }}`.
4. **Words from the vocabulary.** Machine, not node; account, not lane; routine, not flow. Technical terms
   wait behind Technical details (`useTechnical`). One reviewed exception: the environment screens say
   node for the host an environment runs on, since there is no everyday word for it.
5. **Check it.** `pnpm design:check`, then look at it in both themes at 390 and 1440 (or `ui_check` with studio).

## Tokens in one table

| Need | Write | Not |
|---|---|---|
| Page, card, well, menu | `bg-page`, `bg-card`, `bg-inset`, `bg-raised` | `bg-white`, `bg-bg`, `bg-[#fafafa]` |
| Hairline, firm edge, control edge | `border-line`, `border-line-strong`, `border-control` | `border-gray-200` |
| Text: read, second, quiet, off | `text-fg`, `text-muted`, `text-faint`, `text-disabled` | `text-black`, `text-zinc-500` |
| Status word, mark, tint, edge | `text-ok` / `bg-ok-dot` / `bg-ok-fill` / `border-ok-line` (warn, bad, idle, live) | `text-red`, `text-green-600` |
| An agent acting now | `Status` tone `live` with `pulse`: ink, a ringed dot that pulses. Never pink: pink is the design kind of work | pink, or any hue |
| A kind of work | `KindTile`, `KindEyebrow`, `Card kind` | a kind's hue on anything else |
| Type | `text-page-title`, `text-headline`, `text-heading`, `text-section-title`, `text-card-title`, `text-body`, `text-secondary`, `text-caption`, `text-code`, `text-number` | `text-sm`, `text-[13px]`, `text-13` |
| Space, radius, depth | `p-4`, `gap-3`; `rounded-md` controls, `rounded-xl` cards; `shadow-lift` | `p-[7px]`, `rounded-2xl`, `shadow-md` |
| Motion, layers | `duration-(--duration-fast)`, `ease-standard`; `z-10` to `z-70` | `duration-500`, `z-[35]` |

The values live in `tokens/build-tokens.py` (and `tokens/theme.py` for class names). Need a new one? Add it
there, run `python3 tokens/build-tokens.py`, and use it. Never edit `tokens.css`, `theme.css` or the JSON.

## The component groups

| Group | What it is for | Index |
|---|---|---|
| Foundations | Tokens, type roles, kinds of work, icons and marks | [foundations](design-system/foundations.md) |
| Layout and shell | The page frame, `PageHeader`, `Card`, `Section`, `FormCard` | [layout](design-system/layout.md) |
| Data display | `Table`, `StatTile`, badges and pills, `Detail`, `Terminal` | [data](design-system/data.md) |
| Status and feedback | `Status`, `StateBadge`, loading, empty, error, `InlineError` | [feedback](design-system/feedback.md) |
| Actions and forms | `Button`, `Field`, `Select`, `Switch`, `Tabs`, `Menu`, `CodeBlock` | [actions](design-system/actions.md) |
| Overlays | `Dialog`, `ConfirmDialog`, `Drawer`, `SidePanel`, `Tooltip` | [overlays](design-system/overlays.md) |
| The instrument layer | `TimeChart`, `StackedBars`, `Sparkline`, `ImageFrame`, the board | [instrument](design-system/instrument.md) |
| openajar composites | The live view, the terminal list, proofs, checkpoints, the cost line | [composites](design-system/composites.md) |

A new component goes in `src/components/`, with an entry in `src/catalog/registry.tsx` and a line in its
group's index; the test fails without both. Keep its API to what real screens need.

## The checks

| Command | What it holds |
|---|---|
| `pnpm design:check` | The design rules (`src/test/design-system.test.ts`), the source rules and the tokens, in seconds |
| `pnpm test` | All of the above, with every other unit test |
| `pnpm lint` | File size (500 lines), complexity (25), hooks, a11y, no mock in a screen |
| `pnpm test:e2e catalog` | The catalog photographed by group, light and dark, 1440 and 390, against this machine's baselines |
| `node scripts/visual-compare.mjs` | The catalog and the main screens before and after a change, compared pixel by pixel |
| `python3 tokens/build-tokens.py --check` | The generated files match the script; contrast (AA) and colour-blind checks pass |

The design rules, each failing with the file, line and fix: **raw-colour** (hex, `rgb()`, `color-mix()`,
named colours, Tailwind palette classes), **arbitrary-value** (`p-[7px]`, `text-[13px]`, `bg-[...]` in a
screen; layout sizes such as `grid-cols-[...]` are fine), **not-a-token** (`text-sm`, `rounded-2xl`,
`z-15`, `duration-500`: classes that draw nothing here), **unknown-token** (`var(--text-faint)` and other
names that are not tokens), **inline-style** (a literal design value in `style`), **hand-rolled** (a card,
status dot, spinner, inline error, table, stat or page title drawn by hand; clipboard by hand),
**vocabulary**, and **legacy-token** (pre-Mono names such as `bg-bg`, `text-13`, and old components).

What was in the code when the rules came in is counted per file in `src/test/design-allowlist.json`. A
count may only go down: fix one and run `pnpm design:shrink`. Never raise a count; fix the finding.
`legacy-token` is at zero since 2026-11-19 and has no counts, so an old name anywhere fails; a branch
from before then runs `node scripts/codemod-legacy-tokens.mjs` once. The `exceptions` are reviewed
places a rule does not apply, each with its reason; `vocabulary:node` excepts that one word in a file.

The catalog's screenshot baselines are not in git. The first `pnpm test:e2e catalog` on a machine records
them (take it on a clean `main`), later runs compare, and `--update-snapshots` records a change you meant.

## Before a pull request

- [ ] Built from components; nothing drawn by hand that the catalog has.
- [ ] Only token classes and type roles; no new value outside `build-tokens.py`.
- [ ] Every state designed: loading (a skeleton shaped like the content), empty, error, refused, live.
- [ ] Looked at in light and dark, at 390 and 1440; no sideways scroll at 390.
- [ ] Words from the vocabulary, sentence case, no em dash.
- [ ] `pnpm typecheck && pnpm lint && pnpm knip && pnpm test` pass; a changed component has its catalog entry, and `pnpm test:e2e catalog` shows only the change you meant.
