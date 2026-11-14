"""The error catalog (agentos.core.catalog): every code has an entry, one code is one meaning,
and the same entry reaches MCP results, the CLI and API bodies."""

from __future__ import annotations

import inspect
import json
import re
from pathlib import Path

import pytest
from mcp.types import CallToolResult, TextContent

from agentos.core import catalog
from agentos.core.catalog import Entry, Scan
from agentos.core.errors import AgentosError, LaneBusy, PolicyDenied

SRC = Path(__file__).resolve().parents[2] / "src" / "agentos"


@pytest.fixture(scope="module")
def scanned() -> Scan:
    return catalog.scan()


def _entry(code: str, **kw) -> Entry:
    return Entry(code=code, meaning=kw.pop("meaning", "m"), agent="a", fix="f", **kw)


# ---- the catalog against the classes ----

def test_every_module_imports_so_no_error_class_is_missed(scanned):
    assert scanned.failed == []


def test_every_error_class_code_has_a_catalog_entry(scanned):
    """Fails when anyone adds an AgentosError subclass with a new code and no entry in
    src/agentos/core/error_catalog.toml (then run `make errors-doc`)."""
    missing = [p for p in catalog.problems(scanned) if "no entry" in p]
    assert missing == [], "\n".join(missing)


def test_no_two_classes_share_a_code_with_different_meanings(scanned):
    """A code belongs to one class; another class may raise it only when the entry lists it in
    shared_by (the same meaning). A subclass that inherits its parent's code is the same error."""
    shared = [p for p in catalog.problems(scanned) if "one code is one meaning" in p or "shared_by" in p]
    assert shared == [], "\n".join(shared)


def test_the_catalog_is_complete_and_has_no_orphans(scanned):
    assert catalog.problems(scanned) == []
    assert len(catalog.entries()) == len(set(scanned.by_code) | set(scanned.run_codes)) >= 110


# ---- run failure codes (run.error.code) ----

def test_every_run_failure_code_has_a_catalog_entry(scanned):
    """Fails when the runner sets a new run.error.code (a `{"code": ...}`, a StepFailure, a
    `*_CODES` map) with no entry in src/agentos/core/error_catalog.toml (then `make errors-doc`)."""
    missing = [code for code in scanned.run_codes if catalog.entry(code) is None]
    assert missing == [], "\n".join(missing)


def test_the_runner_builds_no_run_code_the_catalog_cannot_see():
    """A code made with an f-string matches no entry; build it from a `*_CODES` dict instead
    (SIGNAL_CODES in agentos.runner.executor)."""
    assert catalog.unseen_run_codes() == []


def test_run_codes_are_collected_from_every_way_the_runner_sets_them(scanned):
    runs = scanned.run_codes
    for code, module in (("edit_mismatch", "runner"), ("crash_loop", "runner"),          # {"code": ...}
                         ("grant_expired", "runner"), ("grant_not_yet_valid", "runner"),  # GRANT_CODES
                         ("wrong_account", "executor"), ("wrong_surface", "executor"),    # map and .get default
                         ("connector_contract_broken", "executor"), ("readback_failed", "executor"),
                         ("signal_captcha", "executor"), ("signal_logged_out", "executor"),  # SIGNAL_CODES
                         ("ambiguous_locator", "executor")):                              # code = a if c else b
        assert f"agentos.runner.{module}" in runs.get(code, []), code
    assert "code" not in runs  # `.get("code")` reads a code, it does not set one
    assert all(not m.endswith((".api", ".schedules")) for m in runs.get("not_found", []))  # HTTP bodies


def test_the_run_code_collector_on_a_source():
    found, unseen = catalog.source_run_codes('''
KIND_CODES = {"a": "from_map"}
class E:
    code = "class_attr"
def f(x, e):
    run.error = {"code": "from_dict", "message": "m"}
    body = {"error": {"code": "http_body"}}
    code = "left" if x else KIND_CODES.get(x, "from_default")
    raise StepFailure("from_step", "m")
    raise StepFailure(code=f"built_{x}")
    other = {"code": f"also_{x}"}
    return (run.error or {}).get("code")
''')
    assert found == {"from_map", "from_dict", "left", "from_default", "from_step"}
    assert unseen == [10, 11]


def test_the_run_code_collector_follows_a_table_from_core_signals():
    """runner.executor takes SIGNAL_CODES from agentos.core.signals; its codes are still its own."""
    found, _ = catalog.source_run_codes('''
from agentos.core.signals import RUN_SIGNAL_CODES
from agentos.elsewhere import OTHER
SIGNAL_CODES = RUN_SIGNAL_CODES
OTHER_CODES = OTHER
''')
    assert found == {"signal_captcha", "signal_checkpoint", "signal_logged_out", "signal_blocked"}


def test_the_markdown_says_where_a_run_fails_with_a_code(scanned):
    text = catalog.markdown(scanned)
    section = text.split("## wrong_surface", 1)[1].split("\n## ", 1)[0]
    assert "- **Runs fail with it in:** `agentos.runner.executor`" in section


def test_problems_catches_a_missing_entry_an_orphan_and_a_clash():
    found = Scan(by_code={"new_code": ["pkg.New"], "busy": ["pkg.A", "pkg.B"]}, failed=[("pkg.broken", "boom")])
    lines = catalog.problems(found, {"busy": _entry("busy"), "gone": _entry("gone")})
    assert any(line.startswith("new_code: no entry") for line in lines)
    assert any(line.startswith("busy: raised by pkg.A, pkg.B") for line in lines)
    assert any(line.startswith("gone: in error_catalog.toml but no error class") for line in lines)
    assert any("pkg.broken could not be imported" in line for line in lines)
    # Listing the second class in shared_by says the two mean the same; then it is fine.
    ok = catalog.problems(Scan(by_code={"busy": ["pkg.A", "pkg.B"]}, failed=[]),
                          {"busy": _entry("busy", shared_by=("pkg.B",))})
    assert ok == []


def test_problems_catches_bad_text():
    lines = catalog.problems(Scan(by_code={"x": ["pkg.X"]}, failed=[]),
                             {"x": Entry(code="x", meaning="one \u2014 two", agent=" ", fix="f", retry_after=5)})
    assert "x: meaning has an em dash" in lines and "x: agent is empty" in lines
    assert "x: retry_after without retryable = true" in lines


def test_a_class_raised_with_a_code_picked_at_the_raise_site_lists_its_codes(scanned):
    """A class whose constructor takes the code (agentos.api.errors.ApiError) must say which codes
    it introduces in `codes`, or the catalog cannot see them."""
    from agentos.core.catalog import _subclasses

    unlisted = []
    for cls in _subclasses(AgentosError):
        if not cls.__module__.startswith("agentos.") or "__init__" not in cls.__dict__:
            continue
        if "code" in inspect.signature(cls.__init__).parameters and not getattr(cls, "codes", None):
            unlisted.append(catalog.class_name(cls))
    assert unlisted == []


def test_every_literal_api_error_code_is_in_the_catalog():
    used = set()
    for path in SRC.rglob("*.py"):
        used |= set(re.findall(r'ApiError\(\s*"([a-z_]+)"', path.read_text()))
    assert used and used <= set(catalog.entries()), sorted(used - set(catalog.entries()))


def test_every_entry_is_readable():
    for item in catalog.entries().values():
        for text in (item.meaning, item.agent, item.fix, *item.reasons.values()):
            assert text.strip() and "\u2014" not in text and len(text) < 240, (item.code, text)
        assert item.retry_after is None or (item.retryable and 0 < item.retry_after <= 3600), item.code


def test_the_generated_files_are_current(scanned):
    """CI diffs them too (`make errors-doc` writes both)."""
    root = SRC.parents[1]
    assert (root / "docs" / "ERRORS.md").read_text() == catalog.markdown(scanned)
    panel_json = root / "panel" / "src" / "api" / "errors.json"
    if panel_json.exists():  # a tree without the panel (the public export, for now) has no copy
        assert json.loads(panel_json.read_text()) == catalog.panel_json()


# ---- the three surfaces ----

def test_mcp_lines_give_the_agent_step_and_the_wait():
    assert catalog.mcp_lines("busy", "all slots") == [f"fix: {catalog.entry('busy').agent}", "retry_after: 30"]
    assert catalog.mcp_lines("lane_not_found", "x")[0].startswith("fix: Call lanes_list")
    assert len(catalog.mcp_lines("lane_not_found", "x")) == 1  # no wait to suggest
    assert catalog.mcp_lines("no_such_code", "x") == []


def test_a_denial_reason_picks_its_own_agent_step():
    reasons = catalog.entry("denied").reasons
    assert catalog.mcp_lines("denied", "expired: the grant ended")[0] == f"fix: {reasons['expired']}"
    assert catalog.mcp_lines("denied", "something else")[0] == f"fix: {catalog.entry('denied').agent}"


async def test_an_mcp_error_result_keeps_its_first_line_and_adds_the_catalog_lines():
    from agentos.core.runtime import Runtime
    from agentos.core.tools import ToolSpec
    from agentos.mcp.registry import build_mcp

    async def handler(ctx):
        raise LaneBusy("all 3 warm slots are active")

    server = build_mcp(Runtime(), [lambda rt: [ToolSpec(name="t", handler=handler)]])
    result = await server.call_tool("t", {})
    text = result.content[0].text
    assert result.is_error and text.split("\n") == [
        "busy: all 3 warm slots are active", f"fix: {catalog.entry('busy').agent}", "retry_after: 30"]


def test_the_node_channel_relays_code_and_message_without_the_catalog_lines():
    from agentos.node.channel.invoke import from_mcp_result

    text = "busy: all slots\nfix: wait\nretry_after: 30"
    result = CallToolResult(content=[TextContent(type="text", text=text)], is_error=True)
    assert from_mcp_result(result)["error"] == {"code": "busy", "message": "all slots"}
    assert catalog.without_mcp_lines("invalid: a\nfix: b") == "invalid: a"
    assert catalog.without_mcp_lines("invalid: a line\nthat goes on") == "invalid: a line\nthat goes on"


def test_the_controller_adds_the_lines_back():
    from agentos.controller.mcp import to_result

    out = to_result("t", {"ok": False, "error": {"code": "busy", "message": "all slots"}}, [], "http://c")
    assert out.is_error and out.content[0].text.split("\n")[1:] == [f"fix: {catalog.entry('busy').agent}",
                                                                     "retry_after: 30"]


def test_cli_error_is_one_line_plus_the_fix():
    assert catalog.cli_error("busy", "all slots") == f"error: busy: all slots\n  fix: {catalog.entry('busy').fix}"
    assert catalog.cli_error("no_such_code", "x") == "error: no_such_code: x"


def test_cli_prints_the_fix_for_an_agentos_error(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from agentos.cli import app

    monkeypatch.setenv("AGENTOS_HOME", str(tmp_path))
    out = CliRunner().invoke(app, ["errors", "nope"])
    assert out.exit_code == 1 and "no error code 'nope'" in out.output
    shown = CliRunner().invoke(app, ["errors", "busy"])
    assert shown.exit_code == 0 and shown.output.splitlines()[0].startswith("busy: ")
    assert "retry: yes, after 30 s" in shown.output


def test_api_error_body_has_code_message_and_fix():
    from agentos.api.errors import error_response

    response = error_response(PolicyDenied("out_of_scope: lane biz/x"))
    body = json.loads(response.body)
    assert response.status_code == 403
    assert body == {"error": {"code": "denied", "message": "out_of_scope: lane biz/x",
                              "fix": catalog.entry("denied").fix}}
    assert catalog.error_body("no_such_code", "m") == {"error": {"code": "no_such_code", "message": "m"}}


def test_a_members_fix_names_no_command_token_or_grant_and_the_owners_keeps_them():
    # The fix is for the person a refusal reaches. A member is told in words, never a command, token or grant;
    # the owner (and the CLI) still reads the catalog's fix, command and all.
    words = re.compile(r"`|\bagentos\b|\btokens?\b|\bgrants?\b|node\.toml", re.IGNORECASE)
    for code in catalog.entries():
        fix = catalog.member_fix(code)
        assert fix is None or not words.search(fix), (code, fix)
        assert catalog.error_body(code, "x", member=True)["error"].get("fix") == fix, code
    assert catalog.member_fix("denied") is None
    assert "agentos grants" in catalog.person_fix("denied")
    assert catalog.error_body("denied", "x")["error"]["fix"] == catalog.person_fix("denied")
    assert "fix" not in catalog.error_body("denied", "x", member=True)["error"]
