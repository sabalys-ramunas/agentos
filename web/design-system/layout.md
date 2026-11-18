# Layout and shell

The frame a page sits in, the head of a page, and the cards and sections it is made of. The shell (rail,
top bar, command bar, status line) belongs to `components/shell/`: a feature never edits it, and adds a
place through `nav.ts` and its rows through `features/<it>/commands.ts`. In the catalog: `/$org/design#layout`.

| Use | Component | File |
|---|---|---|
| The head of every page: title, lede, actions on the right | `PageHeader` | `components/ui/misc.tsx` |
| A block of related facts with a head line | `Card` with `CardHeader` (title, quiet meta, one action) and `CardFooter` | `components/ui/card.tsx` |
| A whole card that is one link | `cardLink` on the `<Link>` | `components/ui/card.tsx` |
| A titled part of a page | `Section` | `components/ui/card.tsx` |
| A settings form | `FormCard` (content, then a well footer with a note and the action) | `components/ui/card.tsx` |
| A job card | `Card kind="test"`: the kind's 2 px top edge | `components/ui/card.tsx` |
| The Technical details switch | `TechnicalToggle` | `components/shell/technical-toggle.tsx` |
| The page frame | `AppShell`, `Rail`, `TopBar` | `components/shell/app-shell.tsx`, `components/shell/rail.tsx`, `components/shell/top-bar.tsx` |
| The status line and the freeze chip | `StatusPill`, `FrozenChip` | `components/shell/status-pill.tsx`, `components/shell/freeze-control.tsx` |
| The command bar | `CommandPalette`, its rows and state | `components/shell/command-palette.tsx`, `components/shell/commands.ts`, `components/shell/palette-store.ts` |
| The menu, counts, crumb and keys | five groups with a g key each; counts from the node; `usePageCrumb` | `components/shell/nav.ts`, `components/shell/counts.ts`, `components/shell/crumb.ts`, `components/shell/shortcuts.tsx` |

A page reads top to bottom as the job: what is happening now first, then what needs a person, then what
happened. A grid track is `minmax(0, 1fr)` by default, so long lines truncate instead of widening a phone.

## Not this

- `rounded-xl border bg-card` by hand: that is `Card` (the check finds it).
- An `<h1>` with `text-page-title` by hand: that is `PageHeader`.
- A tinted panel, a grey header band on a card, or a shadow other than the lift.
- A clock in the shell, or a second primary button "for balance".
