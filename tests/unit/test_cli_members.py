"""`agentos members list|invite|role|remove` against the real panel API (the node of
test_api_members: the real policy, members and passkeys), called with the node token, plus the
terminal QR code the invite prints."""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest
from test_api_members import accept
from test_node_panel import TOKEN, Node
from typer.testing import CliRunner

from agentos import qr
from agentos.cli import app
from agentos.commands import base as cli_base
from agentos.core.ratelimit import RateLimiter

EM_DASH = "—"


@pytest.fixture
def node(tmp_path, monkeypatch):
    n = Node(tmp_path)
    # the CLI runs on this node's home: its one-time presence codes go into the panel.db the app reads
    monkeypatch.setenv("AGENTOS_HOME", str(n.config.home))
    assert CliRunner().invoke(app, ["node", "init"]).exit_code == 0
    n.api_app.state.api.limiter = RateLimiter(per_key=10_000, global_max=10_000)
    client = n.client()

    def send(method, url, *, body, headers):  # the CLI's HTTP call, into the app in-process
        assert headers["Authorization"].startswith("Bearer ")
        # the node token of this test node, and the CLI's own headers (the presence code of a person's call)
        return client.request(method, urlsplit(url).path, json=body,
                              headers={**headers, "Authorization": f"Bearer {TOKEN}"})

    monkeypatch.setattr(cli_base, "_send", send)
    yield n
    n.close()


def run(*args: str, input: str | None = None):
    result = CliRunner().invoke(app, ["members", *args], input=input)
    assert EM_DASH not in result.output
    return result


def code_of(output: str) -> str:
    line = next(x for x in output.splitlines() if "/invite#code=" in x)
    return line.strip().split("#code=")[1]


def test_invite_prints_the_link_once_and_a_qr_code(node):
    r = run("invite", "va", "--role", "approver", "--workspace", "biz-a", "--ttl", "90d", "--max-usd", "50",
            "--qr")
    assert r.exit_code == 0, r.output
    assert "Invite for va: approver in biz-a (pay up to $50), for 90d." in r.output
    assert "works once, for 24 hours" in r.output
    assert "/invite#code=" in r.output
    assert "\x1b[30;47m" in r.output and "▀" in r.output  # black on white, two rows a line
    invite = node.api_app.state.api.members.store.invites()[0]
    assert invite.handle == "va" and invite.member_ttl_s == 90 * 86400
    assert [(x.scope, x.role, x.options) for x in invite.roles] == [("biz-a", "approver", {"max_usd": 50.0})]


def test_invite_without_a_terminal_skips_the_qr_and_json_carries_the_link(node):
    r = run("invite", "ops", "--role", "operator@biz-a", "--role", "viewer@biz-b")
    assert r.exit_code == 0, r.output
    assert "\x1b[" not in r.output
    assert "operator in biz-a, viewer in biz-b" in r.output
    j = run("invite", "client", "--role", "viewer", "-w", "biz-b", "--watch", "--json")
    assert j.exit_code == 0, j.output
    assert '"link"' in j.output and '"watch": true' in j.output


def test_invite_refusals_are_clear(node):
    assert "say where" in run("invite", "va", "--role", "approver").output
    bad = run("invite", "va", "--role", "boss", "-w", "biz-a")
    assert bad.exit_code == 1 and "'boss' is not a role" in bad.output
    assert run("invite", "va", "--role", "viewer", "-w", "biz-a").exit_code == 0
    taken = run("invite", "va", "--role", "viewer", "-w", "biz-a")
    assert taken.exit_code == 1 and "The handle va is taken" in taken.output


def test_list_role_and_remove(node):
    first = run("invite", "va", "--role", "approver", "-w", "biz-a", "--no-qr")
    va = accept(node, code_of(first.output))
    run("invite", "later", "--role", "viewer", "-w", "biz-b", "--no-qr")

    listed = run("list")
    assert listed.exit_code == 0, listed.output
    assert "owner" in listed.output and "approver in biz-a" in listed.output
    assert "Invites not used yet:" in listed.output and "later" in listed.output

    changed = run("role", "va", "--role", "operator", "--role", "approver", "-w", "biz-a", "--watch")
    assert changed.exit_code == 0, changed.output
    assert "operator in biz-a" in changed.output and "approver in biz-a (watches)" in changed.output
    me = va.get("/api/me").json()
    assert sorted(me["can"]["biz-a"]) == ["answer", "approvals", "connect", "decide", "operate", "see", "watch"]

    more = run("role", "va", "--role", "viewer@biz-b")
    assert more.exit_code == 0 and "viewer in biz-b" in more.output and "approver in biz-a" in more.output
    gone = run("role", "va", "--none", "-w", "biz-b")
    assert gone.exit_code == 0 and "biz-b" not in gone.output

    assert run("remove", "va", input="n\n").exit_code == 1  # asked, and refused
    assert va.get("/api/me").status_code == 200
    removed = run("remove", "va", "--yes")
    assert removed.exit_code == 0, removed.output
    assert "Removed va: 1 session ended" in removed.output
    assert va.get("/api/me").status_code == 401  # at the next request
    assert "no member va" in run("remove", "va", "--yes").output


def test_the_owner_cannot_be_removed(node):
    r = run("remove", "owner", "--yes")
    assert r.exit_code == 1 and "owner cannot be removed" in r.output


def test_a_stopped_daemon_is_explained(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTOS_HOME", str(tmp_path / "h"))
    assert CliRunner().invoke(app, ["node", "init"]).exit_code == 0
    import httpx

    def refuse(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(cli_base, "_send", refuse)
    r = run("list")
    assert r.exit_code == 1 and "agentosd is not running" in r.output


# ---- the QR code ----

def test_qr_reed_solomon_and_format_vectors():
    # ISO 18004 / Thonky "HELLO WORLD" 1-M: data codewords and their 10 error correction codewords
    data = [32, 91, 11, 120, 209, 114, 220, 77, 67, 64, 236, 17, 236, 17, 236, 17]
    assert qr.rs_remainder(data, qr.rs_divisor(10)) == [196, 35, 39, 119, 235, 215, 231, 226, 93, 23]
    assert format(qr.format_bits(0), "015b") == "101010000010010"
    assert format(qr.format_bits(5), "015b") == "100000011001110"
    assert format(qr.version_bits(7), "018b") == "000111110010010100"
    assert [qr.data_capacity(v) for v in (1, 5, 7, 10)] == [16, 86, 124, 216]


def test_qr_sizes_and_fixed_patterns():
    link = "http://localhost:7400/invite#code=" + "A" * 43
    m = qr.encode(link)
    assert len(m) == 37  # version 5
    for cx, cy in ((0, 0), (len(m) - 7, 0), (0, len(m) - 7)):  # three finder patterns
        assert all(m[cy][cx + i] for i in range(7)) and m[cy + 3][cx + 3] and not m[cy + 1][cx + 1]
    assert [m[6][x] for x in range(8, len(m) - 8)] == [x % 2 == 0 for x in range(8, len(m) - 8)]  # timing
    assert m[len(m) - 8][8]  # the dark module
    assert len(qr.encode("y" * 213)) == 57  # version 10
    with pytest.raises(qr.QrTooLong):
        qr.encode("z" * 214)


def test_qr_render_is_two_rows_a_line_with_a_quiet_zone():
    text = qr.render("hi", color=False)
    lines = text.splitlines()
    assert len(lines) == (21 + 8 + 1) // 2 and all(len(x) == 29 for x in lines)
    assert lines[0].strip() == ""  # quiet zone
