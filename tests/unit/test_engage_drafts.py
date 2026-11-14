"""The draft provider, brief B3 (agentos.engage.drafts): candidates from a configured provider
enter the owner's approval flow, and a provider that cannot be reached skips only the words."""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
from datetime import timedelta
from types import SimpleNamespace as NS

import httpx
import pytest
from test_engage_executor import IG, IG_YAML, MONDAY, OWNER, Rig, _StubRunner, ig_registry
from typer.testing import CliRunner

from agentos.core.config import NodeConfig
from agentos.core.config_report import DESCRIPTIONS
from agentos.core.errors import InvalidRequest
from agentos.core.runtime import Runtime
from agentos.engage import drafts as drafts_mod
from agentos.engage.drafts import (
    DEFAULT_SECRET_REF,
    MAX_RESPONSE_BYTES,
    DraftRequest,
    DraftsContent,
    HttpDraftProvider,
    PassthroughProvider,
    ProviderUnavailable,
    check_endpoint,
    clean_candidate,
    provider_from_config,
    request_for,
    validate_response,
)
from agentos.engage.fakes import FakeOperations
from agentos.engage.items import ChainedContent, ItemsContent, ItemsInbox
from agentos.engage.migrations import DB, MIGRATIONS
from agentos.engage.models import Mode
from agentos.engage.ports import Content, ContentOrigin, ContentUnavailable, OpResult, OpStatus
from agentos.engage.store import EngageStore
from agentos.ops import migrate

URL = "https://drafts.example.com/v1/candidates"
REQ = DraftRequest("biz-a/instagram", "comment", ("coffee", "latte art"), ("lt", "en"), 140, 3,
                   voice="memory:biz-a/voice")


class FakeVault:
    def __init__(self, secrets: dict[str, str] | None = None):
        self.secrets = dict(secrets or {})
        self.reads: list[tuple[str, str]] = []

    async def get_secret(self, ref: str, *, purpose: str) -> str | None:
        self.reads.append((ref, purpose))
        return self.secrets.get(ref)


def answer(payload, status: int = 200, seen: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        if isinstance(payload, Exception):
            raise payload
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return httpx.Response(status, content=body, headers={"content-type": "application/json"})
    return httpx.MockTransport(handler)


def http(payload, *, status: int = 200, seen: list | None = None, vault=None, ref: str = "") -> HttpDraftProvider:
    return HttpDraftProvider(URL, secret_ref=ref, timeout_s=5, vault=vault, transport=answer(payload, status, seen))


def ask(provider, req: DraftRequest = REQ) -> list[str]:
    return asyncio.run(provider.candidates(req))


# ---------------------------------------------------------------- the http provider


def test_the_request_carries_the_kind_topics_languages_and_length_and_the_token_from_the_vault(caplog):
    seen: list[httpx.Request] = []
    vault = FakeVault({"engage/draft-provider/token": "tok-123"})
    caplog.set_level(logging.DEBUG)
    got = ask(http({"candidates": ["Nice crema on that one", "Which beans?"]}, seen=seen, vault=vault,
                   ref="engage/draft-provider/token"))
    assert got == ["Nice crema on that one", "Which beans?"]
    [req] = seen
    assert req.method == "POST" and str(req.url) == URL
    assert req.headers["authorization"] == "Bearer tok-123"
    assert json.loads(req.content) == {"kind": "comment", "topics": ["coffee", "latte art"], "languages": ["lt", "en"],
                                       "max_length": 140, "n": 3, "target": None, "voice": "memory:biz-a/voice"}
    assert "biz-a/instagram" not in req.content.decode()          # no account name, no secret in the body
    assert vault.reads == [("engage/draft-provider/token", "engage draft provider")]
    assert "tok-123" not in caplog.text and "crema" not in caplog.text and "2 candidates" in caplog.text


def test_no_secret_ref_sends_no_token_and_a_missing_token_is_unavailable():
    seen: list[httpx.Request] = []
    ask(http({"candidates": ["a fine one"]}, seen=seen))
    assert "authorization" not in seen[0].headers
    with pytest.raises(ProviderUnavailable) as e:
        ask(http({"candidates": ["x"]}, vault=FakeVault(), ref="engage/draft-provider/token"))
    assert e.value.reason == "no_token"
    with pytest.raises(ProviderUnavailable) as e:
        ask(http({"candidates": ["x"]}, ref="engage/draft-provider/token"))
    assert e.value.reason == "no_vault"


def test_https_is_required_except_to_this_machine():
    for ok in (URL, "http://127.0.0.1:8080/c", "http://localhost/c", "http://[::1]:9/c"):
        assert check_endpoint(ok) == ok
    for bad in ("http://drafts.example.com/c", "ftp://x/y", "", "drafts.example.com", "https://u:p@drafts.example.com/"):
        with pytest.raises(InvalidRequest):
            check_endpoint(bad)
    with pytest.raises(InvalidRequest, match="https"):
        HttpDraftProvider("http://10.0.0.5/c")
    with pytest.raises(ValueError, match="https"):
        NodeConfig(engage_draft_provider="http", engage_draft_endpoint="http://drafts.example.com/c")
    with pytest.raises(ValueError, match="needs engage_draft_endpoint"):
        NodeConfig(engage_draft_provider="http")
    with pytest.raises(ValueError, match="off, passthrough or http"):
        NodeConfig(engage_draft_provider="brain")


def test_the_answer_is_checked_strictly():
    long = "x" * 141
    five = DraftRequest("a/b", "comment", max_chars=140, n=5)
    assert ask(http({"candidates": ["  ok one  ", long, "", "ok one", "line one\nline two"]}), five) == \
        ["ok one", "line one\nline two"]                      # trimmed, too long and empty dropped, no repeats
    assert ask(http({"candidates": ["bell\x07", "hidden \u202eevil", "zero\u200fmark", "fine"]}), five) == ["fine"]
    for payload, reason in (({"candidates": [long]}, "no_candidates"),
                            ({"candidates": ["a", "b", "c", "d"]}, "bad_response"),       # more than n
                            ({"candidates": ["a", 3]}, "bad_response"),                  # wrong type
                            ({"candidates": "a"}, "bad_response"),
                            (["a"], "bad_response"),
                            (b"not json", "bad_response"),
                            (b"x" * (300 * 1024), "bad_response")):
        with pytest.raises(ProviderUnavailable) as e:
            ask(http(payload))
        assert e.value.reason == reason, payload if not isinstance(payload, bytes) else payload[:10]
    with pytest.raises(ProviderUnavailable) as e:
        ask(http({"candidates": ["a"]}, status=503))
    assert e.value.reason == "http_503"
    with pytest.raises(ProviderUnavailable) as e:
        ask(http({"candidates": ["a"]}, status=302))           # redirects are not followed
    assert e.value.reason == "http_302"
    with pytest.raises(ProviderUnavailable) as e:
        ask(http(httpx.ConnectError("refused")))
    assert e.value.reason == "unreachable"
    with pytest.raises(ProviderUnavailable) as e:
        ask(http(httpx.ReadTimeout("slow")))
    assert e.value.reason == "timeout"
    assert isinstance(e.value, ContentUnavailable)
    assert validate_response({"candidates": ["a"]}, 1, 5) == ["a"]


def test_passthrough_is_deterministic_and_within_the_request():
    p = PassthroughProvider()
    assert ask(p) == ask(p) == ["Test comment 1 about coffee (lt)", "Test comment 2 about latte art (lt)",
                                "Test comment 3 about coffee (lt)"]
    short = ask(p, DraftRequest("a/b", "post", (), (), 20, 2))
    assert len(short) == 2 and all(len(t) <= 20 for t in short)


def test_the_request_follows_the_program():
    rig_program = NS(site="reddit", surface="biz-a/reddit",
                     comments=NS(topics=["coffee"], voice="memory:biz-a/voice", max_chars=200),
                     steady=NS(filters=NS(language=["en"])))
    c = request_for(rig_program, "comment")
    assert (c.kind, c.topics, c.languages, c.max_chars, c.n, c.voice) == \
        ("comment", ("coffee",), ("en",), 200, 3, "memory:biz-a/voice")
    assert request_for(rig_program, "post").max_chars == 5000      # the site takes 40000; a draft is asked shorter
    assert request_for(rig_program.__class__(**{**vars(rig_program), "site": "linkedin"}), "comment").max_chars == 200


# ---------------------------------------------------------------- the content port in sessions


class CountingProvider(PassthroughProvider):
    def __init__(self, fail: bool = False):
        self.calls = 0
        self.fail = fail

    async def candidates(self, request: DraftRequest) -> list[str]:
        self.calls += 1
        if self.fail:
            raise ProviderUnavailable("unreachable", "the draft provider could not be reached (ConnectError)")
        return await super().candidates(request)


def drafts_rig(tmp_path, provider, **kw) -> tuple[Rig, DraftsContent]:
    rig = Rig(tmp_path, **kw)
    drafts = DraftsContent(rig.svc.store, provider, clock=lambda: rig.t)
    rig.content = rig.ex.content = ChainedContent([ItemsContent(rig.svc.store, clock=lambda: rig.t), drafts])
    return rig, drafts


def _windows_with(rig: Rig, kind: str, *, others: bool = True):
    for d in range(7):
        day = MONDAY + timedelta(days=d)
        for w in rig.ex.windows(rig.surface, day):
            if kind in w.kinds and (not others or any(k != kind for k in w.kinds)):
                yield day, w


def test_drafts_enter_the_owners_approval_and_are_never_approved(tmp_path):
    made: list[dict] = []
    approvals = NS(create_action=lambda **kw: made.append(kw) or NS(id=f"ap_{len(made)}"))
    from agentos.engage.adapters import RunnerOperations

    runner = _StubRunner()
    rig, drafts = drafts_rig(tmp_path, PassthroughProvider(),
                             ops=RunnerOperations(Runtime(runner=runner, approvals=approvals)))
    day, w = next(_windows_with(rig, "comment"))
    rep = rig.run(day, w.idx)
    comments = [o for o in rep["ops"] if o["kind"] == "comment"]
    assert comments and {o["status"] for o in comments} == {"awaiting_approval"}
    assert all(o["content_origin"] == "draft" for o in comments)
    assert len(made) == len(comments)                                   # one card per draft
    texts = {m["content"]["params"]["text"] for m in made}
    assert all(t.startswith("Test comment") for t in texts)
    assert all("approve mode" in m["reason"] for m in made)
    asked = [h["payload"]["ask"] for h in rig.svc.store.history(IG, kinds=("op",)) if h["payload"]["kind"] == "comment"]
    assert asked and all(asked)
    rows = rig.svc.drafts.list(IG)
    used = [d for d in rows if d["status"] == "used"]
    assert {d["id"] for d in used} == {o["content_id"] for o in comments} and all(d["run_id"] == "run_1" for d in used)
    assert sum(d["status"] == "alternative" for d in rows) == 2 * len(comments)    # the other candidates are kept
    assert {d["batch"] for d in rows if d["status"] == "alternative"} == {d["batch"] for d in used}
    assert rep["status"] == "done" and any(o["status"] == "verified" for o in rep["ops"] if o["kind"] != "comment")
    content = drafts.take(IG, "comment", 1)
    assert content == []                                                 # nothing handed back: take never calls out


def test_an_unreachable_provider_skips_the_words_and_the_next_window_asks_again(tmp_path):
    provider = CountingProvider(fail=True)
    rig, _ = drafts_rig(tmp_path, provider)
    windows = list(_windows_with(rig, "comment"))
    (day1, w1), (day2, w2) = windows[0], windows[1]
    assert (day1, w1.idx) != (day2, w2.idx)
    rep = rig.run(day1, w1.idx)
    assert rep["status"] == "done"
    comments = [o for o in rep["ops"] if o["kind"] == "comment"]
    assert {(o["status"], o["reason"]) for o in comments} == {("skipped", "draft_unavailable")}
    others = [o for o in rep["ops"] if o["kind"] != "comment"]
    assert others and all(o["status"] == "verified" for o in others)    # the rest of the window ran
    assert provider.calls == 1                                           # asked once in the window
    [hold] = rig.svc.store.holds(IG)                                     # a hold note for the provider only
    assert (hold["kind"], hold["reason"], hold["detail"]) == ("draft_provider", "draft_provider_unavailable",
                                                              "unreachable")
    assert hold["until"] is not None and rig.ex.active_holds(IG) == []   # it holds neither the surface nor a kind
    assert rig.ex.active_holds(IG, kind="comment") == []
    assert [h["kind"] for h in rig.svc.status(IG)["surfaces"][0]["holds"]] == ["draft_provider"]
    [note] = rig.svc.store.history(IG, kinds=("drafts_unavailable",))
    assert note["payload"]["reason"] == "unreachable" and note["payload"]["session"] == [day1.isoformat(), w1.idx]
    [notice] = [n for n in rig.svc.store.notices(IG) if n["kind"] == "drafts"]
    assert "asks again" in notice["text"] and "—" not in notice["text"]

    provider.fail = False                                                # the next window retries on its own
    rep = rig.run(day2, w2.idx)
    comments = [o for o in rep["ops"] if o["kind"] == "comment"]
    assert comments and all(o["status"] == "awaiting_approval" or o["status"] == "verified" for o in comments)
    assert all(o["content_origin"] == "draft" for o in comments)
    assert provider.calls == 1 + len(comments)


def test_items_come_before_drafts_and_an_item_still_goes_out_when_the_provider_is_down(tmp_path):
    provider = CountingProvider(fail=True)
    rig, _ = drafts_rig(tmp_path, provider)
    inbox = ItemsInbox(rig.svc.store, registry=ig_registry(), clock=lambda: rig.t)
    item = inbox.submit(inbox.check(IG, "comment", "Owner's own words", targets=["https://www.instagram.com/p/abc/"]),
                        in_person=True)
    chain = rig.ex.content
    [got] = asyncio.run(chain.atake(IG, "comment", 1))
    assert got.origin is ContentOrigin.ITEM and got.id == item["id"] and provider.calls == 0
    with pytest.raises(ContentUnavailable):
        asyncio.run(chain.atake(IG, "comment", 1))                     # no item left, and the provider is down
    provider.fail = False
    [d] = asyncio.run(chain.atake(IG, "comment", 1))
    assert d.origin is ContentOrigin.DRAFT and d.approved is False and provider.calls == 2
    chain.settle(IG, got, used=True, run_id="run-9")                   # each settle goes back to its port
    chain.settle(IG, d, used=False)
    assert rig.svc.store.item(item["id"])["status"] == "used"
    assert rig.svc.drafts.get(d.id)["status"] == "returned"
    kinds = [h["kind"] for h in rig.svc.store.history(IG)]
    assert "item_used" in kinds and "draft_returned" in kinds and "drafts_received" in kinds


def test_settle_routing_reuse_outcomes_and_the_sweep(tmp_path):
    rig, drafts = drafts_rig(tmp_path, CountingProvider())
    drafts.approval_of = lambda run_id: "ap_77"
    store = rig.svc.store
    [d] = asyncio.run(drafts.atake(IG, "comment", 1))
    ChainedContent([ItemsContent(store), drafts]).settle(IG, d, used=False)   # after a restart: by origin
    assert drafts.book.get(d.id)["status"] == "returned"
    [again] = drafts.take(IG, "comment", 1)                             # handed back: offered again, no call
    assert again.id == d.id and drafts.provider.calls == 1
    drafts.settle(IG, again, used=True, run_id="run-5")
    row = drafts.book.get(d.id)
    assert (row["status"], row["run_id"], row["approval_id"]) == ("used", "run-5", "ap_77")
    [h] = store.history(IG, kinds=("draft_used",))
    assert h["payload"] == {"id": d.id, "run_id": "run-5", "approval_id": "ap_77"}
    drafts.settle(IG, again, used=True, run_id="run-5")                 # settled twice: no second entry
    assert len(store.history(IG, kinds=("draft_used",))) == 1

    # the outcome of the operation that carried it
    day, w = next(_windows_with(rig, "comment"))
    sid = store.put_session(IG, day, w.idx, planned_start=w.start, minutes=w.minutes)["id"]
    [op] = store.add_ops(sid, IG, day, ["comment"])
    store.set_op(op["id"], status="failed", reason="denied", content_origin="draft", content_id=d.id)
    [d2] = asyncio.run(drafts.atake(IG, "comment", 1))
    drafts.settle(IG, d2, used=True, run_id="run-6")
    [op2] = [o for o in store.add_ops(sid, IG, day, ["comment"]) if o["id"] != op["id"]]
    store.set_op(op2["id"], status="verified", content_origin="draft", content_id=d2.id)
    [d3] = asyncio.run(drafts.atake(IG, "comment", 1))                 # left pending: its window is over
    out = rig.svc.drafts.sweep(rig.ex.now() + timedelta(minutes=5))
    assert (out["denied"], out["approved"], out["returned"]) == (1, 1, 1)
    assert [drafts.book.get(x.id)["status"] for x in (d, d2, d3)] == ["denied", "approved", "returned"]
    out = rig.svc.drafts.sweep(rig.ex.now() + timedelta(hours=25))
    assert drafts.book.get(d3.id)["status"] == "expired" and out["expired"] >= 7   # and every alternative
    assert drafts.take(IG, "comment", 1) == []
    assert rig.svc.status(IG)["surfaces"][0]["drafts"]["expired"] >= 7


def test_a_broken_provider_is_an_unavailable_one():
    class Boom:
        name = "boom"

        async def candidates(self, request):
            raise RuntimeError("bug")

    store = NS(active=lambda s: NS(definition=NS(site="reddit", surface=s, comments=NS(topics=[], voice=None,
                                                                                         max_chars=100),
                                                 steady=NS(filters=NS(language=[])))))
    port = DraftsContent.__new__(DraftsContent)
    port.store, port.provider, port.candidates, port.approval_of = store, Boom(), 3, None
    port.clock = lambda: 0.0
    port.book = NS(reserve_returned=lambda *a, **k: [])
    with pytest.raises(ProviderUnavailable) as e:
        asyncio.run(port.atake("biz-a/reddit", "comment", 1))
    assert e.value.reason == "error"


# ---------------------------------------------------------------- the schema, the wiring and the CLI


def test_migration_4_adds_drafts_to_a_version_3_database(tmp_path):
    v3 = migrate.MigrationSet(package="engage", db=DB, create=True, migrations=MIGRATIONS.migrations[:3])
    migrate.migrate(tmp_path, [v3], backup=False)
    path = tmp_path / DB
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO items (id, surface, kind, text, expires_at, submitted_by, submitted_at) "
                 "VALUES ('it_0000000000000001', 'biz-a/reddit', 'post', 'hi', '2030-01-01', 'owner', '2026-01-01')")
    conn.commit()
    assert not conn.execute("SELECT name FROM sqlite_master WHERE name = 'drafts'").fetchall()
    conn.close()
    store = EngageStore(tmp_path)
    try:
        assert store.item("it_0000000000000001")["text"] == "hi"
        cols = {r["name"] for r in store._rows("PRAGMA table_info(drafts)")}
        assert cols >= {"id", "surface", "kind", "batch", "text", "target", "status", "approval_id", "run_id",
                        "provider", "created_at", "taken_at", "settled_at"}
    finally:
        store.close()
    [st] = [s for s in migrate.status(tmp_path) if s.package == "engage"]
    assert st.current == st.latest == len(MIGRATIONS.migrations)


def test_config_settings_have_plain_descriptions_and_build_wires_drafts_after_items(tmp_path):
    names = ("engage_draft_provider", "engage_draft_endpoint", "engage_draft_secret_ref", "engage_draft_timeout_s")
    for n in names:
        assert n in NodeConfig.model_fields and DESCRIPTIONS[n] and "—" not in DESCRIPTIONS[n]
    assert NodeConfig().engage_draft_provider == "off"
    assert provider_from_config(NodeConfig()) is None and provider_from_config(None) is None
    assert isinstance(provider_from_config(NodeConfig(engage_draft_provider="passthrough")), PassthroughProvider)
    p = provider_from_config(NodeConfig(engage_draft_provider="http", engage_draft_endpoint=URL,
                                        engage_draft_secret_ref="engage/draft-provider/token"))
    assert isinstance(p, HttpDraftProvider) and p.name == "http:drafts.example.com" and p.timeout_s == 20.0

    from agentos.engage.service import build

    def built(cfg):
        rt = Runtime(config=cfg, runner=_StubRunner(), lanes=NS())
        svc = build(rt, home=tmp_path)
        svc.store.close()
        return svc.executor.content

    off = built(NodeConfig(home=tmp_path))
    assert [type(p).__name__ for p in off.ports] == ["ItemsContent"]
    on = built(NodeConfig(home=tmp_path, engage_draft_provider="passthrough"))
    assert [type(p).__name__ for p in on.ports] == ["ItemsContent", "DraftsContent"]


def test_cli_lists_drafts_and_tests_the_provider_without_printing_text(tmp_path, monkeypatch):
    from agentos.cli import app

    monkeypatch.setenv("AGENTOS_HOME", str(tmp_path))
    rig, drafts = drafts_rig(tmp_path, PassthroughProvider())
    [d] = asyncio.run(drafts.atake(IG, "comment", 1))
    drafts.settle(IG, d, used=True, run_id="run-3")
    rig.svc.store.close()
    out = CliRunner().invoke(app, ["engage", "drafts", IG])
    assert out.exit_code == 0, out.output
    assert d.id in out.output and "used  run run-3" in out.output and out.output.count("alternative") == 2
    assert "Test comment 2" in out.output

    res = CliRunner().invoke(app, ["engage", "provider", "test"])
    assert res.exit_code == 1 and "engage_draft_provider" in res.output
    (tmp_path / "node.toml").write_text('engage_draft_provider = "passthrough"\n')
    res = CliRunner().invoke(app, ["engage", "provider", "test"])
    assert res.exit_code == 0, res.output
    assert "passthrough: 3 of 3 candidates in" in res.output and "Test comment" not in res.output
    res = CliRunner().invoke(app, ["engage", "provider", "test", "--surface", IG, "--json"])
    assert res.exit_code == 0 and json.loads(res.output)["max_length"] == 140
    assert all("—" not in r for r in (out.output, res.output))


def test_a_draft_is_never_approved_content(tmp_path):
    rig, drafts = drafts_rig(tmp_path, PassthroughProvider())
    got = asyncio.run(drafts.atake(IG, "comment", 2))
    assert len(got) == 2 and all(isinstance(d, Content) and d.approved is False for d in got)
    program = rig.svc.store.active(IG).definition
    assert all(rig.ex._ask(program, "comment", True, d) is True for d in got)
    for site in ("linkedin", "reddit"):                  # auto stays refused there, drafts or not
        from agentos.engage.models import Comments, Mode

        sneaky = program.model_copy(update={"site": site, "comments": Comments(mode=Mode.AUTO)})
        assert rig.ex._ask(sneaky, "comment", True, got[0]) is True


# ---------------------------------------------------------------- review fixes


class CommentsGo(FakeOperations):
    """Every comment ends as `result` (a callable of the call, or an OpResult); the rest verify."""

    def __init__(self, result):
        super().__init__()
        self.result = result

    async def run(self, surface, spec, params, **kw):
        if str(spec.kind) == "comment":
            return self.result() if callable(self.result) else self.result
        return await super().run(surface, spec, params, **kw)


def _comment_ops(rep: dict) -> list[dict]:
    return [o for o in rep["ops"] if o["kind"] == "comment" and o["content_id"]]


def test_a_draft_whose_run_may_have_acted_is_failed_and_never_offered_again(tmp_path):
    ops = CommentsGo(OpResult(OpStatus.FAILED, error="flow_error", run_id="run-x"))       # acted unknown
    rig, drafts = drafts_rig(tmp_path, CountingProvider(), ops=ops)
    day, w = next(_windows_with(rig, "comment"))
    ids = [o["content_id"] for o in _comment_ops(rig.run(day, w.idx))]
    assert ids and {drafts.book.get(i)["status"] for i in ids} == {"failed"}
    assert len(set(ids)) == len(ids)                               # no window took a failed draft again
    assert drafts.take(IG, "comment", 5) == []
    rig.svc.drafts.sweep(rig.ex.now() + timedelta(hours=3))
    assert {drafts.book.get(i)["status"] for i in ids} == {"failed"}
    assert len(rig.svc.store.history(IG, kinds=("draft_failed",))) == len(ids)


def test_a_draft_goes_back_only_when_its_run_provably_did_not_act(tmp_path):
    ops = CommentsGo(OpResult(OpStatus.FAILED, error="lane_busy", acted=False))           # refused before a run
    rig, drafts = drafts_rig(tmp_path, CountingProvider(), ops=ops)
    ids: list[str] = []
    for day, w in list(_windows_with(rig, "comment"))[:3]:
        ids += [o["content_id"] for o in _comment_ops(rig.run(day, w.idx))]
    assert len(ids) >= 3
    # a draft that went back is taken again by a later window (within 24 hours of its return), once only
    twice = [i for i in dict.fromkeys(ids) if ids.count(i) == 2]
    assert twice and all(ids.count(i) <= 2 for i in ids)
    assert all(drafts.book.get(i)["offers"] == 2 for i in twice)
    assert {drafts.book.get(i)["status"] for i in ids} == {"returned"}
    assert not {d.id for d in drafts.take(IG, "comment", 5)} & set(twice)


def test_a_window_stopped_mid_run_fails_its_draft(tmp_path):
    def stop():
        raise asyncio.CancelledError()                            # what a daemon stop delivers

    rig, drafts = drafts_rig(tmp_path, CountingProvider(), ops=CommentsGo(stop))
    day, w = next(_windows_with(rig, "comment"))
    rig.at(w.start)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(rig.ex.run_window(IG, day, w.idx))
    [op] = [o for o in rig.svc.store.ops(IG, day) if o["kind"] == "comment" and o["status"] == "attempted"]
    assert drafts.book.get(op["content_id"])["status"] == "failed"
    rig.svc.drafts.sweep(rig.ex.now() + timedelta(hours=3))
    assert drafts.book.get(op["content_id"])["status"] == "failed" and drafts.take(IG, "comment", 5) == []


def test_the_sweep_fails_a_pending_draft_an_operation_tried_and_never_reoffers_one_with_a_run(tmp_path):
    rig, drafts = drafts_rig(tmp_path, CountingProvider())
    store = rig.svc.store
    day, w = next(_windows_with(rig, "comment"))
    row = store.put_session(IG, day, w.idx, planned_start=w.start, minutes=w.minutes)
    tried, untried, with_run = asyncio.run(drafts.atake(IG, "comment", 3))
    a, b = store.add_ops(row["id"], IG, day, ["comment", "comment"])
    store.set_op(a["id"], status="attempted", content_origin="draft", content_id=tried.id)   # the process died
    store.set_op(b["id"], status="failed", run_id="run-7", content_origin="draft", content_id=with_run.id)
    drafts.settle(IG, with_run, used=False)                       # even handed back, it had a run
    store.set_session(row["id"], status="aborted")
    out = rig.svc.drafts.sweep(rig.ex.now() + timedelta(minutes=5))
    assert (out["failed"], out["returned"]) == (1, 1)
    assert [drafts.book.get(d.id)["status"] for d in (tried, untried, with_run)] == ["failed", "returned", "returned"]
    assert [d.id for d in drafts.take(IG, "comment", 5)] == [untried.id]


def test_a_handed_back_draft_is_offered_once_more_within_24_hours_of_its_return(tmp_path):
    rig, drafts = drafts_rig(tmp_path, CountingProvider())
    [d] = asyncio.run(drafts.atake(IG, "comment", 1))
    rig.t += 20 * 3600
    drafts.settle(IG, d, used=False)
    rig.t += 10 * 3600                                            # 30 h after it was written, 10 h after its return
    [again] = drafts.take(IG, "comment", 1)
    assert again.id == d.id
    drafts.settle(IG, again, used=False)
    assert drafts.take(IG, "comment", 1) == []                    # offered again once only
    assert drafts.book.get(d.id)["offers"] == 2
    rig.svc.drafts.sweep(rig.ex.now() + timedelta(hours=25))
    assert drafts.book.get(d.id)["status"] == "expired"


def test_a_used_draft_is_approved_only_when_verified_and_expires_with_its_card(tmp_path):
    rig, drafts = drafts_rig(tmp_path, CountingProvider())
    store = rig.svc.store
    day, w = next(_windows_with(rig, "comment"))
    sid = store.put_session(IG, day, w.idx, planned_start=w.start, minutes=w.minutes)["id"]
    texts = asyncio.run(drafts.atake(IG, "comment", 3))
    ops = store.add_ops(sid, IG, day, ["comment"] * 3)
    outcomes = [("unverified", "still_running"), ("failed", "approval_expired"), ("verified", None)]
    for d, op, (status, reason) in zip(texts, ops, outcomes, strict=True):
        store.set_op(op["id"], status="awaiting_approval", run_id=f"run-{op['id']}", content_origin="draft",
                     content_id=d.id)
        drafts.settle(IG, d, used=True, run_id=f"run-{op['id']}")
        store.set_op(op["id"], status=status, reason=reason)
    assert {drafts.book.get(d.id)["status"] for d in texts} == {"used"}
    out = rig.svc.drafts.sweep(rig.ex.now() + timedelta(minutes=5))
    assert (out["approved"], out["expired"]) == (1, 1)
    assert [drafts.book.get(d.id)["status"] for d in texts] == ["used", "expired", "approved"]


def test_the_whole_request_has_one_deadline():
    async def trickle():
        for ch in b'{"candidates": ["hello there"]}' + b" " * 170:  # about 20 s in all
            await asyncio.sleep(0.1)                               # each byte in time, the whole far too slow
            yield bytes([ch])

    async def handler(request):
        return httpx.Response(200, content=trickle())

    p = HttpDraftProvider(URL, timeout_s=1.0, transport=httpx.MockTransport(handler))
    t0 = time.monotonic()
    with pytest.raises(ProviderUnavailable) as e:
        ask(p)
    assert e.value.reason == "timeout" and time.monotonic() - t0 < 6.0  # the 1 s deadline, not the 20 s body


def test_a_large_answer_is_cut_off_while_it_streams():
    sent = []

    async def endless():
        while True:
            sent.append(1)
            yield b" " * 65536

    async def handler(request):
        return httpx.Response(200, content=endless())

    with pytest.raises(ProviderUnavailable) as e:
        ask(HttpDraftProvider(URL, timeout_s=5, transport=httpx.MockTransport(handler)))
    assert e.value.reason == "bad_response" and "too large" in str(e.value)
    assert len(sent) <= MAX_RESPONSE_BYTES // 65536 + 2


def test_hidden_characters_are_refused_and_text_is_nfc():
    def u(*cps: int) -> str:
        return "".join(map(chr, cps))

    zwj, vs16 = 0x200D, 0xFE0F
    hidden = (0x200B, 0x200C, zwj, 0x200E, 0x200F, 0x2060, 0x2061, 0x2064, 0xFEFF, 0xE0061, 0xE0001, 0x202E,
              0x2066, 0x00AD, 0x061C, 0xFE00, 0xE0100, 0x3164)
    for cp in hidden:
        assert clean_candidate("hi" + chr(cp) + "there", 140) is None, hex(cp)
    assert clean_candidate(u(vs16) + "start", 140) is None             # a selector with nothing to select
    family = u(0x1F468, zwj, 0x1F469, zwj, 0x1F467)
    for fine in ("family " + family + " day", "love " + u(0x2764, vs16),
                 "wave " + u(0x1F44B, 0x1F3FD, zwj, 0x2642, vs16), "keycap 1" + u(vs16, 0x20E3),
                 "Lithuanian " + u(0x105, 0x10D, 0x119, 0x117, 0x12F, 0x161, 0x173, 0x16B, 0x17E)):
        assert clean_candidate(fine, 140) == fine
    assert clean_candidate("cafe" + u(0x301), 140) == "caf" + u(0xE9)   # NFC: one code point, counted as one
    assert clean_candidate(("e" + u(0x301)) * 3, 3) == u(0xE9) * 3


def test_the_client_ignores_proxy_settings_from_the_environment(monkeypatch):
    seen = {}
    real = httpx.AsyncClient

    def client(**kw):
        seen.update(kw)
        return real(**kw)

    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")
    monkeypatch.setattr(drafts_mod.httpx, "AsyncClient", client)
    vault = FakeVault({"engage/draft-provider/token": "tok-1"})
    ask(http({"candidates": ["fine"]}, vault=vault, ref="engage/draft-provider/token"))
    assert seen["trust_env"] is False


def test_the_default_token_ref_is_read_when_none_is_set():
    seen: list[httpx.Request] = []
    p = http({"candidates": ["fine"]}, seen=seen, vault=FakeVault({DEFAULT_SECRET_REF: "tok-9"}))
    ask(p)
    assert seen[-1].headers["authorization"] == "Bearer tok-9" and p.token_ref == DEFAULT_SECRET_REF
    p = http({"candidates": ["fine"]}, seen=seen, vault=FakeVault())     # nothing stored: no token, no error
    ask(p)
    assert "authorization" not in seen[-1].headers and p.token_ref is None


def test_provider_test_says_when_no_token_is_configured_and_survives_a_deep_answer(tmp_path, monkeypatch):
    from agentos.cli import app

    monkeypatch.setenv("AGENTOS_HOME", str(tmp_path))
    payload = {"candidates": ["fine"]}
    monkeypatch.setattr(drafts_mod, "provider_from_config", lambda *a, **k: http(payload))
    res = CliRunner().invoke(app, ["engage", "provider", "test"])
    assert res.exit_code == 0, res.output
    assert "No token is configured" in res.output and "agentos engage provider token" in res.output
    assert "—" not in res.output
    payload = b"[" * 100000 + b"]" * 100000                        # deep enough for a RecursionError
    res = CliRunner().invoke(app, ["engage", "provider", "test", "--json"])
    assert res.exit_code == 1 and json.loads(res.output)["reason"] == "bad_response"
    with pytest.raises(ProviderUnavailable) as e:
        ask(http(payload))
    assert e.value.reason == "bad_response"


AUTO_IG = IG_YAML.replace("mode: approve", "mode: auto")


def test_passthrough_text_always_gets_a_card_even_in_auto_mode(tmp_path):
    ops = CommentsGo(lambda: OpResult(OpStatus.AWAITING_APPROVAL, run_id="run-1"))
    seen: list[bool] = []
    real = ops.run

    async def run(surface, spec, params, **kw):
        if str(spec.kind) == "comment":
            seen.append(kw["ask"])
        return await real(surface, spec, params, **kw)

    ops.run = run
    rig, _ = drafts_rig(tmp_path, PassthroughProvider(), yaml_text=AUTO_IG, ops=ops)
    assert rig.svc.store.active(IG).definition.comments.mode is Mode.AUTO
    day, w = next(_windows_with(rig, "comment"))
    rig.run(day, w.idx)
    assert seen and all(seen)                                     # every passthrough draft asked
    # an http draft in auto mode on Instagram goes out without a card: the documented exception
    program = rig.svc.store.active(IG).definition
    real_draft = Content(id="dr_0000000000000001", origin=ContentOrigin.DRAFT, text="x")
    assert rig.ex._ask(program, "comment", True, real_draft) is False


def test_passthrough_does_not_start_and_no_auto_program_is_approved_with_it(tmp_path):
    from agentos.engage.errors import ProgramInvalid
    from agentos.engage.service import EngageService, build

    rig = Rig(tmp_path, yaml_text=AUTO_IG)                       # an approved program in auto mode
    rig.svc.store.close()
    rt = Runtime(config=NodeConfig(home=tmp_path, engage_draft_provider="passthrough"), runner=_StubRunner(),
                 lanes=NS())
    svc = build(rt, home=tmp_path)
    try:
        assert [type(p).__name__ for p in svc.executor.content.ports] == ["ItemsContent"]
        [n] = [n for n in svc.store.notices(None) if n["kind"] == "drafts"]
        assert "not started" in n["text"] and IG in n["text"] and "—" not in n["text"]
    finally:
        svc.store.close()

    other = tmp_path / "other"
    svc = EngageService(Runtime(config=NodeConfig(home=other, engage_draft_provider="passthrough")), home=other)
    try:
        out = svc.draft(IG, AUTO_IG, by=OWNER)
        with pytest.raises(ProgramInvalid, match="passthrough"):
            svc.approve(out["program"], by=OWNER)
        out = svc.draft(IG, IG_YAML, by=OWNER)                   # approve mode is fine
        assert svc.approve(out["program"], by=OWNER).state.value == "approved"
    finally:
        svc.store.close()
