"""Run the pilot's real decision code over a harvested walk, with no browser.

`ReplayTools` is a lane made of what the walk recorded: it shows the outline of the current point
and answers an action with the node's own recorded result. The pilot's own `_decide` runs at each
point, and its own `_do` then takes the walk's step, so history, deltas, flipped controls and every
other part of the state come from the pilot's code, whatever shape that code has today.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agentos.pilot import Pilot, Request, Value
from agentos.pilot.loop import Plan, State, Step, Stop
from agentos.pilot.questions import Unreadable
from lanebench.lab.harvest import DATA, HOST, LANE, walk_path
from lanebench.lab.jev import LabDecider, LabJev


class ReplayTools:
    def __init__(self, walk: dict[str, Any]):
        self.points = walk["points"]
        self.at = 0
        self.files = walk["files"]

    def _advance(self) -> dict[str, Any]:
        did = self.points[self.at]["did"] or {}
        self.at = min(self.at + 1, len(self.points) - 1)
        return did.get("result") or {"ok": True}

    async def look(self) -> None:
        return None

    async def call(self, name: str, **arguments: Any) -> Any:
        if name == "browser_snapshot":
            return self.points[self.at]["outline"]
        if name == "files_list":
            return {"inbox": [{"name": f, "id": f} for f in self.files]}
        if name in ("browser_act", "browser_upload", "browser_scroll"):
            return self._advance()
        return {"ok": True}


async def no_sleep(_seconds: float) -> None:
    return None


def request_of(walk: dict[str, Any]) -> Request:
    values = tuple(Value(key=v["key"], label=v["label"], text=v["text"], secret=v["secret"]) for v in walk["values"])
    return Request(lane=LANE, goal=walk["goal"], sites=(HOST,), values=values, files=tuple(walk["files"]),
                   read=walk["read"], seed=f"{walk['task']}:{walk['seed']}")


def gold_plan(req: Request, did: dict[str, Any]) -> Plan:
    value = next((v for v in req.values if v.key == did.get("value")), None)
    return Plan(did["op"], target=did.get("ref"), value=value)


def compact(answers: dict[str, Any]) -> dict[str, Any]:
    """An answer without its keys' long text: what the report's confidence tables need."""
    out: dict[str, Any] = {}
    for name, a in answers.items():
        if not isinstance(a, dict):
            continue
        if "noul" in a:
            out[name] = round(float(a["noul"]), 4)
        else:
            probs = a.get("probabilities") or {}
            top = sorted(probs.values(), reverse=True)
            out[name] = {"choice": a.get("choice"), "p": round(probs.get(a.get("choice"), 0.0), 4),
                         "second": round(top[1], 4) if len(top) > 1 else 0.0, "conf": round(float(a.get("confidence") or 0), 4),
                         "n": len(probs)}
    return out


def outcome(plan: Plan | None, stop: Stop | None) -> dict[str, Any]:
    if plan is not None:
        return {"op": plan.op, "ref": plan.target, "value": plan.value.key if plan.value else None, "agree": plan.agree,
                "why": plan.why}
    assert stop is not None
    return {"stop": stop.status.value, "reason": stop.reason, "answer": stop.answer}


async def decide_walk(walk: dict[str, Any], jev: LabJev) -> list[dict[str, Any]]:
    """One row per decision point. The pilot decides at each, then takes the walk's own step."""
    tools = ReplayTools(walk)
    decider = LabDecider(jev)
    pilot = Pilot(tools, decider, sleep=no_sleep)
    req = request_of(walk)
    st = State(started=True)
    st.files = await pilot._files(req)
    page = await pilot._observe(req)
    rows: list[dict[str, Any]] = []
    for point in walk["points"]:
        decider.requests.clear()
        plan: Plan | None = None
        stop: Stop | None = None
        step: Step | None = None
        row: dict[str, Any] = {}
        try:
            pilot._signals(req, page)
            if req.read:
                await pilot._read(req, st, page)
            step = pilot._frame(req, st, page)
            row = step.row
            plan = await pilot._decide(req, st, step)
        except Stop as s:
            stop = s
        except Unreadable as e:
            stop = Stop(status=_REFUSED, reason="unreadable_answer", message=str(e))
        rows.append({"task": walk["task"], "seed": walk["seed"], "variant": walk["variant"], "i": point["i"],
                     "kind": point["kind"], "gold": point["gold"], "lb": point["lb"], "mutations": walk["mutations"],
                     "adversarial": walk["adversarial"], "lang": walk["lang"], "got": outcome(plan, stop),
                     "answers": [compact(r["answers"]) for r in decider.requests],
                     "requests": len(decider.requests), "chars": sum(r["chars"] for r in decider.requests),
                     "row": {k: v for k, v in row.items() if k in ("op", "ran", "op_conf", "done", "needs_owner", "present")}})
        did = point["did"]
        if not did:
            break
        step = step or pilot._frame(req, st, page)
        page = await pilot._do(req, st, Step(page, step.pending, step.elements, {}, {}, {}), gold_plan(req, did))
    return rows


def load(jobs: list[tuple[Any, int, str]], root: Path = DATA) -> list[dict[str, Any]]:
    walks = []
    for task, seed, variant in jobs:
        path = walk_path(variant, task.id, seed, root)
        walk = json.loads(path.read_text()) if path.exists() else None
        if walk and walk["complete"] and walk["points"]:
            walks.append(walk)
    return walks


from agentos.pilot.models import Status

_REFUSED = Status.REFUSED
