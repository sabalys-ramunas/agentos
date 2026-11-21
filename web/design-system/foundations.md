# Foundations

Every value on a screen comes from a token: colour, type, space, radius, depth, motion and layers. The
tokens live in one script (`tokens/build-tokens.py`, with `tokens/theme.py` for the Tailwind names),
which writes `styles/tokens.css`, `styles/theme.css`, `tokens/tokens.json` and `tokens/theme-keys.json`.
Never edit those four. In the catalog: `/$org/design#foundations`.

| Use | What | File |
|---|---|---|
| A colour | A class on a role: `bg-card`, `text-muted`, `border-line`, `text-bad`, `bg-ok-dot` | `styles/tokens.css`, `styles/theme.css` |
| Text | A type role: `text-body`, `text-secondary`, `text-caption`, `text-card-title`, `text-heading` (20), `text-headline` (32), `text-page-title`, `text-code` | `styles/theme.css` |
| Space | The 4 px grid: `p-4`, `gap-3`. No half steps, no `p-[7px]` | `styles/tokens.css` |
| Radius | `rounded-sm` 4 marks, `-md` 6 controls, `-lg` 8 pictures, `-xl` 10 cards, `-full` pills | |
| Depth | Hairlines, plus `shadow-lift` on cards; `shadow-popover`, `shadow-dialog` for floating layers | |
| Motion | `duration-(--duration-fast)` 100, `-base` 150, `-slow` 200, `-settle` 280, on `ease-standard` | |
| Layers | `z-10` sticky, `z-20` header, `z-30` dropdown, `z-40` overlay, `z-50` dialog, `z-60` toast, `z-70` tooltip | |
| Show a token as data | `Swatch`, `TokenChip` | `components/ui/swatch.tsx` |
| The kind of work | `KindTile`, `KindEyebrow`, `Card kind`, `Chip kind`: tests plum, design pink, build teal, server blue | `components/ui/kind.tsx` |
| Our own icons | `LaneIcon` for a browser, terminal, service, phone; lucide at 16 px for the rest | `components/lane-icon.tsx` |
| Who and what | `BusinessMark`, `PersonMark`, `AgentMark` | `components/brand/identity.tsx` |
| Another company's site | `SiteMark` (official artwork only, beside its name) | `components/brand/site-mark.tsx`, `components/brand/vendored.ts` |
| The app's own things (a place, a setting), never an action | `AppTile` | `components/ui/tile.tsx` |

Colour means something, and only that: green fine, amber needs a look, red failed, ink with a pulsing
ring is an agent acting now (never pink: pink is the design kind of work), a kind's hue is its kind.
Focus is the one blue. Everything else is ink on the page with hairlines.

## Not this

- A hex, `rgb()`, `color-mix()` or a named colour in a screen. Add a token to `build-tokens.py` instead.
- `text-sm`, `rounded-2xl`, `shadow-md`, `bg-red-500`: Tailwind's scales are cleared, so they draw nothing.
- `bg-bg`, `text-13` and the other pre-Mono names: they are no longer classes, and the check refuses them.
- `bg-bg`, `text-red`, `text-13`: the pre-Mono names still work, but new code writes `bg-card`, `text-bad`, `text-secondary`.
- A kind's hue for a status, a selection or decoration; a workspace colour as a surface or text.
- `WorkspaceMark`: it is `BusinessMark` under its old name.
