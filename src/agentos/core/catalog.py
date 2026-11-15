"""The error catalog: what every error code means, and what to do about it.

The codes come from the error classes themselves (every AgentosError subclass that sets its own
`code`, or lists the codes it is raised with in `codes`), so nothing here is a hand-kept list of
codes. What the catalog adds is the text, kept in error_catalog.toml next to this file:

    [busy]
    meaning = "Every warm lane slot is in use and none freed up in time."   # plain language
    agent = "Wait, then call again; or close a lane you no longer need."    # next step for an agent
    fix = "Close idle lanes, or raise max_warm_lanes in node.toml."         # fix for a person
    retryable = true
    retry_after = 30          # seconds; leave out when the wait is not known
    shared_by = ["agentos.memory.errors.RateLimited"]   # other classes that raise this code
    reasons = { expired = "..." }   # optional: a better agent step per "<reason>: ..." message
    surfaces = { mcp = "..." }      # optional: the person's fix on another HTTP surface than the panel
                                    # (mcp: the node's /mcp; controller; controller_mcp: the controller's /mcp)

The same entry reaches all three surfaces: MCP tool results (`fix:` and `retry_after:` lines
under the `<code>: <message>` line), CLI errors (`error: <code>: <message>` plus a `fix:` line),
and API error bodies (`{"error": {"code", "message", "fix"}}`). docs/ERRORS.md is generated from it
(`agentos errors --markdown`).

Runs fail with codes too (`run.error.code`: wrong_surface, grant_expired, ...), set by the runner
rather than raised as classes. `run_codes()` collects them from the runner's source: every
`{"code": "<literal>"}`, every `StepFailure("<literal>", ...)`, every string a function assigns
to a name `code`, and the values of module-level `*_CODES` dicts (codes picked at run time, such
as GRANT_CODES), written out or imported from agentos.core.signals (`SIGNAL_CODES = RUN_SIGNAL_CODES`).
A code made any other way (an f-string) has no entry and is not seen, so build it from a `*_CODES` dict.

tests/unit/test_error_catalog.py fails when a class or a run has a code without an entry, an
entry has neither, or two classes raise one code without the entry saying they share it.
"""

from __future__ import annotations

import ast
import functools
import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

CATALOG_FILE = "error_catalog.toml"
# the HTTP surfaces besides the node's panel API whose person may need another fix (`surfaces` in an entry)
SURFACES = ("mcp", "controller", "controller_mcp", "viewer")
SURFACE_NAMES = {"mcp": "the node's /mcp", "controller": "the controller", "controller_mcp": "the controller's /mcp",
                 "viewer": "a lane's live view"}


@dataclass(frozen=True)
class Entry:
    code: str
    meaning: str
    agent: str
    fix: str
    retryable: bool = False
    retry_after: int | None = None
    shared_by: tuple[str, ...] = ()
    reasons: dict[str, str] = field(default_factory=dict)
    surfaces: dict[str, str] = field(default_factory=dict)

    def fix_on(self, surface: str | None) -> str:
        """The person's fix on `surface` (SURFACES), else the panel's."""
        return self.surfaces.get(surface or "", self.fix)

    def agent_step(self, message: str = "") -> str:
        """The agent's next step; a reason-specific one when the message starts with a known
        reason (policy denials read "<reason>: <detail>")."""
        reason = message.split(":", 1)[0].strip() if ":" in message else ""
        return self.reasons.get(reason, self.agent)

    def retry_text(self) -> str:
        if not self.retryable:
            return "no"
        return f"yes, after {self.retry_after} s" if self.retry_after else "yes, once the cause is fixed"


@functools.cache
def entries() -> dict[str, Entry]:
    raw = tomllib.loads(resources.files(__package__).joinpath(CATALOG_FILE).read_text("utf-8"))
    out: dict[str, Entry] = {}
    for code, item in raw.items():
        out[code] = Entry(
            code=code, meaning=item["meaning"], agent=item["agent"], fix=item["fix"],
            retryable=bool(item.get("retryable", False)), retry_after=item.get("retry_after"),
            shared_by=tuple(item.get("shared_by", ())), reasons=dict(item.get("reasons", {})),
            surfaces=dict(item.get("surfaces", {})))
    unknown = {(code, s) for code, e in out.items() for s in e.surfaces if s not in SURFACES}
    if unknown:
        raise ValueError(f"{CATALOG_FILE}: unknown surfaces {sorted(unknown)}")
    return out


def entry(code: str | None) -> Entry | None:
    return entries().get(code or "")


def person_fix(code: str | None, surface: str | None = None) -> str | None:
    found = entry(code)
    return found.fix_on(surface) if found else None


# ---- the three surfaces ----

def cli_error(code: str, message: str) -> str:
    """`error: <code>: <message>`, and on the next line the person's fix when there is one. A misspelt
    lane or workspace id names the close one first (agentos.core.suggest)."""
    from agentos.core.suggest import owner_message

    message = owner_message(code, message) if message else message
    line = f"error: {code}: {message}" if message else f"error: {code}"
    fix = person_fix(code)
    return f"{line}\n  fix: {fix}" if fix else line


def mcp_lines(code: str, message: str) -> list[str]:
    """Lines an MCP error result adds under `<code>: <message>`: the agent's next step and,
    when a wait helps, how long to wait."""
    found = entry(code)
    if found is None:
        return []
    lines = [f"fix: {found.agent_step(message)}"]
    if found.retryable and found.retry_after:
        lines.append(f"retry_after: {found.retry_after}")
    return lines


MCP_LINE_PREFIXES = ("fix: ", "retry_after: ")


def without_mcp_lines(text: str) -> str:
    """An MCP error text without the catalog's lines, for relays that carry code and message
    and add the lines again on the other side (the node channel and the controller)."""
    lines = text.split("\n")
    while len(lines) > 1 and lines[-1].startswith(MCP_LINE_PREFIXES):
        lines.pop()
    return "\n".join(lines)


# The catalog's fixes are written for the owner and for agents: many name a command, a token or a grant.
# A member (someone the owner invited) is never told those, so their error body gets the fix only when it
# names none of them, and otherwise the message alone.
_NOT_FOR_MEMBERS = re.compile(r"`|\bagentos\b|\bcommands?\b|\btokens?\b|\bgrants?\b|node\.toml", re.IGNORECASE)


def member_fix(code: str | None, surface: str | None = None) -> str | None:
    """The fix for a member: the catalog's, when it names no command, token or grant. Else None."""
    fix = person_fix(code, surface)
    return None if fix is None or _NOT_FOR_MEMBERS.search(fix) else fix


def error_body(code: str, message: str, *, surface: str | None = None, member: bool = False,
               **extra: Any) -> dict[str, Any]:
    """The API error envelope: `{"error": {"code", "message", "fix"?, ...extra}}`. `surface` (SURFACES)
    picks the fix for a surface other than the node's panel API. `member`: the caller is a member, so the
    fix is one a member may read (see member_fix)."""
    body: dict[str, Any] = {"code": code, "message": message}
    fix = member_fix(code, surface) if member else person_fix(code, surface)
    if fix:
        body["fix"] = fix
    return {"error": {**body, **extra}}


# ---- collection from the error classes ----

@dataclass
class Scan:
    by_code: dict[str, list[str]]  # code -> classes that raise it (module.qualname)
    failed: list[tuple[str, str]]  # modules that could not be imported, with the error
    run_codes: dict[str, list[str]] = field(default_factory=dict)  # run.error.code -> modules that set it


def _subclasses(root: type) -> Iterable[type]:
    seen: set[type] = set()
    stack = [root]
    while stack:
        cls = stack.pop()
        if cls in seen:
            continue
        seen.add(cls)
        yield cls
        stack.extend(cls.__subclasses__())


def class_name(cls: type) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


def class_codes(cls: type) -> list[str]:
    """Codes a class introduces: its own `code`, plus `codes` for a class raised with a code
    picked at the raise site (agentos.api.errors.ApiError). Inherited codes do not count."""
    found: list[str] = []
    own = cls.__dict__.get("code")
    if isinstance(own, str):
        found.append(own)
    found.extend(c for c in cls.__dict__.get("codes", ()) if c not in found)
    return found


RUN_PACKAGE = "agentos.runner"
SHARED_CODES_MODULE = "agentos.core.signals"  # where the signal-to-code table lives (runner.executor imports it)


class _RunCodes(ast.NodeVisitor):
    """String literals that become a run's error code in one module (see the module docstring)."""

    def __init__(self) -> None:
        self.found: set[str] = set()
        self.unseen: list[int] = []  # lines where a code is built (an f-string): no entry can match it
        self._skip: set[int] = set()
        self._depth = 0  # inside a function: `code = ...` there is a local, not a class attribute
        self._imported: dict[str, tuple[str, str]] = {}  # local name: (module, name) of a module-level import

    @classmethod
    def _strings(cls, node: ast.AST) -> list[str]:
        """The literal strings an expression can evaluate to: a constant, either side of `a if c
        else b` or `a or b`, and the default of `<map>.get(key, default)`."""
        if isinstance(node, ast.Constant):
            return [node.value] if isinstance(node.value, str) and node.value else []
        if isinstance(node, ast.IfExp):
            return cls._strings(node.body) + cls._strings(node.orelse)
        if isinstance(node, ast.BoolOp):
            return [s for v in node.values for s in cls._strings(v)]
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get" \
                and len(node.args) == 2:
            return cls._strings(node.args[1])
        return []

    def _function(self, node: ast.AST) -> None:
        self._depth += 1
        self.generic_visit(node)
        self._depth -= 1

    visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _function

    def visit_Dict(self, node: ast.Dict) -> None:
        if id(node) in self._skip:
            return
        for key, value in zip(node.keys, node.values, strict=True):
            if not isinstance(key, ast.Constant):
                continue
            if key.value == "code" and isinstance(value, ast.Constant) and isinstance(value.value, str):
                self.found.add(value.value)
            elif key.value == "code" and isinstance(value, ast.JoinedStr):
                self.unseen.append(value.lineno)
            if key.value == "error" and isinstance(value, ast.Dict):
                self._skip.add(id(value))  # {"error": {"code": ...}}: an HTTP error body, not a run's error
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
        if name == "StepFailure":
            first = node.args[0] if node.args else None
            for arg in [first, *(k.value for k in node.keywords if k.arg == "code")]:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    self.found.add(arg.value)
                elif isinstance(arg, ast.JoinedStr):
                    self.unseen.append(arg.lineno)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if not self._depth and node.module and node.level == 0:
            for alias in node.names:
                self._imported[alias.asname or alias.name] = (node.module, alias.name)
        self.generic_visit(node)

    def _imported_codes(self, name: str) -> list[str]:
        """The string values of a `*_CODES` dict the module imports from agentos.core.signals
        (`SIGNAL_CODES = RUN_SIGNAL_CODES`): the module sets those codes as if it wrote the dict."""
        module, attr = self._imported.get(name, ("", ""))
        if module != SHARED_CODES_MODULE:
            return []
        import importlib

        value = getattr(importlib.import_module(module), attr, None)
        return [v for v in value.values() if isinstance(v, str) and v] if isinstance(value, dict) else []

    def visit_Assign(self, node: ast.Assign) -> None:
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if self._depth and "code" in names:
            self.found.update(self._strings(node.value))
        if not self._depth and any(n.endswith("_CODES") for n in names) and isinstance(node.value, ast.Dict):
            self.found.update(v.value for v in node.value.values
                              if isinstance(v, ast.Constant) and isinstance(v.value, str))
        if not self._depth and any(n.endswith("_CODES") for n in names) and isinstance(node.value, ast.Name):
            self.found.update(self._imported_codes(node.value.id))
        self.generic_visit(node)


def source_run_codes(source: str, filename: str = "<source>") -> tuple[set[str], list[int]]:
    """(the run codes one module's source sets, lines where it builds one the catalog cannot see)."""
    visitor = _RunCodes()
    visitor.visit(ast.parse(source, filename))
    return visitor.found, visitor.unseen


def _run_modules(package: str) -> Iterable[tuple[str, Path]]:
    import importlib.util

    spec = importlib.util.find_spec(package)
    for root in (spec.submodule_search_locations or []) if spec else []:
        for path in sorted(Path(root).rglob("*.py")):
            rel = path.relative_to(root).with_suffix("")
            yield ".".join((package, *rel.parts)).removesuffix(".__init__"), path


def run_codes(package: str = RUN_PACKAGE) -> dict[str, list[str]]:
    """{run.error.code: modules that set it}, read from the package's source without importing it
    (only agentos.core.signals is imported, for a `*_CODES` table a module takes from there)."""
    found: dict[str, set[str]] = {}
    for module, path in _run_modules(package):
        for code in source_run_codes(path.read_text("utf-8"), str(path))[0]:
            found.setdefault(code, set()).add(module)
    return {code: sorted(mods) for code, mods in sorted(found.items())}


def unseen_run_codes(package: str = RUN_PACKAGE) -> list[str]:
    """`module:line` of every run code built at run time (an f-string), which no entry can match."""
    return [f"{module}:{line}" for module, path in _run_modules(package)
            for line in source_run_codes(path.read_text("utf-8"), str(path))[1]]


def scan(package: str = "agentos") -> Scan:
    """Import every module of the package and collect the codes of every AgentosError subclass.
    `__main__` modules are skipped (they run a command on import)."""
    import importlib
    import pkgutil

    from agentos.core.errors import AgentosError

    pkg = importlib.import_module(package)
    failed: list[tuple[str, str]] = []
    for mod in pkgutil.walk_packages(pkg.__path__, f"{package}.", onerror=lambda name: failed.append((name, "walk"))):
        if mod.name.rsplit(".", 1)[-1] == "__main__":
            continue
        try:
            importlib.import_module(mod.name)
        except Exception as exc:  # noqa: BLE001  (reported; the test fails on any)
            failed.append((mod.name, f"{type(exc).__name__}: {exc}"))
    by_code: dict[str, list[str]] = {}
    for cls in _subclasses(AgentosError):
        if not cls.__module__.startswith(package):
            continue  # test doubles and plugins outside the package
        for code in class_codes(cls):
            by_code.setdefault(code, []).append(class_name(cls))
    for names in by_code.values():
        names.sort()
    runs = run_codes(RUN_PACKAGE) if package == "agentos" else {}
    return Scan(by_code=dict(sorted(by_code.items())), failed=failed, run_codes=runs)


def problems(found: Scan, catalog: dict[str, Entry] | None = None) -> list[str]:
    """What is wrong between the classes and the catalog, one line each (empty when in step)."""
    catalog = entries() if catalog is None else catalog
    out = [f"{name} could not be imported ({err})" for name, err in found.failed]
    for code, names in found.by_code.items():
        item = catalog.get(code)
        if item is None:
            out.append(f"{code}: no entry in {CATALOG_FILE} (raised by {', '.join(names)})")
            continue
        owners = [n for n in names if n not in item.shared_by]  # exactly one class owns a code
        if len(owners) != 1:
            out.append(f"{code}: raised by {', '.join(names)}; one code is one meaning, so give each class "
                       f"its own code, or list the classes that mean the same in shared_by")
        for name in item.shared_by:
            if name not in names:
                out.append(f"{code}: shared_by names {name}, which does not raise it")
    for code, modules in found.run_codes.items():
        if code not in catalog:
            out.append(f"{code}: no entry in {CATALOG_FILE} (a run failure code set in {', '.join(modules)})")
    for code in catalog:
        if code not in found.by_code and code not in found.run_codes:
            out.append(f"{code}: in {CATALOG_FILE} but no error class raises it and no run fails with it")
    for code, item in catalog.items():
        texts = [("meaning", item.meaning), ("agent", item.agent), ("fix", item.fix),
                 *((f"reason {reason}", text) for reason, text in item.reasons.items()),
                 *((f"fix on {surface}", text) for surface, text in item.surfaces.items())]
        for label, text in texts:
            if not text.strip():
                out.append(f"{code}: {label} is empty")
            if "\u2014" in text:
                out.append(f"{code}: {label} has an em dash")
        if item.retry_after is not None and not item.retryable:
            out.append(f"{code}: retry_after without retryable = true")
    return out


# ---- generated outputs ----

def markdown(found: Scan, catalog: dict[str, Entry] | None = None) -> str:
    catalog = entries() if catalog is None else catalog
    lines = [
        "# Error codes",
        "",
        "Generated by `agentos errors --markdown` (`make errors-doc`) from the error classes and",
        "src/agentos/core/error_catalog.toml; CI fails when it is stale. Every error the node returns",
        "carries one of these codes:",
        "",
        "- MCP tool results start with `<code>: <message>`, then a `fix:` line with the next step",
        "  for the agent, and `retry_after: <seconds>` when waiting helps.",
        "- CLI errors print `error: <code>: <message>` and a `fix:` line for you.",
        "- API error bodies are `{\"error\": {\"code\", \"message\", \"fix\"}}`; the panel shows the fix.",
        "",
        "| Code | Meaning | Retry |",
        "|---|---|---|",
    ]
    codes = sorted(catalog)
    for code in codes:
        item = catalog[code]
        lines.append(f"| [`{code}`](#{code}) | {item.meaning} | {item.retry_text()} |")
    for code in codes:
        item = catalog[code]
        lines += ["", f"## {code}", "", item.meaning, "",
                  f"- **Agent:** {item.agent}",
                  f"- **You:** {item.fix}",
                  f"- **Retry:** {item.retry_text()}"]
        for reason, text in sorted(item.reasons.items()):
            lines.append(f"- **Agent, when the reason is `{reason}`:** {text}")
        for surface in SURFACES:
            if surface in item.surfaces:
                lines.append(f"- **You, on {SURFACE_NAMES[surface]}:** {item.surfaces[surface]}")
        raised = found.by_code.get(code, [])
        if raised:
            lines.append(f"- **Raised by:** {', '.join(f'`{n}`' for n in raised)}")
        runs = found.run_codes.get(code, [])
        if runs:
            lines.append(f"- **Runs fail with it in:** {', '.join(f'`{n}`' for n in runs)}")
    return "\n".join(lines) + "\n"

