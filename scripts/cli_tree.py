"""Record the `agentos` command tree: every command and group path, the function it runs and
its parameters. `--write` refreshes tests/fixtures/cli/old_tree.json (only ever from the tree
as it was before the C1 regrouping: that file is what the hidden aliases must keep working)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import typer.main

FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "cli" / "old_tree.json"


def target(cmd) -> str:
    fn = cmd.callback
    fn = getattr(fn, "wraps_command", fn)  # agentos.cli_groups.with_flag: the command it extends
    fn = getattr(fn, "__wrapped__", fn)
    if fn is None:
        return ""
    return f"{fn.__module__}:{fn.__qualname__}"


def params(cmd) -> list[str]:
    out = []
    for p in cmd.params:
        if p.param_type_name == "argument":
            out.append(f"<{p.name}>")
        else:
            out.extend(sorted([*p.opts, *p.secondary_opts]))
    return sorted(out)


def walk(cmd, path: tuple[str, ...] = ()) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    if path:
        rows[" ".join(path)] = {"kind": "group" if hasattr(cmd, "commands") else "command",
                                "target": target(cmd), "params": params(cmd)}
    if hasattr(cmd, "commands"):  # typer vendors its own click, so no isinstance
        for name, sub in sorted(cmd.commands.items()):
            rows.update(walk(sub, (*path, name)))
    return rows


def tree() -> dict[str, dict]:
    from agentos.cli import app

    return walk(typer.main.get_command(app))


if __name__ == "__main__":
    rows = tree()
    if "--write" in sys.argv:
        FIXTURE.write_text(json.dumps(rows, indent=1, sort_keys=True) + "\n")
        print(f"wrote {len(rows)} paths to {FIXTURE}")
    else:
        print(json.dumps(rows, indent=1, sort_keys=True))
