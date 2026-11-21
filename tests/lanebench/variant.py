"""Variants: run the bench with other agent-facing text (`openajar lanebench run --variant DIR`).

A variant is a folder of up to four files, each optional:

- `instructions.md`: the MCP server's instructions.
- `tools.json`: {tool name: its full description}.
- `recipes.json`: {howto topic: {"title", "summary", "steps"}}. Only these text fields are merged into
  the real entries, which also hold the recipe's prompt arguments.
- `errors.json`: {error code: the next-step line MCP appends to errors of that code (`fix: ...`)}.

`openajar lanebench export-text DIR` writes the current text in the same format, so a variant starts
as an edited copy. The bench serves its own MCP server in its own process, so a variant changes
what that server says and nothing else: the instructions and tool descriptions are given to the
bench's server when it is built, and the recipes and error lines are swapped in this process for
the run (`applied`) and put back after. A node's daemon never sees a variant. The report records the
variant's folder, files and a digest of their contents.
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FILES = ("instructions.md", "tools.json", "recipes.json", "errors.json")
RECIPE_FIELDS = ("title", "summary", "steps")


class VariantError(ValueError):
    pass


@dataclass(frozen=True)
class Variant:
    path: Path
    files: tuple[str, ...] = ()
    digest: str = ""
    instructions: str | None = None
    tools: dict[str, str] = field(default_factory=dict)
    recipes: dict[str, dict[str, str]] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return {"dir": str(self.path), "files": list(self.files), "digest": self.digest}

    def describe(self, specs: Sequence[Any]) -> list[Any]:
        """The specs with the variant's descriptions (copies: a provider's own specs are untouched)."""
        from openajar.core.tools import ToolSpec

        return [dataclasses.replace(s, description=self.tools[s.name])
                if isinstance(s, ToolSpec) and s.name in self.tools else s for s in specs]

    def provider(self, provide: Callable[[Any], Any]) -> Callable[[Any], list[Any]]:
        def wrapped(runtime: Any) -> list[Any]:
            return self.describe(list(provide(runtime)))

        return wrapped


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except ValueError as e:
        raise VariantError(f"{path.name} is not JSON: {e}") from None


def _str_map(name: str, data: Any) -> dict[str, str]:
    if not isinstance(data, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in data.items()):
        raise VariantError(f"{name} must be a JSON object of strings")
    return data


def digest(folder: Path) -> tuple[tuple[str, ...], str]:
    """The variant files present, and a sha256 over their names and bytes (12 hex characters)."""
    present = tuple(f for f in FILES if (folder / f).is_file())
    h = hashlib.sha256()
    for name in present:
        h.update(name.encode() + b"\0" + (folder / name).read_bytes() + b"\0")
    return present, h.hexdigest()[:12]


def load(folder: Path, *, tool_names: Sequence[str] | None = None) -> Variant:
    """Read and check a variant folder. Names that match nothing (a tool, a topic, an error code) are
    refused: a typo would otherwise run the bench on the old text and say nothing."""
    from openajar.core import catalog
    from openajar.node.tools import RECIPES

    folder = Path(folder).expanduser()
    if not folder.is_dir():
        raise VariantError(f"{folder} is not a folder")
    files, sha = digest(folder)
    if not files:
        raise VariantError(f"{folder} holds none of {', '.join(FILES)}")
    instructions = (folder / "instructions.md").read_text() if "instructions.md" in files else None
    tools = _str_map("tools.json", _json(folder / "tools.json")) if "tools.json" in files else {}
    errors = _str_map("errors.json", _json(folder / "errors.json")) if "errors.json" in files else {}
    recipes: dict[str, dict[str, str]] = {}
    if "recipes.json" in files:
        raw = _json(folder / "recipes.json")
        if not isinstance(raw, dict):
            raise VariantError("recipes.json must be a JSON object of {topic: {title, summary, steps}}")
        for topic, fields in raw.items():
            if not isinstance(fields, dict):
                raise VariantError(f"recipes.json: {topic} must be an object")
            extra = set(fields) - set(RECIPE_FIELDS)
            if extra or not all(isinstance(v, str) for v in fields.values()):
                raise VariantError(f"recipes.json: {topic} may hold only text fields {', '.join(RECIPE_FIELDS)}")
            recipes[topic] = dict(fields)
    unknown = [t for t in recipes if t not in RECIPES]
    unknown += [c for c in errors if c not in catalog.entries()]
    if tool_names is not None:
        unknown += [t for t in tools if t not in tool_names]
    if unknown:
        raise VariantError(f"the variant names what the bench does not have: {', '.join(sorted(unknown))}")
    return Variant(path=folder, files=files, digest=sha, instructions=instructions, tools=tools, recipes=recipes,
                   errors=errors)


@contextlib.contextmanager
def applied(variant: Variant | None) -> Iterator[None]:
    """The variant's recipes and error lines in this process, for the run; the originals after."""
    if variant is None:
        yield
        return
    from openajar.core import catalog
    from openajar.node import tools as node_tools

    saved_recipes = {topic: dict(node_tools.RECIPES[topic]) for topic in variant.recipes}
    saved_entries = catalog.entries
    try:
        for topic, fields in variant.recipes.items():
            node_tools.RECIPES[topic].update(fields)
        if variant.errors:
            base = dict(saved_entries())
            patched = {code: dataclasses.replace(e, agent=variant.errors[code]) if code in variant.errors else e
                       for code, e in base.items()}
            catalog.entries = lambda: patched  # type: ignore[assignment]
        yield
    finally:
        for topic, entry in saved_recipes.items():
            node_tools.RECIPES[topic].clear()
            node_tools.RECIPES[topic].update(entry)
        catalog.entries = saved_entries  # type: ignore[assignment]


def current_text(specs: Sequence[Any]) -> dict[str, Any]:
    """The agent-facing text as the bench serves it, in the variant format."""
    from openajar.core import catalog
    from openajar.mcp.registry import tool_description
    from openajar.node.tools import INSTRUCTIONS, RECIPES

    return {
        "instructions.md": INSTRUCTIONS,
        "tools.json": {s.name: tool_description(s) for s in sorted(specs, key=lambda s: s.name)},
        "recipes.json": {topic: {f: r[f] for f in RECIPE_FIELDS if f in r} for topic, r in RECIPES.items()},
        "errors.json": {code: e.agent for code, e in sorted(catalog.entries().items())},
    }


def export(folder: Path, specs: Sequence[Any]) -> dict[str, int]:
    """Write the current text to `folder`; returns how many entries each file holds."""
    folder = Path(folder).expanduser()
    folder.mkdir(parents=True, exist_ok=True)
    counts = {}
    for name, content in current_text(specs).items():
        if isinstance(content, str):
            (folder / name).write_text(content)
            counts[name] = len(content)
        else:
            (folder / name).write_text(json.dumps(content, indent=1, ensure_ascii=False) + "\n")
            counts[name] = len(content)
    return counts


__all__ = ["FILES", "Variant", "VariantError", "applied", "current_text", "digest", "export", "load"]
