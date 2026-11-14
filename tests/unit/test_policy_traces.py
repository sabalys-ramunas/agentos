"""Redaction at write time, append-only storage, queries, retention, and the event bus."""

from __future__ import annotations

import sqlite3

import pytest
from test_policy_support import Clock

from agentos.core.errors import InvalidRequest
from agentos.policy.redact import REDACTED, redact, redact_text
from agentos.policy.traces import EventBus, TraceEvent, TraceStore


@pytest.mark.parametrize("text,secret", [
    ("Authorization: Bearer abcdefghijklmnop123", "abcdefghijklmnop123"),
    ("authorization=Basic dXNlcjpwYXNzd29yZA==", "dXNlcjpwYXNzd29yZA=="),
    ("Cookie: sessionid=s3cr3tv4lue; csrftoken=zzz", "s3cr3tv4lue"),
    ('{"Set-Cookie": "sid=abc123secretvalue"}', "abc123secretvalue"),
    ("login with password=hunter2!", "hunter2!"),
    ("https://alice:pa55word@proxy.example:8080/x", "pa55word"),
    ("otpauth://totp/IG:me?secret=JBSWY3DPEHPK3PXP&issuer=IG", "JBSWY3DPEHPK3PXP"),
    ("card 4111 1111 1111 1111 exp 12/29", "4111 1111 1111 1111"),
    ("card 4111-1111-1111-1111", "4111-1111-1111-1111"),
    ("key sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWx", "sk-ant-api03-AbCdEfGhIjKlMnOpQrStUvWx"),
    ("gh ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789", "ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"),
    ("aws AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
    ("jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U",
     "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"),
    ("grant agt1_QUcBAQAAAMB7ImV4cCI6MTgwMDYwNDgwMCwiaWF0IjoxODAw", "agt1_QUcBAQAAAMB7ImV4cCI6MTgwMDYwNDgwMCwiaWF0IjoxODAw"),
    ("random Zq8xK2mP9vL4nR7tW1yB6cD3fG5hJ0sA", "Zq8xK2mP9vL4nR7tW1yB6cD3fG5hJ0sA"),
    ("hex 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08", "9f86d081884c7d659a2feaa0c5"),
    ("-----BEGIN PRIVATE KEY-----\nMC4CAQAwBQYDK2VwBCIEIA\n-----END PRIVATE KEY-----", "MC4CAQAwBQYDK2VwBCIEIA"),
])
def test_text_redaction(text, secret):
    out = redact_text(text)
    assert secret not in out, out


@pytest.mark.parametrize("text", [
    "Post 1 photo with caption to instagram.com as biz-a",
    "lane biz-a/instagram is warm",
    "content sha256:9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "order 12345 shipped on 2026-11-04, 3 items, total 42.50",
    "/Users/someone/.t3/worktrees/agentos/src/agentos/policy",
    "a-very-long-descriptive-kebab-case-identifier",
    "approval ap_k3j4h5g6f7d8s9a0 and trace tr_abcdefgh12345678",
])
def test_benign_text_survives(text):
    assert redact_text(text) == text


def test_structured_redaction():
    data = {
        "password": "hunter2", "apiKey": "x", "client_secret": "y", "Authorization": "Bearer z",
        "headers": {"Cookie": "a=b", "User-Agent": "Firefox"}, "otp_code": 123456, "pin": 1234,
        "max_tokens": 4096, "token_count": 7, "session_ttl_s": 60, "note": "password=hunter3",
        "nested": [{"refresh_token": "r"}, "plain"], "image": b"\x89PNG....", "ok": True, "caption": "hi",
    }
    out = redact(data)
    for k in ("password", "apiKey", "client_secret", "Authorization", "otp_code", "pin"):
        assert out[k] == REDACTED, k
    assert out["headers"] == {"Cookie": REDACTED, "User-Agent": "Firefox"}
    assert out["max_tokens"] == 4096 and out["token_count"] == 7 and out["session_ttl_s"] == 60
    assert out["note"] == "password=[redacted]" and out["nested"][0]["refresh_token"] == REDACTED
    assert out["image"] == "<8 bytes>" and out["ok"] is True and out["caption"] == "hi"


def test_redaction_scrubs_exact_values_and_truncates():
    out = redact_text("x" * 5000 + " opaque-owner-token", extra=["opaque-owner-token"])
    assert "opaque-owner-token" not in out and "[truncated" in out


@pytest.fixture
def store(tmp_path):
    clock = Clock()
    s = TraceStore(tmp_path / "traces.db", clock=clock, scrub=["static-secret-value"])
    return s, clock


def test_write_redacts_and_derives_workspace(store):
    s, _ = store
    ev = s.write(TraceEvent(kind="e", lane="biz-a/insta", summary="Cookie: a=b", args={"password": "p",
                                                                                         "x": "static-secret-value"}))
    got = s.get(ev.id)
    assert got.workspace == "biz-a" and got.args == {"password": REDACTED, "x": REDACTED}
    assert got.summary == "Cookie: [redacted]"


def test_bad_status_is_coerced(store):
    s, _ = store
    assert s.write(TraceEvent(kind="e", status="weird")).status == "error"


def test_append_only(store, tmp_path):
    s, _ = store
    ev = s.write(TraceEvent(kind="e"))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        s.db.execute("UPDATE traces SET status = 'ok' WHERE id = ?", (ev.id,))


def test_query_filters_cursor_and_viewer(store):
    s, clock = store
    for i in range(7):
        clock.advance(1)
        s.write(TraceEvent(kind="tool_end", tool="lane_read" if i % 2 else "lane_post", lane=f"biz-a/l{i}",
                           grant_id=f"gr_{i % 2}", grant_path=f"/gr_root/gr_{i % 2}/", status="ok"))
    page1, nxt = s.query(limit=3)
    page2, nxt2 = s.query(limit=3, before=nxt)
    page3, nxt3 = s.query(limit=3, before=nxt2)
    ids = [e.id for e in page1 + page2 + page3]
    assert len(ids) == 7 == len(set(ids)) and nxt3 is None
    assert [e.lane for e in page1] == ["biz-a/l6", "biz-a/l5", "biz-a/l4"]
    assert all(e.tool == "lane_read" for e in s.query(tool="lane_read")[0])
    assert len(s.query(viewer_path="gr_1")[0]) == 3 and len(s.query(viewer_path="gr_root")[0]) == 7
    assert len(s.query(viewer_path="gr_")[0]) == 0  # no partial id matches
    assert s.query(before=page1[0].id, limit=10)[0][0].id == page1[1].id
    with pytest.raises(InvalidRequest):
        s.query(before="c_garbage!")
    assert s.query(since=clock.t - 2)[0][-1].lane == "biz-a/l4"  # since is inclusive


def test_retention(store):
    s, clock = store
    s.write(TraceEvent(kind="old", at=clock.t - 100 * 86400))
    s.write(TraceEvent(kind="new", at=clock.t))
    assert s.purge(older_than_days=90) == 1
    assert [e.kind for e in s.query()[0]] == ["new"]


def test_event_bus(store):
    s, _ = store
    seen = []
    unsub = s.subscribe(lambda kind, payload: seen.append((kind, payload["kind"])))
    s.subscribe(lambda kind, payload: 1 / 0)  # a broken subscriber never breaks writes
    s.write(TraceEvent(kind="a"))
    unsub()
    s.write(TraceEvent(kind="b"))
    assert seen == [("trace", "a")]


async def test_event_bus_async_subscriber():
    bus = EventBus()
    got = []

    async def sub(kind, payload):
        got.append(kind)

    bus.subscribe(sub)
    bus.publish("approval", {})
    import asyncio

    await asyncio.sleep(0)
    assert got == ["approval"]


def test_calls_a_killed_daemon_cut_off_get_an_end_that_says_so(tmp_path):
    store = TraceStore(tmp_path / "traces.db", clock=Clock(1_000_000.0))
    cut = store.write(TraceEvent(kind="tool_start", tool="browser_act", lane="biz-a/x", effect="draft",
                                 status="started", principal="claude-code", grant_id="g1", args={"ref": "e3"}))
    done = store.write(TraceEvent(kind="tool_start", tool="whoami", status="started"))
    store.write(TraceEvent(kind="tool_end", tool="whoami", status="ok", parent_id=done.id))
    store.close()

    store = TraceStore(tmp_path / "traces.db", clock=Clock(1_000_060.0))  # agentosd starts again
    assert store.end_unfinished() == 1
    [end] = store.query(kind="tool_end", tool="browser_act")[0]
    assert (end.parent_id, end.status, end.lane, end.grant_id) == (cut.id, "error", "biz-a/x", "g1")
    assert end.error == TraceStore.INTERRUPTED and end.args == {"ref": "e3"}
    assert store.end_unfinished() == 0  # once
    store.close()
