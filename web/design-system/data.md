# Data display

The job itself, with the most information per glance: tables, numbers, pills, details and output. In the
catalog: `/$org/design#data`.

| Use | Component | File |
|---|---|---|
| Rows of things of one kind | `Table` in `TableScroll`, `Th` (sort, unit, align), `Tr` (onActivate, selected), `Td` (main, numeric, label, phone), `CellTitle`, `NoValue`, `RowActions` | `components/ui/table.tsx` |
| A table loading, or empty | `TableSkeleton` (the real head), `TableEmpty` holding an `EmptyState` | `components/ui/table.tsx` |
| Rows picked for one action | `SelectionBar` (ink, with `variant="inverse"` buttons) | `components/ui/selection-bar.tsx` |
| A few big numbers | `StatTile` in a `StatStrip` | `components/ui/stat-tile.tsx` |
| A count, a tag, a filter | `Count`, `Badge`, `Chip` (`aria-pressed`) | `components/ui/badge.tsx` |
| A branch | `BranchPill` (`main` in ink) | `components/ui/branch.tsx` |
| What a yes does, in one line | `IfYes` | `components/ui/if-yes.tsx` |
| Label and value rows | `Detail` inside a `<dl>`; `Kbd` for a key; `Avatar` for initials | `components/ui/misc.tsx` |
| What a process printed | `Terminal` (dark in both themes, ANSI as text, follow mode) | `components/ui/terminal.tsx` |
| A row of cards that slides | `Slider` | `components/ui/slider.tsx` |

Numbers are mono and tabular, right aligned, units in the head. A value the node does not send is an en
dash (`NoValue`), never a guess. Under 768 px a `Table stack` turns into a list.

## Not this

- A raw `<table>`: no sticky head, row heights, sort or phone list (the check finds it).
- `text-stat` by hand: that is `StatTile`.
- A coloured number unless it asks for attention, zebra rows, or a grey table head.
- Ids, hashes and commands in the sans face, or prose in mono.
