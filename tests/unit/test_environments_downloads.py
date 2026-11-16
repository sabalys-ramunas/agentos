"""Downloads off an environment (agentos.environments.downloads): environment_download's packing and its
one-use link, the viewer's GET route, the refusals, the clean up, the record of it, and the CLI.

The machine is the fake of test_environments, except that what it holds under /work is a folder here:
the download's path check runs this computer's shell on it, and the packing is the real
EnvManager.get_archive on a local lane pointed at that folder."""

from __future__ import annotations

import asyncio
import io
import os
import tarfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from test_environments import node, opened  # noqa: F401  (node is a fixture)
from typer.testing import CliRunner

from agentos.env.host import FakeHost
from agentos.env.lane_sync import ArchiveTooLarge
from agentos.env.manager import EnvManager
from agentos.env.models import ExecResult
from agentos.env.paths import EnvSettings as TermSettings
from agentos.env.sandbox import DirectLauncher
from agentos.environments import downloads
from agentos.environments.downloads import tool_specs as download_specs
from agentos.environments.uploads import tool_specs as upload_specs
from agentos.node.viewer_app import build_viewer_app
from agentos.node.viewer_tokens import ViewTokens
from agentos.policy.wrap import ToolCallError, bind_request_token, wrap_tool


class Clock:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def dl(node, tmp_path, monkeypatch):  # noqa: F811
    clock = Clock()
    tokens = ViewTokens(tmp_path / "viewer.key", clock=clock)
    rt, envs = node["rt"], node["envs"]
    rt.register("viewer_links", tokens)
    root = (tmp_path / "machine" / "work").resolve()
    root.mkdir(parents=True)
    envs.create("ws/bench", preset="workbench", node_secrets_ok=True)  # a local lane that packs the folder here
    fake_exec, real_get = envs.exec, envs.get_archive
    packs: list[tuple[str, str, list[str] | None]] = []

    async def exec_(lane_id, cmd, *, timeout_s=None, stdin=None, grant_id=None, max_bytes=None):
        if "readlink -f" not in cmd:
            return await fake_exec(lane_id, cmd, timeout_s=timeout_s, stdin=stdin, grant_id=grant_id)
        proc = await asyncio.create_subprocess_exec("sh", "-c", cmd.replace("/work", str(root)),
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await proc.communicate()
        return ExecResult(exit_code=proc.returncode, stdout=out.decode().replace(str(root), "/work"),
                          stderr=err.decode(), duration_s=0.01)

    async def get_archive(lane_id, src, dest_file, **kw):
        packs.append((lane_id, src, kw.get("paths")))
        assert src == "/work" or src.startswith("/work/"), src
        return await real_get("ws/bench", str(root) + src.removeprefix("/work"), dest_file, **kw)

    monkeypatch.setattr(envs, "exec", exec_)
    monkeypatch.setattr(envs, "get_archive", get_archive)
    wrapped = {s.name: wrap_tool(s, rt) for s in [*download_specs(rt), *upload_specs(rt)]}

    async def call(name="environment_download", *, session="chat-1", **kw):
        with bind_request_token(node["token"], session=session):
            return await wrapped[name](**kw)

    viewer = build_viewer_app(node["lanes"], rt.get("config"), tokens=tokens, environments=lambda: node["mgr"])

    async def fetch(url: str, method: str = "GET", body: bytes | None = None) -> httpx.Response:
        parts = urlsplit(url)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=viewer), base_url="http://localhost") as c:
            return await c.request(method, f"{parts.path}?{parts.query}", content=body)

    return {"call": call, "fetch": fetch, "root": root, "clock": clock, "tokens": tokens, "packs": packs,
            "viewer": viewer}


def members(body: bytes) -> dict[str, bytes | str]:
    """What a download holds: a file's bytes, a link's target ("-> target"), a folder's "/"."""
    out: dict[str, bytes | str] = {}
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tf:
        for m in tf:
            if m.isreg():
                out[m.name] = tf.extractfile(m).read()
            elif m.issym():
                out[m.name] = f"-> {m.linkname}"
            elif m.isdir():
                out[m.name] = "/"
    return out


def leftovers(n, env_id: str) -> list[str]:
    folder = n["mgr"].home / "environments" / env_id / "downloads"
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


async def settled(n):
    traces = n["rt"].get("traces")
    if getattr(traces, "deferred", False):
        await traces.settled()
    return traces


# ------------------------------------------------------------------ a file and a folder
async def test_a_file_comes_off_the_machine_once_with_safe_headers_and_is_recorded(node, dl):  # noqa: F811
    out = await opened(node)
    (dl["root"] / "report.txt").write_text("sales are up\n")
    got = await dl["call"](path="report.txt")
    assert got.get("status") != "approval_pending", got  # its own environment never waits for a person
    assert got["path"] == "/work/report.txt" and got["kind"] == "file" and got["files"] == 1
    assert got["url"].startswith(f"http://localhost:{node['rt'].get('config').mcp_port}/view/env/{out['id']}/download?t=")
    assert got["fetch"] == f'curl --fail-with-body -o out.tgz "{got["url"]}"' and got["unpack"] == "tar -xzf out.tgz"
    assert got["fetch"] in got["next"] and "tar -xzf out.tgz" in got["next"] and "—" not in got["next"]
    assert dl["packs"] == [(f"env/{out['id']}", "/work", ["report.txt"])]
    assert len(leftovers(node, out["id"])) == 1  # packed, waiting on the node

    res = await dl["fetch"](got["url"])
    assert res.status_code == 200, res.text
    assert res.headers["content-type"] == "application/gzip"
    assert res.headers["content-disposition"] == 'attachment; filename="report.txt.tgz"'
    assert "no-store" in res.headers["cache-control"] and res.headers["x-content-type-options"] == "nosniff"
    assert int(res.headers["content-length"]) == len(res.content) == got["bytes"]
    assert members(res.content) == {"./report.txt": b"sales are up\n"}
    assert leftovers(node, out["id"]) == []  # deleted once served

    state = await node["call"]("environment")
    tools = [c["tool"] for c in state["calls"]]
    assert tools[:2] == ["download", "environment_download"], tools  # newest first
    assert "/work/report.txt" in state["calls"][0]["summary"] and state["calls"][0]["ok"]
    [row], _ = (await settled(node)).query(tool="environment.download")
    assert row.status == "ok" and row.lane == f"env/{out['id']}" and row.grant_id == node["grant"].id
    assert row.args["path"] == "/work/report.txt" and row.args["sha256"] == got["sha256"] and row.args["by"] == "agent"


async def test_a_folder_comes_whole_and_a_link_inside_it_stays_a_link(node, dl, tmp_path):  # noqa: F811
    out = await opened(node, repo="example.test/me/shop")
    dist = dl["root"] / "shop" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<h1>sale</h1>\n")
    (dist / "assets" / "app.js").write_text("sale()\n")
    secret = tmp_path / "outside" / "secret.txt"
    secret.parent.mkdir()
    secret.write_text("never leaves\n")
    (dist / "peek").symlink_to(secret)  # packed as a link, never followed
    got = await dl["call"](path="dist")  # relative: starts at the workdir, /work/shop
    assert got["path"] == "/work/shop/dist" and got["kind"] == "folder" and got["files"] == 2
    assert "the folder dist with its 2 files" in got["next"]
    assert dl["packs"][-1] == (f"env/{out['id']}", "/work/shop", ["dist"])
    res = await dl["fetch"](got["url"])
    assert res.status_code == 200 and res.headers["content-disposition"] == 'attachment; filename="dist.tgz"'
    held = members(res.content)
    assert held["./dist/index.html"] == b"<h1>sale</h1>\n" and held["./dist/assets/app.js"] == b"sale()\n"
    assert held["./dist/peek"] == f"-> {secret}"
    assert b"never leaves" not in res.content and not any(b"never leaves" in v for v in held.values()
                                                            if isinstance(v, bytes))


async def test_all_of_work_comes_with_its_contents_at_the_top(node, dl):  # noqa: F811
    await opened(node)
    (dl["root"] / "a.txt").write_text("a\n")
    got = await dl["call"](path="/work")
    assert got["kind"] == "folder" and dl["packs"][-1][1:] == ("/work", None)
    assert "./a.txt" in members((await dl["fetch"](got["url"])).content)


# ------------------------------------------------------------------ what it refuses
@pytest.mark.parametrize("bad", ["/etc/passwd", "../../etc", "/work/../root", "/", "/workshop/x"])
async def test_a_path_outside_work_is_refused(node, dl, bad):  # noqa: F811
    await opened(node)
    with pytest.raises(ToolCallError, match="outside /work"):
        await dl["call"](path=bad)
    assert dl["packs"] == []


async def test_a_symlink_that_leaves_work_is_refused(node, dl, tmp_path):  # noqa: F811
    out = await opened(node)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "passwd").write_text("root:x:0:0\n")
    (dl["root"] / "escape").symlink_to(outside)
    (dl["root"] / "inside.txt").write_text("in\n")
    (dl["root"] / "alias.txt").symlink_to(dl["root"] / "inside.txt")
    for path in ("escape", "/work/escape/passwd"):
        with pytest.raises(ToolCallError, match="outside /work"):
            await dl["call"](path=path)
    assert dl["packs"] == [] and leftovers(node, out["id"]) == []
    got = await dl["call"](path="alias.txt")  # a link that stays in /work: what it names is packed
    assert dl["packs"][-1][1:] == ("/work", ["inside.txt"])
    assert members((await dl["fetch"](got["url"])).content) == {"./inside.txt": b"in\n"}


async def test_a_missing_path_says_so(node, dl):  # noqa: F811
    await opened(node)
    with pytest.raises(ToolCallError, match="does not exist"):
        await dl["call"](path="nope.txt")


async def test_past_the_cap_it_stops_and_keeps_no_partial_file(node, dl, monkeypatch):  # noqa: F811
    out = await opened(node)
    (dl["root"] / "big.bin").write_bytes(os.urandom(200_000))
    monkeypatch.setattr(downloads, "DOWNLOAD_BYTES_MAX", 20_000)
    with pytest.raises(ToolCallError, match="over 20000 bytes"):
        await dl["call"](path="big.bin")
    assert leftovers(node, out["id"]) == []
    assert node["mgr"]._download_links() == {}


async def test_a_download_needs_the_chats_own_environment(node, dl):  # noqa: F811
    with pytest.raises(ToolCallError, match="no_environment"):
        await dl["call"](path="a.txt")
    await opened(node)
    with pytest.raises(ToolCallError, match="environment_download takes files"):
        await dl["call"](on="dev/other", path="a.txt")


# ------------------------------------------------------------------ the link
async def test_a_link_works_once_and_a_tampered_expired_or_swapped_one_is_refused(node, dl):  # noqa: F811
    out = await opened(node)
    (dl["root"] / "a.txt").write_text("a\n")
    first = await dl["call"](path="a.txt")
    assert (await dl["fetch"](first["url"].replace("?t=", "?t=x"))).status_code == 403  # tampered
    head = await dl["fetch"](first["url"], method="HEAD")
    assert head.status_code == 405  # a HEAD never spends it
    assert (await dl["fetch"](first["url"])).status_code == 200
    again = await dl["fetch"](first["url"])
    assert again.status_code == 403 and "used already" in again.json()["error"]["message"]
    # its fix is about download links, never the live view's "A watch link only watches"
    fix = again.json()["error"]["fix"]
    assert "environment_download" in fix and "watch" not in fix and "—" not in fix

    other = await dl["call"](path="a.txt")
    wrong_env = other["url"].replace(f"/env/{out['id']}/", "/env/eother1234/")
    assert (await dl["fetch"](wrong_env)).status_code == 403
    # the same token on the upload route is no upload link, and an upload link is no download link
    as_upload = other["url"].replace("/download?", "/upload?")
    assert (await dl["fetch"](as_upload, method="PUT", body=b"x")).status_code == 403
    up = await dl["call"]("environment_upload")
    as_download = up["url"].replace("/upload?", "/download?")
    assert (await dl["fetch"](as_download)).status_code == 403
    view = dl["tokens"].issue(f"env/{out['id']}-browser", "control").token  # a live view link opens nothing here
    assert (await dl["fetch"](f"http://localhost{urlsplit(other['url']).path}?t={view}")).status_code == 403

    dl["clock"].now += downloads.DOWNLOAD_LINK_S + 1
    assert (await dl["fetch"](other["url"])).status_code == 403  # expired
    assert node["mgr"].sweep_downloads() == 1 and leftovers(node, out["id"]) == []  # the reaper's pass


async def test_an_environment_holds_a_few_unfetched_downloads_at_most(node, dl):  # noqa: F811
    await opened(node)
    (dl["root"] / "a.txt").write_text("a\n")
    for _ in range(downloads.LINKS_OPEN_MAX):
        await dl["call"](path="a.txt")
    with pytest.raises(ToolCallError, match="nobody fetched yet"):
        await dl["call"](path="a.txt")


async def test_closing_ends_the_links_and_deletes_the_files(node, dl):  # noqa: F811
    out = await opened(node)
    (dl["root"] / "a.txt").write_text("a\n")
    got = await dl["call"](path="a.txt")
    assert leftovers(node, out["id"]) != []
    await node["call"]("environment_close", discard=True)
    assert leftovers(node, out["id"]) == [] and node["mgr"]._download_links() == {}
    res = await dl["fetch"](got["url"])
    assert res.status_code == 403
    with pytest.raises(ToolCallError, match="no_environment"):
        await dl["call"](path="a.txt")


async def test_a_restart_leaves_no_archive_behind(node, dl):  # noqa: F811
    out = await opened(node)
    (dl["root"] / "a.txt").write_text("a\n")
    await dl["call"](path="a.txt")
    node["mgr"]._download_links().clear()  # what a restart does to the links in memory
    await node["mgr"].reap_once()
    assert leftovers(node, out["id"]) == []


# ------------------------------------------------------------------ the person's side
def _bridge(loop: asyncio.AbstractEventLoop, apps: dict[str, object]) -> httpx.MockTransport:
    """A sync transport for the CLI (it runs in a thread) that answers from the ASGI apps on the test's loop."""

    def answer(request: httpx.Request) -> httpx.Response:
        app = next(a for prefix, a in apps.items() if request.url.path.startswith(prefix))

        async def go() -> httpx.Response:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as c:
                r = await c.request(request.method, request.url.raw_path.decode(), headers=request.headers,
                                    content=request.content)
                return httpx.Response(r.status_code, headers=r.headers, content=r.content)

        return asyncio.run_coroutine_threadsafe(go(), loop).result(30)

    return httpx.MockTransport(answer)


@pytest.fixture
def cli(node, dl, monkeypatch):  # noqa: F811
    from agentos.api import ApiSettings, build_api
    from agentos.core import config as config_mod
    from agentos.environments import cli as env_cli

    home = node["mgr"].home
    api = build_api(node["rt"], ApiSettings(lane_pump_seconds=0, panel_dir=home / "no-panel"))
    cfg = node["rt"].get("config")
    toml = home / "node.toml"
    toml.write_text("")
    monkeypatch.setattr(config_mod, "config_path", lambda h=None: toml)
    monkeypatch.setattr(config_mod, "load_config", lambda h=None: cfg)
    apps = {"/api/": api, "/view/": dl["viewer"]}

    async def run(*args: str):
        transport = _bridge(asyncio.get_running_loop(), apps)
        monkeypatch.setattr(env_cli, "_http_client", lambda: httpx.Client(transport=transport))
        return await asyncio.to_thread(CliRunner().invoke, env_cli.app, ["--home", str(home), *args])

    return {"run": run, "api": api}


async def test_the_cli_copies_a_folder_into_a_dir_without_the_agent(node, dl, cli, tmp_path):  # noqa: F811
    out = await opened(node, repo="example.test/me/shop")
    dist = dl["root"] / "shop" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<h1>sale</h1>\n")
    (dist / "up").symlink_to("/etc/passwd")  # a link that points outside: skipped, never made here
    into = tmp_path / "here"
    res = await cli["run"]("download", out["id"], "dist", "--to", str(into))
    assert res.exit_code == 0, res.output
    assert (into / "dist" / "index.html").read_text() == "<h1>sale</h1>\n"
    assert not (into / "dist" / "up").exists() and not (into / "dist" / "up").is_symlink()
    assert "Copied the folder /work/shop/dist" in res.output and "1 file." in res.output and "Skipped 1 link" in res.output
    assert sorted(p.name for p in into.iterdir()) == ["dist"]  # the temp archive is gone
    assert leftovers(node, out["id"]) == []
    [row], _ = (await settled(node)).query(tool="environment.download")
    assert row.args["by"] == "owner" and row.principal == "owner"

    res = await cli["run"]("download", out["id"], "/etc/passwd", "--to", str(into))
    assert res.exit_code == 1 and "outside /work" in res.output
    res = await cli["run"]("download", "enope0000", "a.txt")
    assert res.exit_code == 1 and "No environment" in res.output


async def test_the_api_route_is_the_owners_only(node, dl, cli):  # noqa: F811
    out = await opened(node)
    (dl["root"] / "a.txt").write_text("a\n")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=cli["api"]), base_url="http://localhost") as c:
        path = f"/api/environments/{out['id']}/download"
        r = await c.post(path, json={"path": "a.txt"}, headers={"Authorization": f"Bearer {node['token']}"})
        assert r.status_code in (401, 403), r.text  # the agent's grant uses environment_download instead
        assert dl["packs"] == []
        owner = node["rt"].get("config").mcp_token
        r = await c.post(path, json={"path": "a.txt"}, headers={"Authorization": f"Bearer {owner}"})
        assert r.status_code == 200, r.text
        assert r.json()["kind"] == "file" and "/download?t=" in r.json()["url"]


# ------------------------------------------------------------------ the machine's side
async def test_get_archive_packs_a_folder_or_paths_over_the_lanes_transport(tmp_path):
    """EnvManager.get_archive streams a workload's tar into a file on the node (here a local lane; on a VM
    lane the same workload runs over SSH), refuses paths that leave the folder, and stops at max_bytes."""
    envs = EnvManager(tmp_path / "home", host=FakeHost(), launcher=DirectLauncher(),
                      settings=TermSettings(proxy_port=0, install_proxy_port=0))
    envs.create("ws/bench", preset="workbench", node_secrets_ok=True)
    try:
        src = tmp_path / "src"
        (src / "pkg").mkdir(parents=True)
        (src / "pkg" / "a.txt").write_text("a\n")
        (src / "-rf").write_text("a name that looks like an option\n")
        (src / "link").symlink_to("pkg/a.txt")
        whole = await envs.get_archive("ws/bench", str(src), tmp_path / "whole.tgz")
        assert whole["files"] == 2 and whole["bytes"] == (tmp_path / "whole.tgz").stat().st_size
        held = members((tmp_path / "whole.tgz").read_bytes())
        assert held["./pkg/a.txt"] == b"a\n" and held["./link"] == "-> pkg/a.txt" and "./-rf" in held
        assert oct((tmp_path / "whole.tgz").stat().st_mode & 0o777) == "0o600"

        some = await envs.get_archive("ws/bench", str(src), tmp_path / "some.tar", paths=["-rf", "pkg/a.txt"],
                                      gzip=False)
        assert some["files"] == 2
        with tarfile.open(tmp_path / "some.tar", "r:") as tf:
            assert sorted(tf.getnames()) == ["./-rf", "./pkg/a.txt"]

        for bad in (["/etc/passwd"], ["../x"], ["pkg/../../x"], [""], []):
            with pytest.raises(Exception, match="absolute|\\.\\.|not a path|list of"):
                await envs.get_archive("ws/bench", str(src), tmp_path / "bad.tgz", paths=bad)
        assert not (tmp_path / "bad.tgz").exists()

        (src / "big.bin").write_bytes(os.urandom(300_000))
        with pytest.raises(ArchiveTooLarge, match="over 50000 bytes"):
            await envs.get_archive("ws/bench", str(src), tmp_path / "big.tgz", max_bytes=50_000)
        assert not (tmp_path / "big.tgz").exists()

        with pytest.raises(Exception, match="tar could not pack"):
            await envs.get_archive("ws/bench", str(src), tmp_path / "gone.tgz", paths=["not-there"])
        assert not (tmp_path / "gone.tgz").exists()
        (tmp_path / "taken.tgz").write_text("")
        with pytest.raises(FileExistsError):  # never writes through a file that is there already
            await envs.get_archive("ws/bench", str(src), tmp_path / "taken.tgz")
    finally:
        await envs.shutdown()


def test_the_attachment_name_is_safe():
    assert downloads.attachment_name("/work/shop/dist") == "dist.tgz"
    assert downloads.attachment_name('/work/a "b";\r\nc.txt') == "a_b_c.txt.tgz"
    assert downloads.attachment_name("/work") == "work.tgz"
    assert downloads.attachment_name("/work/...") == "work.tgz"


def test_unpack_skips_what_leaves_the_folder(tmp_path):
    from agentos.environments.cli import unpack

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, kind, link, data in (("./ok.txt", tarfile.REGTYPE, "", b"ok"), ("../evil.txt", tarfile.REGTYPE, "", b"x"),
                                       ("./abs", tarfile.SYMTYPE, "/etc/passwd", None)):
            info = tarfile.TarInfo(name)
            info.type, info.linkname = kind, link
            if data is not None:
                info.size = len(data)
            tf.addfile(info, io.BytesIO(data) if data is not None else None)
    archive = tmp_path / "a.tgz"
    archive.write_bytes(buf.getvalue())
    files, skipped = unpack(archive, tmp_path / "into")
    assert files == ["ok.txt"] and sorted(skipped) == ["../evil.txt", "./abs"]
    assert not (tmp_path / "evil.txt").exists() and not Path(tmp_path / "into" / "abs").is_symlink()
