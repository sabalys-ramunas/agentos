"""The files, upload, tab and dialog tools (agentos.node.io_tools) over fakes."""

from __future__ import annotations

import base64
import hashlib
import inspect
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_io_tabs_dialogs import FakeContext, open_dialog

from agentos.core.config import NodeConfig, save_proxies
from agentos.core.errors import (
    AgentPaused,
    ApprovalPending,
    InvalidRequest,
    LaneNotFound,
    NotFound,
    PolicyDenied,
    ServiceUnavailable,
)
from agentos.core.models import (
    ControlState,
    Effect,
    LaneControl,
    LaneInfo,
    LaneKind,
    LaneSpec,
    Principal,
    ProxyConfig,
    Signal,
)
from agentos.core.runtime import Runtime
from agentos.core.tools import TOOL_NAME_RE, ToolImage
from agentos.node import io_tools
from agentos.node.camoufox.engine import PageTracker
from agentos.node.files import MB, FileStatus, LaneFiles
from agentos.node.interaction import UploadResult

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
)
PDF = b"%PDF-1.7\n" + b"0" * 64
LANE = "acme/instagram"


class Ctx:
    def __init__(self, runtime: Runtime, *, deny: Exception | None = None, kind: str = "agent"):
        self.principal = Principal(id="test-agent", kind=kind)
        self.runtime = runtime
        self.deny = deny
        self.authorized: list[dict[str, Any]] = []
        self.traces: list[tuple[str, dict[str, Any]]] = []

    async def authorize(self, effect, *, lane_id=None, domain=None, summary=None, content=None) -> None:
        self.authorized.append({"effect": effect, "lane_id": lane_id, "domain": domain, "summary": summary,
                                "content": content})
        if self.deny is not None:
            raise self.deny

    def trace(self, kind: str, **fields: Any) -> None:
        self.traces.append((kind, fields))


class Lanes:
    def __init__(self, config: NodeConfig):
        self.config = config
        self.infos: dict[str, LaneInfo] = {}
        self.context = FakeContext()
        self.tracker = PageTracker(self.context)
        self.used: list[str] = []

    def add(self, lane_id: str, **kw: Any) -> LaneInfo:
        info = LaneInfo(spec=LaneSpec(id=lane_id, **{k: v for k, v in kw.items() if k != "control"}),
                        control=kw.get("control", LaneControl()))
        self.infos[lane_id] = info
        self.config.lane_dir(lane_id).mkdir(parents=True, mode=0o700, exist_ok=True)
        return info

    def get(self, lane_id: str) -> LaneInfo:
        if lane_id not in self.infos:
            raise LaneNotFound(f"no lane {lane_id}")
        return self.infos[lane_id]

    @asynccontextmanager
    async def using(self, lane_id: str):
        self.get(lane_id)
        self.used.append(lane_id)
        page = await self.tracker.page()
        if page.url == "about:blank":
            page.url = "https://www.instagram.com/create"
        yield page


class Interaction:
    def __init__(self) -> None:
        self.uploads: list[dict[str, Any]] = []

    async def upload(self, page, ref, paths, *, mimes=None, root=None) -> UploadResult:
        self.uploads.append({"ref": ref, "paths": list(paths), "mimes": mimes, "root": root})
        files = [{"name": Path(p).name, "size": Path(p).stat().st_size, "type": m} for p, m in zip(paths, mimes,
                                                                                                  strict=True)]
        return UploadResult(ok=True, ref=ref, url=page.url, via="chooser", files=files, signal=Signal.OK,
                            detail="the input holds it")


@pytest.fixture
def env(tmp_path):
    config = NodeConfig(home=tmp_path / "home", ssh_host="mynode")
    lanes = Lanes(config)
    lanes.add(LANE)
    interaction = Interaction()
    runtime = Runtime(config=config, lanes=lanes, interaction=interaction)
    return runtime, lanes, interaction


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


async def put(ctx: Ctx, data: bytes = PNG, name: str = "hero.png") -> dict:
    return await io_tools.files_put(ctx, on=LANE, name=name, content_base64=b64(data))


# ---- files ----------------------------------------------------------------------------------

async def test_files_put_list_get_delete(env):
    runtime, lanes, _ = env
    ctx = Ctx(runtime)
    out = await put(ctx)
    file = out["file"]
    assert file["id"].startswith("in_") and file["name"] == "hero.png" and file["mime"] == "image/png"
    assert file["size"] == len(PNG) and file["status"] == "complete" and "owner_pid" not in file
    assert ctx.authorized[0]["effect"] is Effect.DRAFT
    assert ctx.authorized[0]["content"]["sha256"] == hashlib.sha256(PNG).hexdigest()
    assert lanes.used == []  # file tools never warm the browser
    stored = runtime.config.lane_dir(LANE) / "files" / "inbox" / file["id"] / "hero.png"
    assert stored.read_bytes() == PNG

    listing = await io_tools.files_list(ctx, on=LANE)
    assert [f["id"] for f in listing["inbox"]] == [file["id"]] and listing["outbox"] == []
    assert listing["limits"]["put"] == "200.0 MB"

    got = await io_tools.files_get(ctx, on=LANE, file_id=file["id"])
    assert isinstance(got, list) and got[0]["file"]["id"] == file["id"]
    assert isinstance(got[1], ToolImage) and got[1].data == PNG and got[1].mime_type == "image/png"

    deleted = await io_tools.files_delete(ctx, on=LANE, file_id=file["id"])
    assert deleted["deleted"]["id"] == file["id"] and not stored.exists()
    with pytest.raises(NotFound):
        await io_tools.files_get(ctx, on=LANE, file_id=file["id"])
    assert [k for k, _ in ctx.traces] == ["files_put", "files_delete"]


async def test_files_put_accepts_a_data_url_and_fixes_the_extension(env):
    runtime, _, _ = env
    out = await io_tools.files_put(Ctx(runtime), on=LANE, name="../../cover",
                                   content_base64="data:image/png;base64," + b64(PNG))
    assert out["file"]["name"] == "cover.png" and "browser_upload" in out["next"]


@pytest.mark.parametrize(("kwargs", "error"), [
    ({}, "exactly one of"),
    ({"content_base64": b64(PNG), "url": "https://example.com/a.png"}, "exactly one of"),
    ({"content_base64": "not base64!!"}, "not valid base64"),
    ({"content_base64": "data:image/png,rawtext"}, "base64 encoded"),
    ({"content_base64": b64(b"MZ\x90\x00" * 4)}, "the inbox takes only"),
    ({"content_base64": ""}, "empty"),
    ({"url": "http://example.com/a.png"}, "only https"),
    ({"url": "https://169.254.169.254/latest"}, "private network"),
])
async def test_files_put_refusals(env, kwargs, error):
    runtime, _, _ = env
    with pytest.raises(InvalidRequest, match=error):
        await io_tools.files_put(Ctx(runtime), on=LANE, name="a.png", **kwargs)
    assert LaneFiles(runtime.config.lane_dir(LANE) / "files").list() == []


async def test_files_put_size_is_checked_before_decoding(env, monkeypatch):
    runtime, _, _ = env
    monkeypatch.setattr(io_tools.FilesConfig, "from_node", classmethod(lambda cls, cfg: cls(max_bytes=1000)))
    with pytest.raises(InvalidRequest, match="larger than"):
        await io_tools.files_put(Ctx(runtime), on=LANE, name="a.pdf", content_base64=b64(PDF + b"0" * 2000))


async def test_files_put_url_uses_the_lane_proxy_and_never_goes_around_it(env, monkeypatch):
    runtime, lanes, _ = env
    lanes.add("acme/reddit", proxy_label="lt1")
    calls = []

    async def fake_put_url(self, name, url, *, proxy=None, policy=None):
        calls.append((name, url, proxy))
        return self.put_bytes(name, PNG, source="url", url=url)

    monkeypatch.setattr(LaneFiles, "put_url", fake_put_url)
    with pytest.raises(InvalidRequest, match="refusing to fetch without it"):
        await io_tools.files_put(Ctx(runtime), on="acme/reddit", name="a.png", url="https://cdn.example.com/a.png")
    assert calls == []

    save_proxies(runtime.config, {"lt1": ProxyConfig(label="lt1", host="203.0.113.7", port=3128, username="u",
                                                      password="secret-pass")})
    out = await io_tools.files_put(Ctx(runtime), on="acme/reddit", name="a.png", url="https://cdn.example.com/a.png")
    assert calls[0][2].host == "203.0.113.7"
    assert "secret-pass" not in str(out)
    out = await io_tools.files_put(Ctx(runtime), on=LANE, name="b.png", url="https://cdn.example.com/b.png")
    assert calls[1][2] is None  # a lane without a proxy fetches directly


async def test_files_get_returns_text_inline_and_large_files_as_a_path(env, monkeypatch):
    runtime, _, _ = env
    ctx = Ctx(runtime)
    text = await put(ctx, b"date,likes\n2026-11-01,42\n", "stats.csv")
    got = await io_tools.files_get(ctx, on=LANE, file_id=text["file"]["id"])
    assert got["text"] == "date,likes\n2026-11-01,42\n" and got["file"]["mime"] == "text/csv"

    pdf = await put(ctx, PDF, "terms.pdf")
    got = await io_tools.files_get(ctx, on=LANE, file_id=pdf["file"]["id"])
    assert got["path"].endswith("/terms.pdf") and got["fetch"].startswith("scp mynode:") and got["fetch"].endswith(" .")

    monkeypatch.setattr(io_tools.FilesConfig, "from_node", classmethod(lambda cls, cfg: cls(inline_image_bytes=10)))
    img = await put(ctx, PNG, "big picture.png")
    got = await io_tools.files_get(ctx, on=LANE, file_id=img["file"]["id"])
    assert isinstance(got, dict) and "path" in got and "'mynode:" in got["fetch"]  # quoted: the name has a space


async def test_files_get_of_an_unfinished_download_has_no_content(env):
    runtime, _, _ = env
    files = io_tools.lane_files(runtime, LANE)
    entry = files.begin_download("export.csv", "https://example.com/e.csv")
    got = await io_tools.files_get(Ctx(runtime), on=LANE, file_id=entry.id)
    assert got["file"]["status"] == "downloading" and "no content" in got["note"] and "path" not in got
    listing = await io_tools.files_list(Ctx(runtime), on=LANE)
    assert listing["outbox"][0]["status"] == "downloading"
    files.fail_download(entry.id, FileStatus.PARTIAL, "interrupted")
    got = await io_tools.files_get(Ctx(runtime), on=LANE, file_id=entry.id)
    assert got["file"]["status"] == "partial"


@pytest.mark.parametrize("tool", ["files_put", "files_list", "files_get", "files_delete"])
async def test_file_tools_check_the_lane(env, tool):
    runtime, lanes, _ = env
    lanes.add("acme/shell", kind=LaneKind.TERMINAL)
    args = {"files_put": {"name": "a.png", "content_base64": b64(PNG)}, "files_list": {},
            "files_get": {"file_id": "in_0123456789ab"}, "files_delete": {"file_id": "in_0123456789ab"}}[tool]
    handler = getattr(io_tools, tool)
    with pytest.raises(InvalidRequest, match="workspace/name"):
        await handler(Ctx(runtime), on="../etc", **args)
    with pytest.raises(LaneNotFound):
        await handler(Ctx(runtime), on="acme/nope", **args)
    with pytest.raises(InvalidRequest, match="terminal lane"):
        await handler(Ctx(runtime), on="acme/shell", **args)


async def test_a_paused_lane_takes_no_files_from_agents(env):
    runtime, lanes, _ = env
    lanes.add("acme/paused", control=LaneControl(state=ControlState.PAUSED, by="owner"))
    with pytest.raises(AgentPaused):
        await io_tools.files_put(Ctx(runtime), on="acme/paused", name="a.png", content_base64=b64(PNG))
    out = await io_tools.files_put(Ctx(runtime, kind="human"), on="acme/paused", name="a.png",
                                   content_base64=b64(PNG))
    assert out["file"]["status"] == "complete"


async def test_a_denied_put_stores_nothing(env):
    runtime, _, _ = env
    with pytest.raises(PolicyDenied):
        await put(Ctx(runtime, deny=PolicyDenied("no")))
    assert LaneFiles(runtime.config.lane_dir(LANE) / "files").list() == []


# ---- upload ---------------------------------------------------------------------------------

async def test_browser_upload_attaches_inbox_files_only(env):
    runtime, lanes, interaction = env
    ctx = Ctx(runtime)
    a = (await put(ctx, PNG, "a.png"))["file"]
    b = (await put(ctx, PNG, "b.png"))["file"]

    out = await io_tools.browser_upload(ctx, on=LANE, ref="e7", file_id=[a["id"], b["id"]])
    assert out["ok"] and out["file_ids"] == [a["id"], b["id"]] and out["via"] == "chooser"
    call = interaction.uploads[0]
    inbox = (runtime.config.lane_dir(LANE) / "files" / "inbox").resolve()
    assert call["root"].resolve() == inbox and all(p.is_relative_to(inbox) for p in call["paths"])
    assert call["mimes"] == ["image/png", "image/png"]
    auth = ctx.authorized[-1]
    assert auth["effect"] is Effect.DRAFT and auth["domain"] == "www.instagram.com"
    assert auth["content"] == [a["sha256"], b["sha256"]] and "a.png" in auth["summary"]
    assert lanes.used == [LANE]

    one = await io_tools.browser_upload(ctx, on=LANE, ref="e7", file_id=a["id"])
    assert one["file_ids"] == [a["id"]]


async def test_browser_upload_refusals(env):
    runtime, _, interaction = env
    ctx = Ctx(runtime)
    files = io_tools.lane_files(runtime, LANE)
    download = files.begin_download("x.png", None)
    with pytest.raises(InvalidRequest, match="is in the outbox"):
        await io_tools.browser_upload(ctx, on=LANE, ref="e1", file_id=download.id)
    src = runtime.config.home / "artifact.png"
    src.write_bytes(PNG)
    files.finish_download(download.id, src)
    with pytest.raises(InvalidRequest, match="only files in the lane's inbox"):
        await io_tools.browser_upload(ctx, on=LANE, ref="e1", file_id=download.id)
    with pytest.raises(InvalidRequest, match="not a file id"):
        await io_tools.browser_upload(ctx, on=LANE, ref="e1", file_id="../../etc/passwd")
    with pytest.raises(NotFound):
        await io_tools.browser_upload(ctx, on=LANE, ref="e1", file_id="in_0123456789ab")
    with pytest.raises(InvalidRequest, match="at least one"):
        await io_tools.browser_upload(ctx, on=LANE, ref="e1", file_id=[])
    with pytest.raises(InvalidRequest, match="at most 20"):
        await io_tools.browser_upload(ctx, on=LANE, ref="e1", file_id=["in_0123456789ab"] * 21)
    assert interaction.uploads == []


async def test_browser_upload_cannot_reach_another_lanes_inbox(env):
    runtime, lanes, interaction = env
    lanes.add("other/facebook")
    theirs = (await io_tools.files_put(Ctx(runtime), on="other/facebook", name="private.png",
                                       content_base64=b64(PNG)))["file"]
    with pytest.raises(NotFound):
        await io_tools.browser_upload(Ctx(runtime), on=LANE, ref="e1", file_id=theirs["id"])
    assert interaction.uploads == []


async def test_browser_upload_needs_an_interaction_that_uploads(env):
    runtime, _, _ = env
    a = (await put(Ctx(runtime)))["file"]
    runtime.register("interaction", object())
    with pytest.raises(ServiceUnavailable, match="cannot upload"):
        await io_tools.browser_upload(Ctx(runtime), on=LANE, ref="e1", file_id=a["id"])


# ---- tabs and dialogs -------------------------------------------------------------------------

async def test_browser_tabs_and_browser_tab(env):
    runtime, lanes, _ = env
    ctx = Ctx(runtime)
    main = await lanes.tracker.page()
    main.url = "https://www.instagram.com/"
    popup = lanes.context.open("https://accounts.example/oauth", opener=main)
    out = await io_tools.browser_tabs(ctx, on=LANE)
    assert [(t["index"], t["active"], t["opened_by"]) for t in out["tabs"]] == [(0, False, None), (1, True, 0)]
    assert ctx.authorized[-1]["effect"] is Effect.READ and ctx.authorized[-1]["domain"] == "accounts.example"

    out = await io_tools.browser_tab(ctx, on=LANE, index=0)
    assert out["active"] == 0 and out["tabs"][0]["active"] and lanes.tracker.active is main
    with pytest.raises(InvalidRequest, match="no tab 3"):
        await io_tools.browser_tab(ctx, on=LANE, index=3)
    assert popup in lanes.context.pages


async def test_browser_dialog_and_answer(env):
    runtime, lanes, _ = env
    ctx = Ctx(runtime)
    page = await lanes.tracker.page()
    page.url = "https://www.instagram.com/p/1/"
    assert (await io_tools.browser_dialog(ctx, on=LANE))["pending"] is None
    assert (await io_tools.browser_dialog_answer(ctx, on=LANE, accept=True))["answered"] is None

    dialog = open_dialog(page, "confirm", "Delete this post?")
    shown = await io_tools.browser_dialog(ctx, on=LANE)
    assert shown["pending"]["type"] == "confirm" and shown["pending"]["message"] == "Delete this post?"
    assert "dismissed in" in shown["pending"]["auto_answer"] and shown["others_pending"] == 0

    out = await io_tools.browser_dialog_answer(ctx, on=LANE, accept=True, text="ignored")
    assert dialog.answers == [("accept", None)]
    assert out["answered"]["outcome"] == "accepted" and "only used for a prompt" in out["note"]
    auth = ctx.authorized[-1]
    assert auth["effect"] is Effect.COMMIT and "Delete this post?" in auth["summary"]
    assert auth["domain"] == "www.instagram.com"
    assert (await io_tools.browser_dialog(ctx, on=LANE))["recent"][0]["outcome"] == "accepted"

    prompt = open_dialog(page, "prompt", "Caption?", default="")
    await io_tools.browser_dialog_answer(ctx, on=LANE, accept=True, text="Launch day")
    assert prompt.answers == [("accept", "Launch day")] and ctx.authorized[-1]["effect"] is Effect.DRAFT

    leave = open_dialog(page, "beforeunload", "")
    await io_tools.browser_dialog_answer(ctx, on=LANE, accept=False)
    assert leave.answers == [("dismiss", None)] and ctx.authorized[-1]["effect"] is Effect.DRAFT


async def test_an_approval_pending_holds_the_dialog_open(env):
    runtime, lanes, _ = env
    page = await lanes.tracker.page()
    dialog = open_dialog(page, "confirm", "Publish now?")
    entry = lanes.tracker.dialogs.current(page)
    before = entry.deadline
    with pytest.raises(ApprovalPending):
        await io_tools.browser_dialog_answer(Ctx(runtime, deny=ApprovalPending("ask", "ap_1")), on=LANE, accept=True)
    assert entry.pending and entry.deadline >= before + io_tools.APPROVAL_HOLD_SECONDS - 61
    assert dialog.answers == []


async def test_tab_and_dialog_tools_need_a_tracked_engine(env):
    runtime, lanes, _ = env

    class Bare:
        url = "https://example.com/"
        context = object()

    @asynccontextmanager
    async def using(lane_id):
        yield Bare()

    lanes.using = using
    with pytest.raises(ServiceUnavailable, match="tabs"):
        await io_tools.browser_tabs(Ctx(runtime), on=LANE)
    with pytest.raises(ServiceUnavailable, match="dialogs"):
        await io_tools.browser_dialog(Ctx(runtime), on=LANE)


# ---- specs ------------------------------------------------------------------------------------

def test_tool_specs_follow_the_contract(env):
    runtime, _, _ = env
    specs = io_tools.tool_specs(runtime)
    effects = {s.name: s.effect for s in specs}
    assert effects == {
        "files_put": Effect.DRAFT, "files_list": Effect.READ, "files_get": Effect.READ, "files_delete": Effect.DRAFT,
        "browser_upload": Effect.DRAFT, "browser_tabs": Effect.READ, "browser_tab": Effect.READ,
        "browser_dialog": Effect.READ, "browser_dialog_answer": Effect.DRAFT,
    }
    for spec in specs:
        assert TOOL_NAME_RE.match(spec.name) and spec.lane_arg == "on" and spec.description
        assert "—" not in spec.description
        params = inspect.signature(spec.handler).parameters
        assert next(iter(params)) == "ctx" and params["on"].kind is inspect.Parameter.KEYWORD_ONLY
        assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for name, p in params.items() if name != "ctx")
        assert inspect.iscoroutinefunction(spec.handler)


def test_limits_fit_the_default_node(env):
    runtime, _, _ = env
    files = io_tools.lane_files(runtime, LANE)
    assert files.config.max_bytes == 200 * MB and files.root == runtime.config.lane_dir(LANE) / "files"


@pytest.mark.parametrize(("route", "cleared"), [("approved", True), ("standing", True), ("auto", False)])
async def test_a_dialog_a_person_approved_is_not_held_by_ask_first(env, monkeypatch, route, cleared):
    runtime, lanes, _ = env
    ctx = Ctx(runtime)
    page = await lanes.tracker.page()
    page.url = "https://www.instagram.com/p/1/"
    open_dialog(page, "confirm", "Publish this post?")
    calls: list[str] = []
    monkeypatch.setattr(io_tools.egress, "guard_for",
                        lambda _: SimpleNamespace(person_approved=lambda: calls.append("approved")))

    async def decided(effect, **kw):
        return SimpleNamespace(route=route)

    ctx.authorize = decided
    await io_tools.browser_dialog_answer(ctx, on=LANE, accept=True)
    assert bool(calls) is cleared
