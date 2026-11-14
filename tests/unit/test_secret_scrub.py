"""Vault values out of page text: the matcher (what it catches and what it does not), its cache,
and the sinks it is wired into (tool results and errors, traces, run proof)."""

from __future__ import annotations

import base64
from urllib.parse import quote, quote_plus

import pytest
from fakes import FakeInteraction, FakeLaneManager
from mcp import Client
from test_vault_support import PASSWORD, SEED, USERNAME, make_vault, private_home

from agentos.core import logs
from agentos.core.config import NodeConfig
from agentos.core.errors import EngineError
from agentos.core.models import LaneSpec, Signal, SnapshotResult
from agentos.core.runtime import Runtime
from agentos.mcp.registry import build_mcp
from agentos.node.tools import INSTRUCTIONS, tool_specs
from agentos.policy.scrub import REDACTED, Matcher, forms, runtime_scrub
from agentos.policy.traces import TraceEvent, TraceStore
from agentos.runner.util import Scrubber
from agentos.vault.errors import VaultBusy
from agentos.vault.secret import Secret
from agentos.vault.service import VaultService

LANE = "biz-a/instagram"
OTHER_LANE = "biz-b/reddit"
OTHER_PW = "reddit-pw-9931"


@pytest.fixture
async def vault(tmp_path):
    v = make_vault(private_home(tmp_path))
    await v.add_entry(name="ig", kind="password", domain="instagram.com", lanes=[LANE],
                      username=USERNAME, secret=PASSWORD)
    await v.add_entry(name="rd", kind="password", domain="reddit.com", lanes=[OTHER_LANE], secret=OTHER_PW)
    return v


# ---- the matcher ----

def test_a_value_is_caught_as_typed_in_any_case_and_percent_encoded():
    m = Matcher([PASSWORD])
    assert m.sub(f"value: {PASSWORD}") == f"value: {REDACTED}"
    assert m.sub(f"shouted {PASSWORD.upper()}!") == f"shouted {REDACTED}!"
    for encoded in (quote(PASSWORD, safe=""), quote_plus(PASSWORD), quote(PASSWORD, safe="!'()*-._~")):
        assert m.sub(f"https://x.example/cb?p={encoded}&n=1") == f"https://x.example/cb?p={REDACTED}&n=1"
    assert m.sub(f"/cb?p={quote(PASSWORD, safe='').lower()}") == f"/cb?p={REDACTED}"  # lower-case hex too


def test_the_longer_value_wins_and_text_without_a_value_is_returned_as_is():
    m = Matcher(["abcd", "abcdefgh"])
    assert m.sub("x abcdefgh y abcd") == f"x {REDACTED} y {REDACTED}"
    text = "nothing secret here"
    assert m.sub(text) is text


def test_what_is_not_caught_a_value_split_across_lines_or_transformed():
    """Documented limits (agentos.policy.scrub): split values and further encodings stay."""
    m = Matcher([PASSWORD])
    half = len(PASSWORD) // 2
    split = f"- paragraph: {PASSWORD[:half]}\n- paragraph: {PASSWORD[half:]}"
    assert m.sub(split) == split
    b64 = base64.b64encode(PASSWORD.encode()).decode()
    assert m.sub(b64) == b64


def test_forms_are_the_value_and_its_url_encodings():
    assert forms("a b&c") == {"a b&c", "a%20b%26c", "a+b%26c"}
    assert forms("plain") == {"plain"}


def test_snapshot_ref_tokens_are_never_altered():
    """An agent acts by its refs: a code or a short password that happens to be a ref's digits
    leaves the ref whole (it used to become [ref=e[redacted]])."""
    for value in ("1234", "12345678"):
        m = Matcher([value])
        for text in (f'- button "Go" [ref=e{value}]', f'- link [ref=f2e{value}] [cursor=pointer]',
                     f"no element matches aria-ref=e{value}"):
            assert m.sub(text) == text
        assert m.sub(f"[ref=e{value}]: code {value}") == f"[ref=e{value}]: code {REDACTED}"


def test_short_and_all_digit_values_are_caught_only_between_boundaries():
    m = Matcher(["test", "1234", "482913"])
    assert m.sub("latest tests: test") == f"latest tests: {REDACTED}"
    assert m.sub("call 912345 or 12345, pin 1234.") == f"call 912345 or 12345, pin {REDACTED}."
    assert m.sub("order 4829130 shipped; code 482913") == f"order 4829130 shipped; code {REDACTED}"
    assert m.sub("TEST_value") == f"{REDACTED}_value"  # an underscore is a boundary, as in a key name


def test_a_long_value_is_still_caught_inside_other_text():
    m = Matcher([PASSWORD, "sk-live-abcdef"])
    assert m.sub(f"x{PASSWORD}y") == f"x{REDACTED}y"
    assert m.sub("key=prefix_sk-live-abcdefZZ") == f"key=prefix_{REDACTED}ZZ"


# ---- the vault service ----

async def test_scrub_covers_usernames_and_other_lanes_secrets(vault):
    text = f"logged in as {USERNAME.upper()}; reused {OTHER_PW}; link /u?email={quote(USERNAME, safe='')}"
    out = vault.scrub(text)
    assert USERNAME.lower() not in out.lower() and OTHER_PW not in out and "%40" not in out
    assert out.count(REDACTED) == 3
    assert OTHER_PW in vault.scrub(text, LANE)  # a lane's own scrub still covers that lane's entries


async def test_short_usernames_are_left_alone(vault):
    await vault.add_entry(name="short", kind="password", domain="x.com", lanes=[LANE], username="ada",
                          secret="a-long-enough-pw")
    assert vault.scrub("ada wrote a post") == "ada wrote a post"


async def test_the_matcher_is_built_once_and_rebuilt_when_the_vault_changes(vault, monkeypatch):
    opened = []
    real = vault.store.open_all
    monkeypatch.setattr(vault.store, "open_all", lambda *a, **k: opened.append(1) or real(*a, **k))
    vault.scrub(f"one {PASSWORD}")
    vault.scrub(f"two {PASSWORD}")
    assert len(opened) == 1
    await vault.add_entry(name="new", kind="api_key", domain="api.example.com", lanes=[LANE], secret="sk-new-0042")
    assert vault.scrub("key sk-new-0042") == f"key {REDACTED}"
    assert len(opened) == 2


async def test_a_value_added_by_another_process_is_scrubbed(vault):
    vault.scrub("warm the cache")
    cli = VaultService(vault.home, socket_path=vault.home / "run" / "cli.sock")  # the CLI, on the same file
    await cli.add_entry(name="cli", kind="api_key", domain="api.example.com", lanes=[LANE], secret="sk-from-cli-77")
    assert vault.scrub("sk-from-cli-77") == REDACTED


async def test_a_sealed_vault_forgets_its_values(vault):
    assert vault.scrub(PASSWORD) == REDACTED
    vault.seal()
    assert vault.scrub(PASSWORD) == PASSWORD  # nothing to match with: the values are gone from memory
    assert not any(matcher for _, matcher in vault._scrub_cache.values())


async def test_a_failed_read_is_never_cached_as_an_empty_matcher(vault, monkeypatch):
    """One VaultBusy used to cache an empty matcher until the next write: every password passed."""
    real = vault.store.open_all
    fails = [1]

    def flaky(*a, **k):
        if fails:
            fails.pop()
            raise VaultBusy("the vault is busy")
        return real(*a, **k)

    monkeypatch.setattr(vault.store, "open_all", flaky)
    assert vault.scrub(PASSWORD) == REDACTED  # tried once more
    fails[:] = [1, 1]
    vault._scrub_gen += 1  # as a write would
    with pytest.raises(VaultBusy):
        vault.scrub(PASSWORD)
    assert runtime_scrub(Runtime(vault=vault))(PASSWORD) != PASSWORD  # withheld, not passed
    assert vault.scrub(PASSWORD) == REDACTED  # the vault reads again: nothing empty was kept


async def test_a_matcher_built_while_the_vault_changed_is_not_cached(vault, monkeypatch):
    real = vault.store.open_all

    def racing(*a, **k):
        vault._scrub_gen += 1  # a write through the service lands while the matcher is built
        return real(*a, **k)

    monkeypatch.setattr(vault.store, "open_all", racing)
    vault.scrub("warm")
    assert None not in vault._scrub_cache


async def test_a_totp_seed_is_scrubbed_as_a_setup_page_shows_it(vault):
    await vault.add_entry(name="2fa", kind="password", domain="x.com", lanes=[LANE], username="someone-else",
                          secret="another-long-pw", totp=SEED)
    grouped = " ".join(SEED[i:i + 4] for i in range(0, len(SEED), 4))
    for shown in (SEED, SEED.lower(), grouped, grouped.lower(), grouped.replace(" ", "-"),
                  f"otpauth://totp/x:me?secret={SEED}&issuer=x"):
        assert SEED.lower() not in vault.scrub(f"Your key: {shown}").lower().replace(" ", "").replace("-", "")


async def test_recent_codes_in_page_text_are_those_of_the_lane_read(vault):
    vault._remember_code(LANE, Secret("482913"))
    text = "order 482913 confirmed"
    assert vault.scrub(text) == f"order {REDACTED} confirmed"  # logs and traces: every lane's codes
    assert vault.scrub(text, codes_lane=OTHER_LANE) == text
    assert vault.scrub(text, codes_lane=LANE) == f"order {REDACTED} confirmed"
    page = runtime_scrub(Runtime(vault=vault), lane_codes=True)
    with logs.log_context(lane=OTHER_LANE):
        assert page(text) == text
    with logs.log_context(lane=LANE):
        assert page(text) == f"order {REDACTED} confirmed"
    assert page(text) == f"order {REDACTED} confirmed"  # no lane: every lane's


def test_runtime_scrub_follows_the_vault_on_the_runtime_and_fails_closed():
    rt = Runtime()
    scrub = runtime_scrub(rt)
    assert scrub("text") == "text"  # no vault yet

    class Broken:
        def scrub(self, text):
            raise RuntimeError("boom")

    rt.register("vault", Broken())
    assert scrub("text") == "[withheld: the vault scrub failed]"


# ---- the sinks ----

def snapshot_with(text: str) -> SnapshotResult:
    return SnapshotResult(url="https://www.instagram.com/accounts/edit/", title=f"Edit {USERNAME}", text=text,
                          ref_count=1, signal=Signal.OK)


class PageInteraction(FakeInteraction):
    def __init__(self, text: str):
        super().__init__()
        self.text = text

    async def snapshot(self, page):
        return snapshot_with(self.text)


@pytest.fixture
async def lanes():
    m = FakeLaneManager(NodeConfig(name="test-node"))
    await m.create(LaneSpec(id=LANE))
    return m


def body(result) -> str:
    return "\n".join(block.text for block in result.content if block.type == "text")


async def test_a_snapshot_and_a_delta_reach_the_agent_without_vault_values(vault, lanes):
    page = (f'- textbox "Email" [ref=e1]: {USERNAME}\n'
            f'- link "Reset" [ref=e2]:\n  - /url: /reset?u={quote(USERNAME, safe="")}&t={quote_plus(PASSWORD)}\n'
            f"- paragraph: your old password was {PASSWORD.lower()} and {OTHER_PW}")
    inter = PageInteraction(page)
    runtime = Runtime(config=lanes.config, lanes=lanes, interaction=inter, vault=vault)
    async with Client(build_mcp(runtime, [tool_specs], instructions=INSTRUCTIONS)) as c:
        full = body(await c.call_tool("browser_snapshot", {"on": LANE}))
        inter.text = page + f"\n- paragraph: new line with {PASSWORD}"
        delta = body(await c.call_tool("browser_act", {"on": LANE, "ref": "e1", "action": "click",
                                                       "snapshot": "delta"}))
    for text in (full, delta):
        low = text.lower()
        assert PASSWORD.lower() not in low and USERNAME.lower() not in low and OTHER_PW not in text
        assert quote(USERNAME, safe="").lower() not in low
    assert "new line with [redacted]" in delta
    assert f"title: Edit {REDACTED}" in full


async def test_an_engine_error_that_quotes_the_page_is_scrubbed(vault, lanes):
    inter = FakeInteraction()
    inter.fail_with = EngineError(f"snapshot failed: text {PASSWORD} not found")
    runtime = Runtime(config=lanes.config, lanes=lanes, interaction=inter, vault=vault)
    async with Client(build_mcp(runtime, [tool_specs], instructions=INSTRUCTIONS)) as c:
        result = await c.call_tool("browser_snapshot", {"on": LANE})
    assert result.is_error and PASSWORD not in body(result) and REDACTED in body(result)


def test_a_trace_is_scrubbed_before_it_is_written(vault, tmp_path):
    store = TraceStore(tmp_path / "traces.db")
    store.value_scrub = runtime_scrub(Runtime(vault=vault))
    ev = store.write(TraceEvent(kind="browser.act", summary=f"typed {USERNAME}", error=f"page said {PASSWORD}",
                                args={"url": f"https://x.example/?p={quote_plus(PASSWORD)}", "n": 3}))
    store.close()
    assert USERNAME not in (ev.summary or "") and PASSWORD not in (ev.error or "")
    assert ev.args["n"] == 3 and quote_plus(PASSWORD) not in ev.args["url"]


def test_run_proof_is_scrubbed_with_and_without_hidden_params(vault):
    scrub = runtime_scrub(Runtime(vault=vault))
    assert Scrubber(extra=scrub)({"readback": f"posted as {USERNAME}", "count": 2}) == {
        "readback": f"posted as {REDACTED}", "count": 2}
    hidden = Scrubber({"caption": "hello world"}, extra=scrub)
    assert hidden(f"hello world by {USERNAME}") == f"${{caption}} by {REDACTED}"
    assert not Scrubber()  # nothing to hide: the executor skips the work
