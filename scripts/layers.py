"""The layer check: every import in src/agentos goes down the layer order in scripts/layers.toml.

Run it with `make layers` (part of `make lint` and `make merge-check`). It reads every module with
`ast`, so an import inside a function counts the same as one at the top of a file, and so does one
under `if TYPE_CHECKING:`. It fails when:

- a module imports a module on a higher layer, and that import is not in the exceptions list;
- packages on the same layer import each other in a circle (A imports B and B imports A, or longer);
- an exception no longer matches any import (it was fixed: delete its line).

The exceptions list may only shrink. When you fix one, delete its line. Do not add lines: put the
code on the layer it belongs to, move a shared type or Protocol down, or pass a callback in.

`--report` prints every import that breaks the order, grouped by package pair, and `--cycles` the
pairs of top-level packages that import each other (ignoring layers and exceptions).
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import sys
import tomllib
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
PACKAGE = "agentos"
CONFIG = ROOT / "scripts" / "layers.toml"


@dataclass(frozen=True)
class Import:
    importer: str  # agentos.node.forms
    imported: str  # agentos.api.jsonable (the module, or the package when the name is not a module)
    path: str      # src/agentos/node/forms.py
    line: int


def module_name(path: Path) -> str:
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def all_modules() -> dict[str, Path]:
    return {module_name(p): p for p in sorted((SRC / PACKAGE).rglob("*.py"))}


def _resolve(name: str, modules: dict[str, Path]) -> str:
    """The longest prefix of `name` that is a module (`agentos.node.egress.X` -> `agentos.node.egress`)."""
    parts = name.split(".")
    while parts:
        candidate = ".".join(parts)
        if candidate in modules:
            return candidate
        parts.pop()
    return name


def imports_of(mod: str, path: Path, modules: dict[str, Path]) -> list[Import]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    is_package = path.name == "__init__.py"
    rel = str(path.relative_to(ROOT))
    out: list[Import] = []
    for node in ast.walk(tree):
        targets: list[str] = []
        if isinstance(node, ast.Import):
            targets = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = mod.split(".") if is_package else mod.split(".")[:-1]
                base = base[: len(base) - (node.level - 1)]
                stem = ".".join(base + ([node.module] if node.module else []))
            else:
                stem = node.module or ""
            # `from agentos.node import egress` imports the module agentos.node.egress
            targets = [f"{stem}.{alias.name}" if f"{stem}.{alias.name}" in modules else stem
                       for alias in node.names]
        for target in targets:
            if target == PACKAGE or target.startswith(PACKAGE + "."):
                out.append(Import(mod, _resolve(target, modules), rel, node.lineno))
    return out


def top_package(mod: str) -> str:
    parts = mod.split(".")
    return parts[1] if len(parts) > 1 else parts[0]


class Layers:
    def __init__(self, config: dict) -> None:
        self.names: list[str] = []
        self.packages: dict[str, int] = {}   # "node" -> layer index (0 is the top)
        self.patterns: list[tuple[str, int]] = []  # ("*.cli", 0), ("node.daemon", 0)
        for i, layer in enumerate(config["layer"]):
            self.names.append(layer["name"])
            for member in layer["members"]:
                if "*" in member or "." in member:
                    self.patterns.append((member, i))
                else:
                    self.packages[member] = i
        self.exceptions: dict[str, str] = dict(config.get("exceptions", {}))

    def place(self, mod: str) -> tuple[int, str] | None:
        """(layer index, unit) for a module. A pattern (`*.cli`, `node.daemon`) wins over its package's
        layer. The unit is what the cycle check counts as one node: the top-level package, or the
        pattern's module when that sits on another layer than its package."""
        rest = mod.split(".", 1)[1] if "." in mod else ""
        pkg = top_package(mod)
        for pattern, i in self.patterns:
            if fnmatch.fnmatchcase(rest, pattern) or fnmatch.fnmatchcase(rest, pattern + ".*"):
                if self.packages.get(pkg) == i:
                    return i, pkg
                parts = rest.split(".")
                head = next(".".join(parts[:n]) for n in range(1, len(parts) + 1)
                            if fnmatch.fnmatchcase(".".join(parts[:n]), pattern))
                return i, head  # its own node in the cycle check: `runner.workflows.cli`
        if pkg in self.packages:
            return self.packages[pkg], pkg
        return None


def edge_key(imp: Import) -> str:
    return f"{imp.importer} -> {imp.imported}"


def scan() -> list[Import]:
    modules = all_modules()
    found: list[Import] = []
    for mod, path in modules.items():
        found += imports_of(mod, path, modules)
    return sorted(set(found), key=lambda i: (i.path, i.line, i.imported))


def cycles(edges: dict[str, set[str]]) -> list[list[str]]:
    """Strongly connected components with more than one node (Tarjan, iterative)."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on: set[str] = set()
    out: list[list[str]] = []
    counter = 0
    nodes = sorted(set(edges) | {b for bs in edges.values() for b in bs})
    for root in nodes:
        if root in index:
            continue
        work = [(root, iter(sorted(edges.get(root, ()))))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on.add(root)
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on.add(w)
                    work.append((w, iter(sorted(edges.get(w, ())))))
                    advanced = True
                    break
                if w in on:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == v:
                        break
                if len(comp) > 1:
                    out.append(sorted(comp))
    return out


def package_cycles(found: list[Import]) -> list[tuple[str, str, int, int]]:
    count: dict[tuple[str, str], int] = defaultdict(int)
    for imp in found:
        a, b = top_package(imp.importer), top_package(imp.imported)
        if a != b:
            count[(a, b)] += 1
    return sorted(((a, b, n, count[(b, a)]) for (a, b), n in count.items() if a < b and (b, a) in count),
                  key=lambda x: -(x[2] + x[3]))


def check(layers: Layers, found: list[Import], report: bool) -> int:
    problems: list[str] = []
    unplaced = sorted({top_package(i.importer) for i in found if layers.place(i.importer) is None}
                      | {top_package(i.imported) for i in found if layers.place(i.imported) is None})
    for pkg in unplaced:
        problems.append(f"agentos.{pkg} is on no layer: add it to scripts/layers.toml")
    used: set[str] = set()
    upward: dict[tuple[str, str], list[Import]] = defaultdict(list)
    same_layer: dict[str, set[str]] = defaultdict(set)
    same_layer_imports: dict[tuple[str, str], list[Import]] = defaultdict(list)
    for imp in found:
        a, b = layers.place(imp.importer), layers.place(imp.imported)
        if a is None or b is None:
            continue
        key = edge_key(imp)
        if key in layers.exceptions:
            used.add(key)
            continue
        (la, ua), (lb, ub) = a, b
        if lb < la:
            upward[(top_package(imp.importer), top_package(imp.imported))].append(imp)
        elif la == lb and ua != ub:
            same_layer[ua].add(ub)
            same_layer_imports[(ua, ub)].append(imp)
    for (a, b), imps in sorted(upward.items()):
        for imp in imps:
            la, lb = layers.place(imp.importer)[0], layers.place(imp.imported)[0]  # type: ignore[index]
            problems.append(f"{imp.path}:{imp.line}: {imp.importer} ({layers.names[la]}) imports "
                            f"{imp.imported} ({layers.names[lb]}), a higher layer")
    for comp in cycles(same_layer):
        lines = [f"  {imp.path}:{imp.line}: {edge_key(imp)}"
                 for a in comp for b in comp if b in same_layer.get(a, ()) for imp in same_layer_imports[(a, b)]]
        problems.append(f"these import each other in a circle: {', '.join(comp)}\n" + "\n".join(lines))
    for key in layers.exceptions:
        if key not in used:
            problems.append(f"stale exception (nothing imports this any more): delete \"{key}\" "
                            "from scripts/layers.toml")
    if report:
        for (a, b), imps in sorted(upward.items(), key=lambda kv: -len(kv[1])):
            print(f"== {a} -> {b}: {len(imps)}")
            for imp in imps:
                print(f"  {imp.path}:{imp.line}: {edge_key(imp)}")
    for p in problems:
        print(p)
    if problems:
        print(f"\nlayers: {len(problems)} problem(s). The order and the rules are in scripts/layers.toml "
              "and docs/ARCHITECTURE.md (section 13, Layers).")
        return 1
    print(f"layers: ok ({len(found)} imports, {len(layers.exceptions)} listed exceptions)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--report", action="store_true", help="print each import that breaks the order")
    parser.add_argument("--cycles", action="store_true", help="print the package pairs that import each other")
    args = parser.parse_args(argv)
    found = scan()
    if args.cycles:
        pairs = package_cycles(found)
        for a, b, n, m in pairs:
            print(f"{a}/{b} {n}/{m}")
        print(f"{len(pairs)} pairs of packages import each other")
        return 0
    with CONFIG.open("rb") as f:
        layers = Layers(tomllib.load(f))
    return check(layers, found, args.report)


if __name__ == "__main__":
    sys.exit(main())
