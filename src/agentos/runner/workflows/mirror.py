"""What of a workflow run leaves the node: the one rule (docs/WORKFLOWS.md, "What leaves the node").

`run_view` is a run as the node has it, for a person there and for an agent, whose family is labelled
for reading it (`tools.py`). `mirror` is the same run for everywhere else: the `workflow_run` event, a
`workflow_result`, and what a workflow's row says of its last runs, which reaches the controller and
any agent. Content stays on the node. That a run happened, how it went and how much arrived does not.

Only what these tables name crosses, so a field added to `run_view` stays on the node until someone
adds it here. The owner has not made this final: to send more or less, change the tables, nothing else.
"""

from __future__ import annotations

import json
from typing import Any

# of a run: ids, the node's own words for what set it off and who started it, times, and what it cost
RUN_FIELDS = ("id", "flow", "version", "revision", "status", "cause", "by", "started_at", "finished_at", "cost")
# of a step: ids, a label someone wrote in the definition, the node's own few words (counts, yes or no),
# times, and `arrived`, the kind of what started the run
STEP_FIELDS = ("step", "label", "kind", "status", "short", "started_at", "finished_at", "attempts", "approval_id",
               "run_id", "why", "arrived")
# of a row of a loop: its number, and the node's own words for why it stopped
ITEM_FIELDS = ("label", "status", "note")
# of a run's output card: the kind, a step's label, and the node's own words
OUTPUT_FIELDS = ("kind", "title", "more")
# of what went in and came out of a step, what the node itself wrote there: counts, a step's id, and
# which way it went. An action, a notify and a trigger have none: what they hold came from somewhere.
NODE_VALUES: dict[str, dict[str, tuple[str, ...]]] = {
    "branch": {"output": ("took",)},
    "loop": {"input": ("items",), "output": ("count", "stopped")},
    "wait": {"input": ("amount", "unit")},
    "ask": {"output": ("answer",)},
    "stop_if_replied": {"input": ("after",), "output": ("replied", "waiting")},
}
# in place of a failure's words that may quote a step's value, a site's answer or a crash
WITHHELD_WORDS = "The details stay on the computer that ran it."


def withheld(values: dict[str, Any]) -> dict[str, Any]:
    """In place of values: that there were some, and how much. `fields` is a count, because the names
    of what a webhook sent are a stranger's too."""
    rows = sum(len(v) for v in values.values() if isinstance(v, list))
    return {"withheld": True, "fields": len(values), "size": len(json.dumps(values, ensure_ascii=False, default=str)),
            **({"rows": rows} if rows else {})}


def _values(step: dict[str, Any], which: str) -> dict[str, Any] | None:
    values = step.get(which)
    if not values:
        return None
    own = NODE_VALUES.get(step.get("kind", ""), {}).get(which, ())
    rest = {k: v for k, v in values.items() if k not in own}
    return {**{k: v for k, v in values.items() if k in own}, **(withheld(rest) if rest else {})}


def _step(step: dict[str, Any]) -> dict[str, Any]:
    out = {k: step[k] for k in STEP_FIELDS if k in step}
    out["input"], out["output"] = _values(step, "input"), _values(step, "output")
    out["note"] = WITHHELD_WORDS if step.get("note_outside") else step.get("note")
    if "items" in step:
        out["items"] = [{k: item[k] for k in ITEM_FIELDS if k in item} for item in step["items"]]
    return out


def mirror(view: dict[str, Any]) -> dict[str, Any]:
    """A `run_view` as it may leave the node."""
    output = view.get("output")
    return {**{k: view[k] for k in RUN_FIELDS if k in view},
            "result": WITHHELD_WORDS if view.get("result_outside") else view.get("result"),
            "output": {k: output[k] for k in OUTPUT_FIELDS if k in output} if isinstance(output, dict) else None,
            "steps": [_step(s) for s in view.get("steps") or []]}
