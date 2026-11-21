"""MCP edge cases: bearer-token variants, argument validation, error mapping, redaction.

Passing tests pin edge cases that already work. `xfail(strict=True)` tests demonstrate a bug:
they fail today, and once the bug is fixed they pass, so the marker must then be removed.
"""

import logging
import re

import pytest
from fakes import FakeInteraction, FakeLaneManager
from mcp import Client
from starlette.testclient import TestClient

from openajar.core.config import NodeConfig, save_proxies
from openajar.core.errors import (
    AgentPaused,
    ApprovalPending,
    EngineError,
    HumanInControl,
    InvalidRequest,
    LaneExists,
    NotFound,
    OpenajarError,
    PolicyDenied,
    ServiceUnavailable,
)
from openajar.core.models import LaneSpec, ProxyConfig
from openajar.mcp.server import (
    Redactor,
    build_http_app,
    build_server,
    error_message,
    normalize_ref,
    normalize_url,
)

TOKEN = "s3cr3t-token-value"
USER = "customer-acme-zone"
PASSWORD = "pw-Zx81-secret"
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}},
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def config_with_proxy(tmp_path, password: str = PASSWORD) -> NodeConfig:
    cfg = NodeConfig(home=tmp_path, mcp_token=TOKEN)
    save_proxies(cfg, {"lt1": ProxyConfig(label="lt1", host="5.6.7.8", port=12323, username=USER,
                                          password=password)})
    return cfg


def text_of(result) -> str:
    return "\n".join(block.text for block in result.content if block.type == "text")


# ---- bearer token and DNS rebinding --------------------------------------------------


@pytest.fixture
def http(tmp_path):
    cfg = NodeConfig(home=tmp_path, mcp_token=TOKEN)
    app = build_http_app(build_server(FakeLaneManager(cfg), FakeInteraction(), cfg), cfg)
    with TestClient(app, base_url="http://localhost:7400") as client:
        yield client


@pytest.mark.parametrize("authorization", [f"bearer {TOKEN}", f"BEARER {TOKEN}", f"Bearer    {TOKEN}  "])
def test_scheme_case_and_extra_spaces_are_accepted(http, authorization):
    response = http.post("/mcp", json=INIT, headers=MCP_HEADERS | {"Authorization": authorization})
    assert response.status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {"Authorization": f"Bearer\t{TOKEN}"},
        {"Authorization": f"Bearer x{TOKEN}"},
        {"Authorization": f"Bearer {TOKEN[:-1]}"},
        {"X-Api-Key": TOKEN},
        {"Authorization": f"Token {TOKEN}"},
    ],
)
def test_other_token_carriers_are_refused(http, headers):
    assert http.post("/mcp", json=INIT, headers=MCP_HEADERS | headers).status_code == 401


def test_token_in_the_query_string_is_refused(http):
    response = http.post(f"/mcp?token={TOKEN}&access_token={TOKEN}", json=INIT, headers=MCP_HEADERS)
    assert response.status_code == 401
    assert TOKEN not in response.text


def test_the_first_authorization_header_decides(http):
    """A wrong first header is not rescued by a correct second one."""
    headers = [*MCP_HEADERS.items(), ("Authorization", "Bearer wrong"), ("Authorization", f"Bearer {TOKEN}")]
    assert http.post("/mcp", json=INIT, headers=headers).status_code == 401


def test_host_header_for_another_name_is_refused_even_with_the_token(http):
    headers = MCP_HEADERS | {"Authorization": f"Bearer {TOKEN}", "Host": "evil.example"}
    assert http.post("/mcp", json=INIT, headers=headers).status_code == 421


def test_unauthenticated_request_for_a_foreign_host_is_refused(http):
    # The trusted-Host check runs first on every route, so a foreign Host never reaches auth.
    assert http.post("/mcp", json=INIT, headers=MCP_HEADERS | {"Host": "evil.example"}).status_code == 421


# ---- error mapping -------------------------------------------------------------------


def all_openajar_errors() -> list[type[OpenajarError]]:
    """Every OpenajarError class once (a class with two error bases, like StudioTimeout, is a
    subclass of both)."""
    found, todo = [], [OpenajarError]
    while todo:
        cls = todo.pop()
        if cls not in found:
            found.append(cls)
            todo.extend(cls.__subclasses__())
    return found


# Codes two classes set on purpose. `frozen`: a call in a frozen scope (openajar.core.errors.Frozen)
# and a grant a freeze revoked (openajar.policy.errors.GrantFrozen) are the same news for the agent,
# stop and tell the user, so both read `frozen` (commit 13a2642a); they differ in their HTTP status.
SHARED_CODES = {"frozen": {"Frozen", "GrantFrozen"}}


def test_every_openajar_error_has_a_distinct_snake_case_code():
    """A class that sets its own code must not reuse another class's code, so clients can tell
    errors apart. A subclass that inherits its parent's code is the same kind of error (for
    example policy's Denied is a PolicyDenied) and is fine."""
    errors = all_openajar_errors()
    own = [cls.code for cls in errors if "code" in cls.__dict__ and cls.code not in SHARED_CODES]
    assert len(own) == len(set(own)), sorted(own)
    for code, names in SHARED_CODES.items():
        assert {cls.__name__ for cls in errors if cls.__dict__.get("code") == code} <= names, code
    assert all(re.fullmatch(r"[a-z_]+", cls.code) for cls in errors), [cls.code for cls in errors]


@pytest.mark.parametrize(
    ("exc", "prefix"),
    [
        (OpenajarError("plain"), "error: plain"),
        (ApprovalPending("needs a human", "ap-1"), "approval_pending: needs a human"),
        (ServiceUnavailable("no vault"), "unavailable: no vault"),
        (PolicyDenied("scope"), "denied: scope"),
        (HumanInControl("a human has the lane"), "human_in_control: a human has the lane"),
        (AgentPaused("paused"), "agent_paused: paused"),
        (NotFound("gone"), "not_found: gone"),
        (LaneExists("x exists"), "lane_exists: x exists"),
    ],
)
def test_each_openajar_error_maps_to_its_code(tmp_path, exc, prefix):
    assert error_message(exc, Redactor(NodeConfig(home=tmp_path))) == prefix


def test_unexpected_exception_keeps_only_its_first_line(tmp_path):
    message = error_message(ValueError("first line\nsecret second line"), Redactor(NodeConfig(home=tmp_path)))
    assert message == "engine_error: ValueError: first line"


def test_exception_without_a_message_still_names_its_type(tmp_path):
    assert error_message(KeyError(), Redactor(NodeConfig(home=tmp_path))) == "engine_error: KeyError"


def test_long_error_messages_are_cut_to_600_characters(tmp_path):
    message = error_message(OpenajarError("x" * 5000), Redactor(NodeConfig(home=tmp_path)))
    assert len(message) == 600 and message.endswith("...")


def test_credentials_in_a_non_http_url_in_an_error_are_masked(tmp_path):
    exc = EngineError("fetch ftp://bob:hunter2@files.example failed")
    message = error_message(exc, Redactor(NodeConfig(home=tmp_path)))
    assert "hunter2" not in message and "files.example" in message


# ---- redaction -----------------------------------------------------------------------


def test_proxy_password_is_masked_wherever_it_appears(tmp_path):
    redactor = Redactor(config_with_proxy(tmp_path))
    out = redactor.text(f"proxy http://{USER}:{PASSWORD}@5.6.7.8:12323 and bare {PASSWORD}")
    assert PASSWORD not in out and USER not in out


def test_password_with_an_at_sign_is_fully_masked(tmp_path):
    redactor = Redactor(config_with_proxy(tmp_path, password="pa@ss"))
    out = redactor.text(f"engine_error: proxy http://{USER}:pa@ss@5.6.7.8:12323 refused")
    assert out == "engine_error: proxy http://***@5.6.7.8:12323 refused"


async def test_debug_logs_never_carry_a_credential(tmp_path, caplog):
    cfg = config_with_proxy(tmp_path)
    manager, interaction = FakeLaneManager(cfg), FakeInteraction()
    await manager.create(LaneSpec(id="personal/reddit"))
    interaction.fail_with = RuntimeError(f"auth failed for {USER} with {PASSWORD}")
    caplog.set_level(logging.DEBUG)
    async with Client(build_server(manager, interaction, cfg), mode="auto") as client:
        result = await client.call_tool("browser_snapshot", {"on": "personal/reddit"})
    assert result.is_error
    assert PASSWORD not in caplog.text and USER not in caplog.text


# ---- argument validation through the real MCP client ---------------------------------


@pytest.fixture
async def lanes(tmp_path):
    cfg = NodeConfig(home=tmp_path, mcp_token=TOKEN, max_warm_lanes=2)
    manager, interaction = FakeLaneManager(cfg), FakeInteraction()
    await manager.create(LaneSpec(id="personal/reddit"))
    return manager, interaction, cfg


async def test_wrongly_typed_arguments_are_errors_not_crashes(lanes):
    manager, interaction, cfg = lanes
    async with Client(build_server(manager, interaction, cfg), mode="auto") as client:
        for args in ({"on": 123, "url": "https://example.com"}, {"on": "personal/reddit", "url": ["x"]}):
            result = await client.call_tool("browser_navigate", args)
            assert result.is_error, args


@pytest.mark.parametrize(
    "url",
    [
        "JAVASCRIPT:alert(1)",
        "  file:///etc/passwd",
        "FiLe:///var/lib/agentos/secrets/proxies.json",
        "data:text/html,<script>alert(1)</script>",
        "view-source:https://example.com",
        "blob:https://example.com/0000",
        "chrome-extension://abc/page.html",
        "about:blank",
        "\x00javascript:alert(1)",
        "java\tscript:alert(1)",
        "http:example.com",
        "https://",
        "http://example.com:99999",
        "ftp://example.com/",
    ],
)
def test_only_http_and_https_reach_the_browser(url):
    with pytest.raises(InvalidRequest):
        normalize_url(url)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTPS://EXAMPLE.COM", "HTTPS://EXAMPLE.COM"),
        ("https://[::1]:8443/x", "https://[::1]:8443/x"),
        ("https://exa-mple.com/%E2%9C%93?q=1#top", "https://exa-mple.com/%E2%9C%93?q=1#top"),
        ("localhost:8080", "https://localhost:8080"),
        ("reddit.com/r/x?a=b", "https://reddit.com/r/x?a=b"),
    ],
)
def test_web_urls_pass_as_written(raw, expected):
    assert normalize_url(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "\n\t"])
def test_empty_urls_are_invalid(raw):
    with pytest.raises(InvalidRequest, match="empty"):
        normalize_url(raw)


def test_urls_with_embedded_credentials_are_refused():
    with pytest.raises(InvalidRequest):
        normalize_url("https://agent:hunter2@example.com/")


def test_malformed_ipv6_url_is_invalid_not_an_engine_error():
    with pytest.raises(InvalidRequest):
        normalize_url("http://[::1")


def test_nfkc_unsafe_host_is_invalid_not_an_engine_error():
    with pytest.raises(InvalidRequest):
        normalize_url("https://evil＃.example.com")


def test_absurdly_long_urls_are_refused():
    with pytest.raises(InvalidRequest):
        normalize_url("https://example.com/" + "x" * 100_000)


@pytest.mark.parametrize("raw", ["e12", " e12 ", "ref=e12", "[ref=e12]", "[e12]", "ref=e12\n"])
def test_refs_in_every_copied_form_work(raw):
    assert normalize_ref(raw) == "e12"


@pytest.mark.parametrize("raw", ["", "   ", "[]", "[ref=]"])
def test_empty_refs_are_invalid(raw):
    with pytest.raises(InvalidRequest, match="ref is empty"):
        normalize_ref(raw)


@pytest.mark.parametrize("raw", ['e1"]', "e1 or 1=1", "e" + "9" * 500])
def test_refs_must_look_like_snapshot_refs(raw):
    with pytest.raises(InvalidRequest):
        normalize_ref(raw)


async def test_huge_text_is_refused_before_it_holds_a_lane(lanes):
    manager, interaction, cfg = lanes
    args = {"on": "personal/reddit", "ref": "e1", "action": "type", "text": "a" * 1_000_000}
    async with Client(build_server(manager, interaction, cfg), mode="auto") as client:
        result = await client.call_tool("browser_act", args)
    assert result.is_error and text_of(result).startswith("invalid:")
    assert [c for c in manager.calls if c[0] == "using"] == []


@pytest.mark.parametrize("url", ["https://agent:hunter2@example.com/", "http://[::1:hunter2",
                                 "https://example.com/a?token=hunter2 x", "https://example.com:99999/?k=hunter2"])
def test_url_errors_never_quote_the_url(url):
    with pytest.raises(InvalidRequest) as err:
        normalize_url(url)
    assert "hunter2" not in str(err.value)


@pytest.mark.parametrize("url", ["https://example.com/a\nb", "https://exa mple.com/", "https://example.com/\x00"])
def test_urls_with_control_characters_or_spaces_are_refused(url):
    with pytest.raises(InvalidRequest):
        normalize_url(url)


@pytest.mark.parametrize("raw", ["f6e192", "[ref=f1e2]", "e1"])
def test_frame_refs_are_accepted(raw):
    assert normalize_ref(raw) == raw.strip("[]").removeprefix("ref=")
