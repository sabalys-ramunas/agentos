# Status and feedback

What state a thing is in, and what a person sees while it loads, is empty, fails or is refused. Every
screen designs all of these. In the catalog: `/$org/design#feedback`.

| Use | Component | File |
|---|---|---|
| The state of a run, a machine, a check | `Status status={...}`: a shape and a word. Words and tones come from `lib/labels.ts` | `components/ui/status.tsx` |
| A mark beside words of your own | `StatusDot tone pulse` | `components/ui/status.tsx` |
| State for a person who is not technical | `StateBadge`, `ConditionBadge` (All good, Waiting for you, Stuck, Sleeping, Working now) | `components/ui/state-badge.tsx` |
| Working on it | `Spinner`, or `loading` on a `Button` | `components/ui/spinner.tsx` |
| How full | `Meter` (ink, amber from 80%, red from 95%) | `components/ui/misc.tsx` |
| Loading and failing, for a whole view | `QueryView` (skeleton, then the data, or `ErrorState` with Try again) | `components/ui/states.tsx` |
| Nothing here yet | `EmptyState`: an icon, one bold line, one sentence, one action | `components/ui/states.tsx` |
| A refusal or failure in place | `InlineError` (or `error` on a `Field`) | `components/ui/states.tsx` |
| A part this place does not serve, no access | `NotLive`, `NoAccess` | `components/ui/states.tsx` |
| A page that does not exist | `NotFound` | `components/not-found.tsx` |
| The plan is full | `PlanNotice`, `OverLimitBanner` | `components/plan-notice.tsx` |
| Add to Home Screen | `InstallPrompt` | `components/install-prompt.tsx` |

An error says what happened, that nothing changed, and what to do. Ink with a moving ring means an
agent acting now, and nothing else: never decoration, never a resting state.

## Not this

- A status by colour alone, or a row filled with status colour.
- A dot drawn with `rounded-full bg-ok-dot`, a `Loader2 animate-spin`, a red `<p role="alert">`: those are `StatusDot`, `Spinner`, `InlineError` (the check finds each).
- "No data", "Error 410", a page spinner, or a bare "Done".
