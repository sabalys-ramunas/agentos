"""Size guardrails for src/agentos: no new giant function, no new giant file, and listed ones only shrink.

Run it with `make complexity` (part of `make lint` and `make merge-check`).

- Functions: cyclomatic complexity from ruff's mccabe rule (C901). One of 25 or more fails unless
  scripts/complexity-allowlist.txt lists it (`path::Qualified.name complexity`), and a listed one
  fails when it gets more complex than its number.
- Files: a module over 1,000 lines fails unless scripts/file-length-allowlist.txt lists it
  (`path lines`), and a listed one fails when it grows past its number.

Both lists may only shrink. A line for a function or file that is now under the limit fails (delete
it); one that got smaller fails until its number comes down. `--update` does both for you: it
deletes what is under the limit and lowers numbers, and it never adds a line or raises a number.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "agentos"
MAX_COMPLEXITY = 24   # 25 or more fails
MAX_LINES = 1000      # 1,001 or more fails
FUNCTIONS = ROOT / "scripts" / "complexity-allowlist.txt"
FILES = ROOT / "scripts" / "file-length-allowlist.txt"
_TOO_COMPLEX = re.compile(r"is too complex \((\d+) > \d+\)")

HEADER = {
    FUNCTIONS: ("# Functions with cyclomatic complexity 25 or more (ruff C901), as they are today. This list may\n"
                "# only shrink: split a function and delete its line, and never add one. scripts/complexity.py.\n"),
    FILES: ("# Modules over 1,000 lines, with their length today. This list may only shrink: split a file and\n"
            "# delete its line, and never add one or raise a number. scripts/complexity.py.\n"),
}


def _qualnames(path: Path) -> dict[int, str]:
    """Line of each def -> its qualified name (`Class.method`, `outer.inner`)."""
    out: dict[int, str] = {}

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                name = f"{prefix}{child.name}"
                if not isinstance(child, ast.ClassDef):
                    out[child.lineno] = name
                walk(child, name + ".")
            else:
                walk(child, prefix)

    walk(ast.parse(path.read_text(encoding="utf-8")), "")
    return out


def measure_functions() -> dict[str, int]:
    proc = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--isolated", "--select", "C901",
         "--config", f"lint.mccabe.max-complexity = {MAX_COMPLEXITY}", "--output-format", "json",
         "--exit-zero", str(SRC)],
        capture_output=True, text=True, check=True, cwd=ROOT)
    found: dict[str, int] = {}
    names: dict[Path, dict[int, str]] = {}
    for item in json.loads(proc.stdout or "[]"):
        path = Path(item["filename"])
        m = _TOO_COMPLEX.search(item["message"])
        if not m:
            continue
        if path not in names:
            names[path] = _qualnames(path)
        line = item["location"]["row"]
        qual = names[path].get(line, f"line{line}")
        key = f"{path.relative_to(ROOT)}::{qual}"
        n = 2
        while key in found:  # the same name twice in one file
            key = f"{path.relative_to(ROOT)}::{qual}[{n}]"
            n += 1
        found[key] = int(m.group(1))
    return found


def measure_files() -> dict[str, int]:
    found = {}
    for path in sorted(SRC.rglob("*.py")):
        lines = len(path.read_text(encoding="utf-8").splitlines())
        if lines > MAX_LINES:
            found[str(path.relative_to(ROOT))] = lines
    return found


def read_list(path: Path) -> dict[str, int]:
    out = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.split("#", 1)[0].strip()
            if line:
                key, number = line.rsplit(None, 1)
                out[key] = int(number)
    return out


def write_list(path: Path, entries: dict[str, int]) -> None:
    body = "".join(f"{k} {v}\n" for k, v in sorted(entries.items()))
    path.write_text(HEADER[path] + body, encoding="utf-8")


def compare(what: str, unit: str, limit: str, listed: dict[str, int], now: dict[str, int],
            list_path: Path) -> tuple[list[str], dict[str, int]]:
    problems = []
    tightened = {}
    rel = list_path.relative_to(ROOT)
    unit = f" {unit}" if unit else ""
    for key, value in sorted(now.items()):
        if key not in listed:
            problems.append(f"{key}: {what} is {value}{unit} ({limit}). Split it; {rel} only shrinks.")
        elif value > listed[key]:
            problems.append(f"{key}: {what} grew from {listed[key]} to {value}{unit}. Listed ones must not "
                            "grow: split it, or move the new code out.")
    for key, value in sorted(listed.items()):
        if key not in now:
            problems.append(f"{key}: now under the limit. Delete its line from {rel} (or run --update).")
        elif now[key] < value:
            problems.append(f"{key}: down to {now[key]}{unit} from {value}. Lower its number in {rel} "
                            "(or run --update).")
    for key, value in listed.items():
        if key in now:
            tightened[key] = min(value, now[key])
    return problems, tightened


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--update", action="store_true",
                        help="delete lines under the limit and lower numbers (never adds or raises)")
    parser.add_argument("--init", action="store_true", help=argparse.SUPPRESS)  # writes both lists from scratch
    args = parser.parse_args(argv)
    functions, files = measure_functions(), measure_files()
    if args.init:
        write_list(FUNCTIONS, functions)
        write_list(FILES, files)
        print(f"wrote {len(functions)} functions and {len(files)} files")
        return 0
    f_problems, f_tight = compare("complexity", "", f"{MAX_COMPLEXITY + 1} or more fails",
                                  read_list(FUNCTIONS), functions, FUNCTIONS)
    l_problems, l_tight = compare("length", "lines", f"over {MAX_LINES} fails", read_list(FILES), files, FILES)
    if args.update:
        write_list(FUNCTIONS, f_tight)
        write_list(FILES, l_tight)
        f_problems = [p for p in f_problems if "Split it" in p or "grew" in p]
        l_problems = [p for p in l_problems if "Split it" in p or "grew" in p]
    problems = f_problems + l_problems
    for p in problems:
        print(p)
    if problems:
        print(f"\ncomplexity: {len(problems)} problem(s). The rules are in docs/DEVELOPING.md (Layers and size limits).")
        return 1
    print(f"complexity: ok ({len(functions)} functions of {MAX_COMPLEXITY + 1} or more and {len(files)} files "
          f"over {MAX_LINES:,} lines, all listed)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
