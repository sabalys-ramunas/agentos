# Actions and forms

What a person presses and fills in. In the catalog: `/$org/design#actions`.

| Use | Component | File |
|---|---|---|
| The point of a view (once per view) | `Button variant="primary"` | `components/ui/button.tsx` |
| Everything else | `Button` (secondary is the default), `ghost` in rows, `danger` (a red word on a hairline), `inverse` on ink | `components/ui/button.tsx` |
| An icon alone | `IconButton label` (the label is required) | `components/ui/button.tsx` |
| A field | `Field label hint error` around `Input` or `Textarea` | `components/ui/field.tsx` |
| One of many | `Select` | `components/ui/select.tsx` |
| On or off, yes or no | `Switch`, `Checkbox` | `components/ui/toggle.tsx` |
| Parts of one thing | `Tabs`, `TabList`, `Tab`, `TabPanel` | `components/ui/tabs.tsx` |
| A view of the same thing | `SegmentedControl` | `components/ui/tabs.tsx` |
| Actions on one thing, a popover, a tip | `Menu`, `Popover`, `Tooltip` | `components/ui/menu.tsx` |
| A command or id to copy | `CodeBlock code`, `CopyButton text`, `useCopy()` | `components/ui/misc.tsx` |
| A choice of a few options, each with a sentence; a settings form's note and save | `ChoiceGroup`, `Choice`, `CheckCard`, `RadioRow`; `FormNote`, `FormActions`, `SettingRow`, `SaveStatus`, `UnsavedChanges` | `components/ui/choice.tsx`, `components/ui/form.tsx`, `components/unsaved-changes.tsx` |

A busy button is `loading` (it stays focusable). `shortcut="A"` shows the key and sets
`aria-keyshortcuts`. Deny sits left of Approve, everywhere. The hosted app never has a password field.

## Not this

- Two primary buttons, a red filled button, or disabling by hand instead of `loading`.
- `navigator.clipboard.writeText` in a screen: that is `CopyButton` or `useCopy` (the check finds it).
- A switch that would change nothing: say why instead.
