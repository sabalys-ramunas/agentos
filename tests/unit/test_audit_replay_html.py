"""Replay for agents: the approval behind each step, the replay as one self-contained HTML file, and
the opt-in screenshot kept after an agent's commit (agentos.audit.replay_page, agentos.audit.captures)."""

from __future__ import annotations

import base64
import hashlib
import re
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_policy_support import PRESENT, T0, Clock, make_service
from typer.testing import CliRunner

from agentos.audit import captures
from agentos.audit.service import AuditService
from agentos.core.errors import ApprovalPending
from agentos.core.models import Effect
from agentos.policy.routing import PolicyConfig, RoutingTable
from agentos.policy.traces import TraceEvent, TraceStore

DAY = "2027-01-15"  # T0 is 2027-01-15T08:00Z
LANE = "biz-a/instagram"
HOSTILE = '<img src=x onerror=alert(1)><script>alert(2)</script><a href="javascript:alert(3)">x</a>'


def png(width: int = 2, seed: int = 0) -> bytes:
    """A real (tiny) PNG."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        return len(data).to_bytes(4, "big") + tag + data + zlib.crc32(tag + data).to_bytes(4, "big")
    raw = b"".join(b"\x00" + bytes([seed % 256, 0, 0]) * width for _ in range(2))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", width.to_bytes(4, "big") + (2).to_bytes(4, "big") + b"\x08\x02\x00\x00\x00")
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


# ---------------------------------------------------------------- the approval behind a step

@pytest.fixture
def policy(tmp_path):
    clock = Clock(T0)
    # every commit asks a person (Ask me first everywhere)
    svc, _ = make_service(tmp_path, clock=clock, config=PolicyConfig(routing=RoutingTable(ask_first_default=True)))
    yield svc, clock, tmp_path
    svc.close()


def _start(svc, grant, tool: str = "lane_post", args: dict | None = None) -> str:
    return svc.traces.write(TraceEvent(kind="tool_start", tool=tool, lane=LANE, effect="draft", status="started",
                                       principal=grant.subject.id, principal_kind="agent", grant_id=grant.id,
                                       grant_path=grant.path, args=args or {"on": LANE})).id


def _end(svc, grant, start: str, tool: str = "lane_post", **fields) -> None:
    svc.traces.write(TraceEvent(kind="tool_end", tool=tool, lane=LANE, effect="commit", status="ok",
                                principal=grant.subject.id, principal_kind="agent", grant_id=grant.id,
                                grant_path=grant.path, parent_id=start, **fields))


async def _approved_call(svc, tok) -> tuple[str, str]:
    """An agent's commit that waits for the owner, who approves it with a passkey; the retried call
    goes on. Returns (the retried call's start id, the approval id)."""
    grant = svc.resolve(tok)[1]
    ctx = svc.context(grant, tool="lane_post", lane_id=LANE)
    ctx.start_id = _start(svc, grant)
    with pytest.raises(ApprovalPending) as e:
        await ctx.authorize(Effect.COMMIT, lane_id=LANE, summary="Post a caption to instagram.com",
                            content={"caption": "Fresh beans today"})
    _end(svc, grant, ctx.start_id)
    svc.approvals.decide(e.value.approval_id, "approve", by="owner", via="passkey", in_person=True, proof=PRESENT)
    ctx = svc.context(grant, tool="lane_post", lane_id=LANE)
    ctx.start_id = _start(svc, grant)
    d = await ctx.authorize(Effect.COMMIT, lane_id=LANE, summary="Post a caption to instagram.com",
                            content={"caption": "Fresh beans today"})
    assert d.route == "approved" and d.via == e.value.approval_id
    _end(svc, grant, ctx.start_id, summary="Posted")
    return ctx.start_id, e.value.approval_id


async def test_the_approval_shows_on_the_step_and_the_chain_still_verifies(policy):
    svc, clock, home = policy
    _, tok = svc.issue(subject={"id": "claude-code"}, workspaces=["biz-a"], ceiling="commit")
    start, ap_id = await _approved_call(svc, tok)
    row = next(r for r in svc.traces.children(start) if r["kind"] == "authorize")
    assert '"route": "approved"' in row["args"] and ap_id in row["args"]
    assert row["summary"] == f"approved via {ap_id}"  # the old text stays for every reader of it
    audit = AuditService(svc.traces, policy=svc, home=home, clock=clock)
    step = next(s for s in audit.replay(LANE, DAY, tz="UTC")["steps"] if s["id"] == start)
    [a] = step["approvals"]
    assert a["found"] and a["route"] == "approved" and a["id"] == ap_id
    assert (a["by"], a["how"], a["level"]) == ("owner", "passkey", "present")
    assert a["summary"] == "Post a caption to instagram.com" and "Fresh beans today" in a["preview"]
    shared = next(s for s in audit.replay(LANE, DAY, tz="UTC", shared=True)["steps"] if s["id"] == start)
    assert shared["approvals"][0]["id"] is None and shared["approvals"][0]["by"] == "owner"
    # a share says that a person said yes and how, not the card they read or how they proved it
    assert not {"preview", "method", "asked_at", "edited", "note"} & set(shared["approvals"][0])
    svc.traces.checkpoint("test")
    assert svc.traces.verify().status == "ok"


async def test_an_old_row_with_only_the_summary_text_still_names_its_approval(policy):
    svc, clock, home = policy
    _, tok = svc.issue(subject={"id": "claude-code"}, workspaces=["biz-a"], ceiling="commit")
    _, ap_id = await _approved_call(svc, tok)
    grant = svc.resolve(tok)[1]
    old = _start(svc, grant)
    # as nodes wrote it before route and via: the summary only, no args
    svc.traces.write(TraceEvent(kind="authorize", tool="lane_post", lane=LANE, effect="commit", status="ok",
                                summary=f"approved via {ap_id}", grant_id=grant.id, grant_path=grant.path,
                                parent_id=old))
    _end(svc, grant, old)
    audit = AuditService(svc.traces, policy=svc, home=home, clock=clock)
    step = next(s for s in audit.replay(LANE, DAY, tz="UTC")["steps"] if s["id"] == old)
    assert step["approvals"][0]["by"] == "owner" and step["approvals"][0]["how"] == "passkey"
    svc.traces.checkpoint("test")
    assert svc.traces.verify().status == "ok"


# ---------------------------------------------------------------- the HTML file

class FakeRunner:
    def __init__(self, runs: dict):
        self.runs = runs

    def get(self, run_id):
        return self.runs[run_id]

    def describe(self, run):
        return run


@pytest.fixture
def node(tmp_path):
    clock = Clock(T0)
    store = TraceStore(tmp_path / "traces.db", clock=clock, scrub=["vault-secret-hunter22"])
    shot = tmp_path / "runner" / "shots" / "run_1" / "final.png"
    shot.parent.mkdir(parents=True)
    shot.write_bytes(png(seed=7))
    runner = FakeRunner({"run_1": {"id": "run_1", "flow": "instagram-post", "status": "succeeded", "lane": LANE,
                                   "proof": {"url": "https://www.instagram.com/p/abc/", "readbacks": [],
                                             "screenshot": "runner/shots/run_1/final.png"}}})
    svc = AuditService(store, runner=runner, home=tmp_path, clock=clock, node="node-1")
    start = store.write(TraceEvent(kind="tool_start", tool="browser_navigate", lane=LANE, status="started",
                                   principal="claude-code", principal_kind="agent", grant_id="gr_secretgrant01",
                                   grant_path="/gr_secretgrant01/",
                                   args={"on": LANE, "url": "javascript:alert(document.cookie)",
                                         "password": "vault-secret-hunter22",
                                         "token": "agt_abcdefghijklmnopqrstuvwxyz0123456789"}))
    store.write(TraceEvent(kind="authorize", tool="browser_navigate", lane=LANE, status="ok", parent_id=start.id,
                           summary="auto", args={"route": "auto", "via": None,
                                                 "after_untrusted": "a web page at evil.example"}))
    store.write(TraceEvent(kind="tool_end", tool="browser_navigate", lane=LANE, effect="read", status="ok",
                           principal="claude-code", principal_kind="agent", grant_id="gr_secretgrant01",
                           grant_path="/gr_secretgrant01/", parent_id=start.id, summary=HOSTILE,
                           error="note: vault-secret-hunter22 was typed"))
    clock.advance(30)
    run = store.write(TraceEvent(kind="tool_start", tool="flow_run", lane=LANE, status="started", run_id="run_1",
                                 principal="claude-code", principal_kind="agent", args={"flow": "instagram-post"}))
    store.write(TraceEvent(kind="tool_end", tool="flow_run", lane=LANE, effect="commit", status="ok", run_id="run_1",
                           parent_id=run.id, summary="Posted to instagram.com"))
    yield svc, store, tmp_path
    store.close()


def _attrs(page: str) -> list[str]:
    return re.findall(r'\b(?:src|href|action|srcset|poster)\s*=\s*"([^"]*)"', page, flags=re.IGNORECASE)


def test_the_page_is_self_contained_and_escapes_everything(node):
    svc, _store, _ = node
    made = svc.replay_page(lane=LANE, day=DAY, tz="UTC")
    page = made["html"]
    assert made["steps"] == 2
    assert "<script" not in page.lower() and "<a " not in page.lower() and "<img src=x" not in page
    assert '<meta http-equiv="Content-Security-Policy" content="default-src &#x27;none&#x27;; img-src data:; ' \
           'style-src &#x27;unsafe-inline&#x27;">' in page
    assert all(v.startswith("data:image/") for v in _attrs(page))  # nothing fetched, nothing linked
    assert "&lt;img src=x onerror=alert(1)&gt;" in page and "&lt;script&gt;" in page
    assert "javascript:alert(document.cookie)" in page  # shown as text only
    assert not re.search(r"(href|src)\s*=\s*\"?javascript:", page, flags=re.IGNORECASE)
    assert "gr_secretgrant01" not in page and "agt_abcdefghijklmnop" not in page
    assert "vault-secret-hunter22" not in page
    assert "After untrusted text" in page and "a web page at evil.example" in page
    assert "—" not in page


def test_screenshots_are_left_out_unless_asked_for(node):
    svc, *_ = node
    page = svc.replay_page(lane=LANE, day=DAY, tz="UTC")["html"]
    assert "data:image" not in page and "left out of this file" in page
    made = svc.replay_page(lane=LANE, day=DAY, tz="UTC", screenshots=True)
    assert made["images"] == 1
    assert "data:image/png;base64," + base64.b64encode(png(seed=7)).decode() in made["html"]
    assert "cannot be checked against the log" in made["html"]  # a run's proof has no hash in the log


def test_one_trace_as_a_page(node):
    svc, store, _ = node
    row = next(r for r in store.chain_rows() if r["kind"] == "tool_start" and r["tool"] == "flow_run")
    made = svc.replay_page(trace=row["id"], tz="UTC")
    assert made["steps"] == 1 and "Ran the saved flow instagram-post" in made["html"]


def test_cli_writes_the_page_and_warns(node, tmp_path, monkeypatch):
    from agentos.audit.cli import app

    _svc, store, home = node
    store.close()
    monkeypatch.setenv("AGENTOS_HOME", str(home))
    out = tmp_path / "out" / "replay.html"
    r = CliRunner().invoke(app, ["replay", "--lane", LANE, "--day", DAY, "--tz", "UTC", "--html", str(out)])
    assert r.exit_code == 0, r.output
    assert "anyone who gets it can read it" in r.output and "wrote" in r.output and "—" not in r.output
    assert out.read_text().startswith("<!doctype html>") and oct(out.stat().st_mode & 0o777) == "0o600"


def test_typed_text_arguments_and_queries_stay_out_unless_asked_for(tmp_path, monkeypatch):
    """A file anyone can read and nobody can take back: only the vault's values were scrubbed when
    written, so a code the agent typed, a DM, an OAuth code in an address stay out by default."""
    from agentos.audit.cli import app

    clock = Clock(T0)
    store = TraceStore(tmp_path / "traces.db", clock=clock)
    nav = store.write(TraceEvent(kind="tool_start", tool="browser_navigate", lane=LANE, status="started",
                                 principal="claude-code", principal_kind="agent",
                                 args={"on": LANE, "url": "https://example.com/cb?code=oauthcode77&state=s1#frag9"}))
    store.write(TraceEvent(kind="tool_end", tool="browser_navigate", lane=LANE, effect="read", status="ok",
                           parent_id=nav.id, summary="opened https://example.com/cb?code=oauthcode77"))
    store.write(TraceEvent(kind="link_click", tool="browser_navigate", lane=LANE, status="ok", parent_id=nav.id,
                           summary="click link to https://example.com/next?thread=dm4471 on biz-a/instagram"))
    act = store.write(TraceEvent(kind="tool_start", tool="browser_act", lane=LANE, status="started",
                                 principal="claude-code", principal_kind="agent",
                                 args={"on": LANE, "ref": "e7", "action": "type", "text": "meet at noon, Ana"}))
    store.write(TraceEvent(kind="tool_end", tool="browser_act", lane=LANE, effect="draft", status="ok",
                           parent_id=act.id))
    svc = AuditService(store, home=tmp_path, clock=clock)
    page = svc.replay_page(lane=LANE, day=DAY, tz="UTC")["html"]
    for secret in ("oauthcode77", "state=s1", "frag9", "dm4471", "noon, Ana"):
        assert secret not in page, secret
    assert "Typed 17 characters into element e7" in page
    assert "Opened https://example.com/cb?[left out]" in page
    assert "Arguments (names only, their values are left out): on, ref, action, text" in page
    assert "<pre>" not in page and "—" not in page
    full = svc.replay_page(lane=LANE, day=DAY, tz="UTC", include_args=True)["html"]
    for secret in ("oauthcode77", "frag9", "dm4471", "meet at noon, Ana"):
        assert secret in full, secret
    assert "Arguments (redacted when they were written)" in full
    store.close()
    monkeypatch.setenv("AGENTOS_HOME", str(tmp_path))
    out = tmp_path / "r.html"
    r = CliRunner().invoke(app, ["replay", "--lane", LANE, "--day", DAY, "--tz", "UTC", "--html", str(out)])
    assert r.exit_code == 0 and "noon, Ana" not in out.read_text() and "arguments left out" in r.output
    r = CliRunner().invoke(app, ["replay", "--lane", LANE, "--day", DAY, "--tz", "UTC", "--html", str(out),
                                 "--include-args"])
    assert r.exit_code == 0 and "meet at noon, Ana" in out.read_text() and "arguments included" in r.output
    assert CliRunner().invoke(app, ["replay", "--lane", LANE, "--day", DAY, "--include-args"]).exit_code == 1


def test_an_approval_from_telegram_reads_as_the_owner_from_telegram():
    from zoneinfo import ZoneInfo

    from agentos.audit import replay_page

    a = {"found": True, "route": "approved", "by": "telegram:owner", "how": "telegram", "level": "chat",
         "at": "2027-01-15T08:00:00+00:00", "summary": "Post a caption"}
    html = replay_page._approval(a, ZoneInfo("UTC"))
    assert "Approved by the owner</b> from Telegram at 2027-01-15 08:00:00." in html
    assert "telegram:owner" not in html and "through telegram" not in html


def test_screenshots_past_the_files_budget_are_left_out(node, monkeypatch):
    from agentos.audit import replay_page

    svc, *_ = node
    monkeypatch.setattr(replay_page, "EMBED_BYTES", 10)  # smaller than the run's proof as base64
    made = svc.replay_page(lane=LANE, day=DAY, tz="UTC", screenshots=True)
    assert made["images"] == 0 and made["left_out"] == 1 and "data:image" not in made["html"]
    assert "left out of this file: the file already holds" in made["html"]
    monkeypatch.setattr(replay_page, "EMBED_BYTES", 50_000_000)
    made = svc.replay_page(lane=LANE, day=DAY, tz="UTC", screenshots=True)
    assert made["images"] == 1 and made["left_out"] == 0


# ---------------------------------------------------------------- screenshots kept after a commit

class FakeGuard:
    def __init__(self, open_for: str | None = None):
        self.open_for = open_for
        self._login_until: dict[str, float] = {}

    def clock(self) -> float:
        return 0.0

    def login_open(self, host: str) -> bool:
        return self.open_for is not None and host.endswith(self.open_for)


class FakeCtx:
    """What browser_act hands captures.after_commit: an agent's call with its own trace writer."""

    def __init__(self, store: TraceStore, home: Path, shot: bytes, start: str):
        self.principal = SimpleNamespace(kind="agent", id="claude-code")
        self.in_person = False
        self.effect = Effect.COMMIT
        self.taken = 0

        async def screenshot(_page):
            self.taken += 1
            return shot

        self.runtime = SimpleNamespace(get=lambda name: SimpleNamespace(home=home) if name == "config" else None,
                                       interaction=SimpleNamespace(screenshot=screenshot))
        self.policy = SimpleNamespace(traces=store)
        self.store, self.start = store, start

    def trace(self, kind: str, **fields):
        status, lane, summary = fields.pop("status"), fields.pop("lane"), fields.pop("summary")
        self.store.write(TraceEvent(kind=kind, status=status, lane=lane, summary=summary, args=fields,
                                    tool="browser_act", parent_id=self.start))


class FakePage:
    url = "https://www.instagram.com/p/new/"

    async def title(self):
        return "Your post <b>is live</b>"


def _act(store: TraceStore) -> str:
    start = store.write(TraceEvent(kind="tool_start", tool="browser_act", lane=LANE, status="started",
                                   principal="claude-code", principal_kind="agent",
                                   args={"on": LANE, "ref": "e12", "action": "click"}))
    store.write(TraceEvent(kind="tool_end", tool="browser_act", lane=LANE, effect="commit", status="ok",
                           parent_id=start.id, principal="claude-code", principal_kind="agent"))
    return start.id


@pytest.fixture
def capture_node(tmp_path, monkeypatch):
    from agentos.node import egress

    clock = Clock(T0)
    store = TraceStore(tmp_path / "traces.db", clock=clock)
    guard = FakeGuard()
    monkeypatch.setattr(egress, "guard_for", lambda _page: guard)
    captures._settings.clear()
    yield store, tmp_path, guard, clock
    store.close()


async def test_a_lane_that_did_not_opt_in_keeps_nothing(capture_node):
    store, home, _, _ = capture_node
    ctx = FakeCtx(store, home, png(seed=1), _act(store))
    assert await captures.after_commit(ctx, LANE, FakePage()) is None
    assert ctx.taken == 0 and not captures.capture_dir(home).exists()
    captures.settings_for(home).set("biz-b/*", True)  # another workspace only
    assert await captures.after_commit(ctx, LANE, FakePage()) is None and ctx.taken == 0


async def test_a_kept_screenshot_is_in_the_replay_and_checked_against_its_hash(capture_node):
    store, home, _, clock = capture_node
    captures.settings_for(home).set("biz-a/*", True)
    shot = png(seed=3)
    start = _act(store)
    rec = await captures.after_commit(FakeCtx(store, home, shot, start), LANE, FakePage())
    assert rec is not None and rec["sha256"] == "sha256:" + hashlib.sha256(shot).hexdigest()
    path = captures.capture_file(home, rec["sha256"], "png")
    assert path.read_bytes() == shot and oct(path.stat().st_mode & 0o777) == "0o600"
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
    row = next(r for r in store.children(start) if r["kind"] == captures.CAPTURE_KIND)
    assert rec["sha256"] in row["args"] and row["hash"] is not None  # the chain covers the hash, not the image
    svc = AuditService(store, home=home, clock=clock)
    step = svc.replay(LANE, DAY, tz="UTC")["steps"][0]
    assert step["capture"]["sha256"] == rec["sha256"] and step["capture"]["url"] == FakePage.url
    page = svc.replay_page(lane=LANE, day=DAY, tz="UTC")["html"]
    assert "data:image" not in page  # still left out by default
    made = svc.replay_page(lane=LANE, day=DAY, tz="UTC", screenshots=True)
    assert made["mismatched"] == 0 and "matches the hash the log recorded" in made["html"]
    assert "Your post &lt;b&gt;is live&lt;/b&gt;" in made["html"]
    path.write_bytes(png(seed=4))  # the image was swapped after it was taken
    made = svc.replay_page(lane=LANE, day=DAY, tz="UTC", screenshots=True)
    assert made["mismatched"] == 1 and "does not match the hash the log recorded" in made["html"]
    store.checkpoint("test")
    assert store.verify().status == "ok"


async def test_nothing_is_kept_while_the_vault_login_window_is_open(capture_node):
    store, home, guard, _ = capture_node
    captures.settings_for(home).set(LANE, True)
    guard.open_for = "instagram.com"  # the vault is filling a login on this site
    ctx = FakeCtx(store, home, png(seed=5), _act(store))
    assert await captures.after_commit(ctx, LANE, FakePage()) is None and ctx.taken == 0
    guard.open_for = None
    guard._login_until = {"facebook.com": 10.0}  # a login window open for another site of the lane
    assert await captures.after_commit(ctx, LANE, FakePage()) is None and ctx.taken == 0
    guard._login_until = {}
    assert await captures.after_commit(ctx, LANE, FakePage()) is not None and ctx.taken == 1


async def test_a_persons_own_commit_keeps_nothing(capture_node):
    store, home, _, _ = capture_node
    captures.settings_for(home).set(LANE, True)
    ctx = FakeCtx(store, home, png(seed=6), _act(store))
    ctx.in_person = True
    assert await captures.after_commit(ctx, LANE, FakePage()) is None and ctx.taken == 0


async def test_retention_and_the_size_cap_delete_captures(capture_node):
    import os

    store, home, _, _ = capture_node
    captures.settings_for(home).set(LANE, True)
    kept = []
    for seed in range(3):
        rec = await captures.after_commit(FakeCtx(store, home, png(width=40, seed=seed + 10), _act(store)), LANE,
                                          FakePage())
        kept.append(captures.capture_file(home, rec["sha256"], "png"))
    old = kept[0]
    os.utime(old, (T0 - 100 * 86400, T0 - 100 * 86400))
    assert captures.prune(home, retention_days=90, now=T0) == 1 and not old.exists()
    os.utime(kept[1], (T0 - 10, T0 - 10))
    os.utime(kept[2], (T0, T0))
    size = kept[2].stat().st_size
    assert captures.prune(home, retention_days=90, max_bytes=size, now=T0) == 1
    assert not kept[1].exists() and kept[2].exists()  # the oldest goes first


async def test_a_stale_half_written_file_goes_and_the_sweep_is_throttled(capture_node, monkeypatch):
    import os

    store, home, _, _ = capture_node
    captures.settings_for(home).set(LANE, True)
    await captures.after_commit(FakeCtx(store, home, png(seed=20), _act(store)), LANE, FakePage())
    root = captures.capture_dir(home)
    stale, fresh = root / "dead.123.tmp", root / "live.456.tmp"
    stale.write_bytes(b"x")
    fresh.write_bytes(b"y")
    os.utime(stale, (T0 - 2 * 3600, T0 - 2 * 3600))
    os.utime(fresh, (T0 - 60, T0 - 60))
    assert captures.prune(home, retention_days=90, now=T0) == 1
    assert not stale.exists() and fresh.exists()
    swept = []
    monkeypatch.setattr(captures, "prune", lambda h, **kw: swept.append(h))
    captures._last_prune.clear()
    for seed in (21, 22, 23):
        await captures.after_commit(FakeCtx(store, home, png(seed=seed), _act(store)), LANE, FakePage())
    assert swept == [home]  # one sweep, in a worker thread, not one per commit


def test_cli_turns_capture_on_and_off(tmp_path, monkeypatch):
    from agentos.audit.cli import app

    captures._settings.clear()
    monkeypatch.setenv("AGENTOS_HOME", str(tmp_path))
    runner = CliRunner()
    assert "no lane keeps" in runner.invoke(app, ["capture"]).output
    r = runner.invoke(app, ["capture", "biz-a/*", "--on"])
    assert r.exit_code == 0 and captures.settings_for(tmp_path).enabled("biz-a/social")
    assert runner.invoke(app, ["capture", "biz-a/*", "--off"]).exit_code == 0
    assert not captures.settings_for(tmp_path).enabled("biz-a/social")
    assert runner.invoke(app, ["capture", "nope", "--on"]).exit_code == 1
