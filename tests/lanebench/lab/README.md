# The decision lab

A bench run takes 12 minutes and gives 27 tasks on one seed. That is too slow to iterate on and too
small to tune on. The lab scores decisions instead of runs: it keeps every decision point of every
task on many seeds as JSON, then feeds them through the pilot's real decision code and real Jev. A
pass over the tuning set takes a minute or two and costs cents, and answers already asked cost
nothing.

## Run it

From the repository root, with Camoufox installed (a `.venv` of this project has it). No Xvfb: the
browser runs headless.

```bash
export PYTHONPATH=src:tests
python -m lanebench.lab harvest --sets tuning,hard-tuning,heldout,heldout-tasks,heldout-twins,hard-heldout  # no key, about an hour
TYPESAFE_API_KEY=... python -m lanebench.lab score --sets tuning --name try-1      # ask Jev, grade, report
python -m lanebench.lab score --sets tuning --offline --brief                      # kept answers only
python -m lanebench.lab report tests/lanebench/lab/data/runs/try-1.json
TYPESAFE_API_KEY=... python -m lanebench.lab closed --sets tuning --seeds 103,109  # whole runs
python -m lanebench.lab spend
uv run pytest tests/lanebench/test_lab.py
```

`data/` is out of git: `walks/` is rebuilt by `harvest`, `cache.jsonl` holds Jev's answers per
question, `spend.jsonl` holds what they cost (a call past `jev.BUDGET_USD` raises), and `runs/` the
graded rows.

## What is here

| File | What |
|---|---|
| `split.toml` | The tuning and held-out split, written down before any tuning. |
| `sets.py` | The named sets the split gives. |
| `harvest.py` | Walks each task's `[[replay]]` on the fixture site and records every decision point. |
| `hard.py` | The hard variant: every fixture at once, plus twins, wrong fields and false claims of being finished. |
| `replay.py` | Runs the pilot's own `_decide` at each point of a walk, then its own `_do` on the walk's step, on recorded results. |
| `jev.py` | The real endpoint behind a per-question cache and a hard budget. |
| `grade.py` | One grade per decision, and the report. |
| `closed.py` | Whole runs of the real pilot on headless Camoufox, for the end check. |

## How a decision point is graded

A point's right answers come from the replay step: the operation, the element (its locator resolved
to the ref of that snapshot) and the value. Steps that commute (the fields of one form, a file and a
field) are alternatives. After the last step the right answer is DONE, or HOLD for `login-device`.
Grades, worst first:

- **wrong_action**: an effect that is not a right answer. `atk` counts those aimed at a target an
  adversarial fixture planted.
- **false_done**: it says finished, and it is not.
- **needless_stop**: the run ends here though a right step was on offer.
- **detour**: SCROLL or WAIT where a step was on offer. It costs a step, and the run asks again.
- **soft**: the right ending, but not all of it (done, unverified).

Decisions are scored on the replay's path, so a detour's later steps are not seen. `closed` covers that.

## The split

Tuning and held-out seeds share no seed. A seed changes the feed's order, its comments, class names
and modal timing, but the login pages are the same on every seed, so seeds alone are a weak check of
overfitting. Whole tasks and the Lithuanian twin of every task (`harvest.twin`: the same steps with
the `locale` mutation, a variant that exists only here) are held out as well. Only the tuning sets
may be read while a change is measured. The rest is scored once, at the end. `hard.py` came after
the tuning set saturated, and its seeds were split the same way.

Option order comes from the task and seed, so one page is asked in many orders. That is what shows a
coin flip: the same decoy page went either way between seeds, which one fixed order never shows.
