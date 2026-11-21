# openajar composites

The parts only openajar has. They live in their feature, because each reads its own data, and the catalog
draws them from the mock (`/$org/design#composites`). A new screen about an environment or an account
reuses them instead of drawing its own.

| Use | Component | File |
|---|---|---|
| An environment's time, cost, doing and code beside its screen | `TimePanel`, `DoingPanel`, `CodePanel` (glass on the night band, cards below 1024 px) | `features/environments/env-panels.tsx` |
| The live screen on the night band, its replay, take over and hand back | `EnvStage`, `ControlFrame` (the control pill) | `features/environments/env-stage.tsx`, `features/lane/control.tsx` |
| What its terminal ran | `CommandsCard` (the terminal list: command, state, when) | `features/environments/env-cards.tsx` |
| Proof that it works: screenshots and clips | `ProofsCard` | `features/environments/proofs.tsx` |
| Where it can go back to | `CheckpointsCard` (a timeline) | `features/environments/checkpoints.tsx` |
| What it runs on and costs | `MachineLine` (one quiet line: the machine, its price an hour) | `features/environments/env-cards.tsx` |

Money waits for Technical details everywhere except where renting is the point (an environment's machine).
A composite says what the node did, in words from `lib/vocabulary.ts`, and shows the job, never a summary
of it.

## Not this

- A second live view, a second terminal list, or a cost written out by hand on another screen.
