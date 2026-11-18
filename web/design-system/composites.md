# agentos composites

The parts only agentos has. They live in their feature, because each reads its own data, and the catalog
draws them from the mock (`/$org/design#composites`). A new screen about an environment or an account
reuses them instead of drawing its own.

| Use | Component | File |
|---|---|---|
| An environment's numbers in one strip | `Facts` | `features/environments/env-header.tsx` |
| The live browser, with take over and hand back | `BrowserCard` (live view frame), `ControlFrame` | `features/environments/env-cards.tsx`, `features/lane/control.tsx` |
| What its terminal ran | `CommandsCard` (the terminal list: command, state, when) | `features/environments/env-cards.tsx` |
| Proof that it works: screenshots and clips | `ProofsCard` | `features/environments/proofs.tsx` |
| Where it can go back to | `CheckpointsCard` (a timeline) | `features/environments/checkpoints.tsx` |
| What it costs | `MachineCard` (the cost line: price an hour, cost while unused) | `features/environments/env-cards.tsx` |

Money waits for Technical details everywhere except where renting is the point (an environment's machine).
A composite says what the node did, in words from `lib/vocabulary.ts`, and shows the job, never a summary
of it.

## Not this

- A second live view, a second terminal list, or a cost written out by hand on another screen.
