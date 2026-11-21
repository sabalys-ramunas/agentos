# Agent experience rules for studio

openajar is used by agents first. A person reads the report at the end; an agent makes every call
before that. These rules apply to every tool, result and error added in this work. A builder who
cannot meet one says so in the hand-back; it is never dropped quietly.

The test for each rule: a small model (Haiku) that has only the tool descriptions and the skill
must finish the loop without help. If it cannot, the tool is wrong, not the model.

## 1. One call answers one question

An agent asks "is my change all right?", not "what is the scrollWidth of the card?". The entry
tools match the questions:

| The agent's question | One call |
|---|---|
| Who am I working for, and what are the rules? | `profile_use` |
| Is this page all right? | `ui_check` |
| What did my change do? | `ui_diff` |
| Is it on the design system? | part of `ui_check`, also `system_check` alone |
| Make this design better | `panel_start` |
| How do models compare on this? | `bench_start` |

Lower-level tools (capture, single checks) exist for composing, but no everyday loop needs them.

## 2. Nothing required that openajar already knows

A call needs the target and nothing else. Widths, themes, tokens, components, guidelines and
references come from the profile and the project's settings. Every default that was applied is
named in the result (`used: {widths: [360, 768, 1280], themes: [light, dark], profile: acme}`),
so the agent can see what it got and change it.

## 3. Small by default, more on request

- A default result fits in about 1,500 tokens: a verdict line, counts by check, the worst
  findings grouped by component, and handles for the rest.
- Images are never inline by default. A result names them (`cap_...`, `crop_...`); the agent
  asks for one when it needs to look. A crop of the fault is offered before a full screenshot.
- `detail="full"` and `finding_get(id)` give everything. Paging is by handle, never by repeat.

## 4. A finding is something an agent can fix

Every finding has: what check, where (ref and selector, component, source file and line when
known), what was measured, what was expected, a suggested fix, where it was seen (widths,
themes, states), and how it is known:

- `measured`: read from the page; the same every run.
- `heuristic`: a rule that can be wrong; says why.
- `vision`: a model looked at pixels; never mixed into the measured counts.

An agent can ask for `measured` only. Findings are grouped by component and check, one per
cause, never one per DOM node.

## 5. Same ids, and deltas

A finding id is stable across runs (derived from check, component and place, not from order).
A second call with `since=<run id>` returns only what changed: `fixed`, `new`, `still`,
`worse`. This is the hill an agent climbs, so it must be cheap to read.

## 6. Never a clean pass that was not checked

A result has a `coverage` part: what ran, what did not and why ("dark theme did not apply",
"layout shift cannot be measured on this engine", "2 of 14 screens failed to load", "fonts
fell back"). A check that could not run is `skipped` with a reason, never absent and never
`ok`. A blank page, a dev server that did not answer and an error page are failures of the
call, with a fix line, not pages with zero findings.

## 7. The same input gives the same output

Capture is pinned (engine, fonts, viewport, time, motion off). Where a result can vary (a
model judging), it is run more than once and the spread is reported. A number without its
spread is not shown as a number.

## 8. Each result says what to do next

Like the rest of openajar: errors start with a code and carry a `fix:` line. Results carry
`next:` in one sentence ("3 measured findings: fix them, then call ui_check with
since=run_41"). When a person must act, the result says who and where, and gives the wait
tool.

## 9. Long work is a job, and waiting is a tool

`panel_start` and `bench_start` return a job id at once. `job_wait` blocks until there is
something to read (a round ended, a checkpoint needs a person, the budget ran out, it
finished). An agent never polls. A job has a budget (rounds, money, time) with a default
ceiling; it stops at the ceiling and says so. A job can be resumed and its record read later.

## 10. Context is served in slices

`profile_use(profile, task)` returns what that task needs and pointers to the rest: design
gets tokens, components, checkable rules and references; writing gets voice and accounts;
dev gets environments and the names of keys. It never returns secrets, and never returns
private fields a task has no use for. The slice states its size and what was left out.

## 11. Few tools, short descriptions, found when needed

The tools live in one `studio` toolset. The description of each is short and says when to
call it and what comes back. A skill (`openajar-studio`) teaches the three loops in under a
page each. `howto` has the recipes. Description changes are tested with the tool eval
(rule 15) before they merge.

## 12. Page text and model text are data

Findings quote page text; a quote is capped and marked as data. A judge in a panel gets
images and a brief, never page text, code, model names, round numbers or the goal. What a
judge writes back is data for the tally; it cannot call tools or change a profile.

## 13. An agent proposes, the owner decides

Anything that changes how later runs behave (a profile's brand, a guideline, a role) is a
proposal with a diff and a reason. It waits in "Needs you". An agent cannot approve its own
proposal or raise its own limits. Review lanes cannot reach account lanes.

## 14. A person's turn is part of the loop

A taste checkpoint is a card with two or three options and their screens. The agent gets
`needs_owner` with a wait tool and can do other work. The answer is saved to the profile as
a preference, so the same question is not asked twice.

## 15. The agent experience is measured

A fixed set of small tasks ("check this page and fix what is measured", "find why the card
moved", "start a panel with a 2 round budget") is run by a model that has only the tool
descriptions and the skill. Recorded per task: finished or not, calls, tokens read, wrong
turns. Two models run it: a strong one and Haiku. A change to a tool, a result shape or a
description that makes these worse does not merge. Every wrong turn found in real use is
added as a task.

## 16. The environment explains itself

`studio_status` says what this install can do: which engines, which fonts, local or node,
which packs, which model keys for judging. An agent reads it once and does not guess. A call
that needs something missing fails with `unavailable` and the command that adds it.
