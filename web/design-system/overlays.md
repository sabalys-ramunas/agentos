# Overlays

Layers over the page: a decision, the inspect drawer, a picked row beside its list, a tip. Floating layers
take `shadow-popover` or `shadow-dialog`; nothing else does. In the catalog: `/$org/design#overlays`.

| Use | Component | File |
|---|---|---|
| A decision or a short form | `Dialog` (centred, a bottom sheet on a phone) with the confirm in `footer` | `components/ui/dialog.tsx` |
| Are you sure, and what happens | `ConfirmDialog` (`danger`, `typeToConfirm` when it cannot be undone) | `components/ui/dialog.tsx` |
| The plumbing of one thing | `Drawer` (the inspect surface) | `components/ui/dialog.tsx` |
| A picked row beside its list | `SidePanel` (a Drawer under 1024 px) | `components/ui/dialog.tsx` |
| Proving it is you | `StepUpDialog`: opens by itself on `step_up_required`, never from a screen | `components/shell/step-up-dialog.tsx` |
| A few lines about one thing, a tip | `Popover`, `Tooltip` | `components/ui/menu.tsx` |

The close button is last in the DOM so the first field takes focus, and focus returns to what opened it
(`useReturnFocus`). Only one dialog is visible at a time.

## Not this

- A dialog for something a page could say, or a dialog that opens a dialog.
- A hand-made overlay with `fixed inset-0` and a z-index of its own.
