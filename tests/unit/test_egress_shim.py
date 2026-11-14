"""The egress package's shim: every name
the single module agentos.node.egress had still resolves on the package, to the same object its new
home holds, and each name is defined in one module only, so a patch of its home reaches every reader
in the package."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

from agentos.node import egress

# where each name the single module defined lives now
HOMES: dict[str, tuple[str, ...]] = {
    "classify": (
        "NEVER_SENT", "SCAN_MAX", "_NAME_TOKEN", "_anycase", "_scan_body", "classify", "effects_of",
        "gate_pattern", "never_sent", "rule_hosts", "ungated_sites", "unreviewed_sites",
    ),
    "constants": (
        "ACTION_TIMEOUT_MS", "APPROVED_WINDOW_S", "ASK_FIRST_TELL_S", "DOCUMENT_KINDS", "FLOW_RELOAD_S",
        "HIT_EVERY_S", "LIVE_TYPES", "LOG_EVERY_S", "NOTES_KEPT", "NOTICE_BURST", "NOTICE_EVERY_S",
        "NOTICE_WINDOW_S", "OBSERVE_EVERY_S", "OBSERVE_MAX_MINUTES", "OBSERVE_NAME", "POPUP_WINDOW_S",
        "SAFE_METHODS", "SETTLE_COLD_AGE_S", "SETTLE_COLD_S", "SETTLE_GRACE_S", "SETTLE_MAX_S",
        "SETTLE_QUIET_S", "SHAPE_DEPTH", "SHAPE_DOTTED_OP_KEYS", "SHAPE_HEADERS", "SHAPE_JSON_FIELDS",
        "SHAPE_MAX_FIELDS", "SHAPE_OP_KEYS", "SHAPE_PARSE_MAX", "SITES_TTL_S", "STOP_TIMEOUT_S",
        "TRACE_EVERY_S", "TRACE_MAX_BYTES", "TRACE_NAME", "UNRULED_EVERY_S", "UPLOADS_KEPT",
        "UPLOAD_HASH_MAX", "WEB_SCHEMES", "_DOTTED_NAME_RE", "_MULTIPART_NAME_RE", "_SHAPE_NAME_RE",
    ),
    "guard": (
        "Events", "LaneGuard", "_GUARDS", "guard_for",
    ),
    "helpers": (
        "QUIET_SCHEMES", "SUBMIT_KEYS", "_in_subframe", "_path", "_post_data",
        "_press_mode", "_short", "_tab_before_newline",
    ),
    "page_js": (
        "COVER_JS", "COVER_POINTS", "TARGET_JS", "_RESOLVE_JS",
    ),
    "rules": (
        "ABORTED_KEEP", "EFFECT_KINDS", "EFFECT_RULES", "EffectRule", "IG_GQL", "LINKEDIN_RSC",
        "LOGIN_WINDOW_S", "META_GQL", "REDDIT_GQL", "SENT_KEEP", "UNKNOWN", "_REGISTER_RULE",
        "_RULE_ID_RE",
    ),
    "shapes": (
        "OP_FIELDS", "READ_DOC_IDS", "READ_OPS", "_DOC_ID_KEYS", "_DOC_ID_RE", "_DOC_KEYWORD",
        "_MUTATION_WORD", "_PUT_LIKE", "_QUERY_NAME", "_op_name", "_shape_paths", "body_covered",
        "body_fields", "gql_verdict", "operations", "read_observe", "request_op", "request_shape",
        "unknown_rule", "write_observe",
    ),
    "sites": (
        "_PCT", "allow_entry", "host_of", "in_list", "in_sites", "norm_path", "site_match", "site_scope",
    ),
    "suggested": (
        "IDENTITY_SITES", "PLATFORM_SITES", "SUGGESTED_SITES",
    ),
    "trace": (
        "EgressTrace",
    ),
    "verdicts": (
        "Block", "CommitMeter", "Driver", "FlowCommit", "HUMAN_KINDS", "PublishGate", "Refusal",
        "_body_values", "_ceiling", "grant_lists", "judge", "refusal_message",
    ),
}
# what the single module had in scope from its imports (attribute reach-through: egress.globs,
# egress.FRAME_PREFIX, egress.json)
IMPORTED = (
    "ActionKind", "Any", "AsyncIterator", "Awaitable", "COUNTRY_TOKEN", "Callable", "Effect",
    "EgressRefused", "EngineError", "FRAME_PREFIX", "GOOGLE_COUNTRY_SUFFIXES", "GUARD_PATTERN",
    "InvalidRequest", "Iterable", "Iterator", "Mapping", "Path", "Protocol", "UTC", "asyncio",
    "contextlib", "dataclass", "datetime", "deque", "field", "gate_kind", "globs", "json", "logging", "os",
    "publish", "re", "registrable_domain", "same_site", "split_site", "threading", "time", "unquote",
    "urlsplit", "validate_sites", "weakref",
)
# names tests patch, read at call time through their home module
PATCHED = {"EFFECT_RULES": "rules", "NOTICE_WINDOW_S": "constants", "registrable_domain": "sites",
           "same_site": "sites"}
PACKAGE = Path(egress.__file__).parent


def _defined(path: Path) -> set[str]:
    out: set[str] = set()
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Assign):
            out |= {n.id for t in node.targets for n in ast.walk(t) if isinstance(n, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out.add(node.target.id)
    return out


@pytest.mark.parametrize("module", sorted(HOMES))
def test_each_moved_name_is_the_same_object_on_the_package(module):
    home = importlib.import_module(f"agentos.node.egress.{module}")
    for name in HOMES[module]:
        assert getattr(egress, name) is getattr(home, name), f"{module}.{name}"


def test_what_the_module_imported_still_resolves_on_the_package():
    missing = [name for name in IMPORTED if not hasattr(egress, name)]
    assert missing == []
    assert egress.globs is importlib.import_module("agentos.core.globs")
    assert egress.registrable_domain is egress.sites.registrable_domain
    assert egress.same_site is egress.sites.same_site


def test_every_name_is_defined_in_one_module_only():
    seen: dict[str, list[str]] = {}
    for path in sorted(PACKAGE.glob("*.py")):
        for name in _defined(path) - {"log"}:  # each module names the one logger, agentos.node.egress
            seen.setdefault(name, []).append(path.stem)
    twice = {name: homes for name, homes in seen.items() if len(homes) > 1}
    assert twice == {}
    assert "__init__" not in {home for homes in seen.values() for home in homes}  # the shim defines nothing
    for name, module in PATCHED.items():
        assert seen.get(name, [module]) == [module] and hasattr(getattr(egress, module), name), name


def test_each_module_logs_as_the_package():
    for module in HOMES:
        home = importlib.import_module(f"agentos.node.egress.{module}")
        if hasattr(home, "log"):
            assert home.log is egress.log
