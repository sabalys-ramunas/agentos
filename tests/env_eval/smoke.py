"""The smoke checks: what can be read from the product's own definitions, with no node, no Docker and no
model (`run.py smoke`, and tests/unit/test_env_eval.py in CI).

Each check is a value against its limit in tests/env_eval/budgets.toml: the error catalog's steps and fixes,
the environment skill's length, and the everyday tool list an agent reads in every chat (how many tools, the
longest description, all of them together, and arguments with no description). The tool list's token budget
is tests/unit/test_toolsets.py's; these are the numbers behind it that an agent stumbles on.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
BUDGETS = Path(__file__).resolve().parent / "budgets.toml"
SKILL = REPO / "src" / "openajar" / "client" / "skills" / "openajar-environment" / "SKILL.md"
EVERYDAY = "core"  # the toolset an agent sees without asking for more


def budgets() -> dict[str, Any]:
    return tomllib.loads(BUDGETS.read_text())


def _cli_json(*args: str) -> Any:
    cli = str(Path(sys.executable).parent / "openajar")
    out = subprocess.run([cli, *args, "--json"], capture_output=True, text=True, timeout=120, check=False, cwd=REPO)
    if out.returncode != 0:
        raise RuntimeError(f"`openajar {' '.join(args)} --json` failed: {out.stderr.strip()[-300:]}")
    return json.loads(out.stdout)


def limit_check(value: float, limit: float, detail: str = "") -> dict[str, Any]:
    return {"value": value, "limit": limit, "ok": value <= limit, "detail": detail if value > limit else ""}


def run() -> dict[str, dict[str, Any]]:
    lim = budgets()["static"]
    errors, tools = _cli_json("errors"), _cli_json("tools")
    everyday = [t for t in tools if t.get("toolset") == EVERYDAY]
    no_agent = [e["code"] for e in errors if not (e.get("agent") or "").strip()]
    no_fix = [e["code"] for e in errors if not (e.get("fix") or "").strip()]
    longest = max(everyday, key=lambda t: len(t["description"]))
    undescribed = [f"{t['name']}.{p['name']}" for t in everyday for p in t["params"]
                   if not (p.get("description") or "").strip()]
    return {
        "errors with no step for the agent": limit_check(len(no_agent), lim["errors_without_agent_step"], ", ".join(no_agent[:8])),
        "errors with no fix for the person": limit_check(len(no_fix), lim["errors_without_fix"], ", ".join(no_fix[:8])),
        "skill length (words)": limit_check(len(SKILL.read_text().split()), lim["skill_words"]),
        "longest everyday tool description (characters)": limit_check(
            len(longest["description"]), lim["tool_description_chars_max"], longest["name"]),
        "everyday tools": limit_check(len(everyday), lim["everyday_tools"]),
        "everyday tool descriptions together (characters)": limit_check(
            sum(len(t["description"]) for t in everyday), lim["everyday_description_chars"]),
        "everyday tool arguments with no description": limit_check(
            len(undescribed), lim["everyday_params_undescribed"], ", ".join(undescribed[:10])),
    }


def show(checks: dict[str, dict[str, Any]]) -> str:
    lines = []
    for name, c in checks.items():
        mark = "ok  " if c["ok"] else "OVER"
        lines.append(f"{mark} {name}: {c['value']} (limit {c['limit']}){'  ' + c['detail'] if c['detail'] else ''}")
    return "\n".join(lines)
