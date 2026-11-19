"""file_read, file_write and file_edit on a real lane (a workbench lane running here),
and terminal_exec's and terminal_job_read's sizes (QA 2026-11-15, P2-1 and P2-8)."""

import asyncio
import inspect
import shlex
import sys
import time

import pytest

from agentos.core.errors import InvalidRequest, NotFound
from agentos.core.models import Effect, Principal
from agentos.core.runtime import Runtime
from agentos.core.tools import TOOL_NAME_RE
from agentos.env import files
from agentos.env import tool_specs as term_specs
from agentos.env.execs import Capture, encode_output, fit
from agentos.env.host import FakeHost
from agentos.env.manager import EnvManager
from agentos.env.paths import EnvSettings
from agentos.env.sandbox import DirectLauncher


class Ctx:
    def __init__(self, runtime):
        self.runtime = runtime
        self.principal = Principal(id="test-agent")
        self.calls: list[dict] = []
        self.traces: list[tuple] = []

    async def authorize(self, effect, *, lane_id=None, domain=None, summary=None, content=None, facts=None):
        self.calls.append({"effect": effect, "lane_id": lane_id, "summary": summary, "content": content,
                           "facts": facts})

    def trace(self, kind, **fields):
        self.traces.append((kind, fields))


class Environments:
    """The environments service as the file tools ask it: where a relative path starts."""

    def __init__(self, workdir):
        self.workdir = str(workdir)

    def workdir_of(self, lane_id):
        return self.workdir if lane_id == "ws/bench" else None


@pytest.fixture
async def lane(tmp_path):
    mgr = EnvManager(tmp_path / "home", host=FakeHost(), launcher=DirectLauncher(),
                     settings=EnvSettings(proxy_port=0, max_output_bytes=4000))
    mgr.create("ws/bench", preset="workbench", node_secrets_ok=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    runtime = Runtime(envs=mgr, environments=Environments(repo))
    yield Ctx(runtime), repo
    await mgr.shutdown()


def tool(name):
    return {s.name: s for s in files.tool_specs(None)}[name].handler


def test_the_file_tools_are_well_formed_terminal_tools():
    from agentos.core.toolsets import TOOLSETS
    from agentos.environments.routing import TERMINAL

    core = next(t for t in TOOLSETS if t.name == "core").tools
    for spec in files.tool_specs(None):
        assert TOOL_NAME_RE.match(spec.name) and "\u2014" not in spec.description  # the effect: in the annotations
        params = list(inspect.signature(spec.handler).parameters.values())
        assert params[0].name == "ctx" and params[1].name == "on"
        assert spec.effect is Effect.EXEC and spec.hidden_without == "envs"
        assert spec.name in TERMINAL and spec.name in core  # without `on`: the chat's environment


async def test_write_then_read_with_line_numbers_relative_to_the_repository(lane):
    ctx, repo = lane
    out = await tool("file_write")(ctx, on="ws/bench", path="src/app.py", content="a = 1\nb = 'two'\n")
    assert out == {"path": f"{repo}/src/app.py", "state": "created", "bytes": 16, "lines": 2}
    assert (repo / "src/app.py").read_text() == "a = 1\nb = 'two'\n"  # the quote went through as it is
    assert ctx.calls[-1]["effect"] is Effect.EXEC and ctx.calls[-1]["facts"] is not None
    read = await tool("file_read")(ctx, on="ws/bench", path="src/app.py")
    assert read["content"] == "     1\ta = 1\n     2\tb = 'two'"
    assert (read["lines"], read["total_lines"], read["size"]) == ("1-2", 2, 16) and "next_offset" not in read
    again = await tool("file_write")(ctx, on="ws/bench", path=f"{repo}/src/app.py", content="x")
    assert again["state"] == "updated" and (repo / "src/app.py").read_text() == "x"


async def test_read_pages_with_offset_and_limit_and_counts_a_last_line_without_newline(lane):
    ctx, repo = lane
    (repo / "long.txt").write_text("\n".join(f"line {i}" for i in range(1, 51)))
    out = await tool("file_read")(ctx, on="ws/bench", path="long.txt", offset=10, limit=5)
    assert out["lines"] == "10-14" and out["next_offset"] == 15 and out["total_lines"] == 50
    assert out["content"].splitlines()[0] == "    10\tline 10"
    end = await tool("file_read")(ctx, on="ws/bench", path="long.txt", offset=49)
    assert end["lines"] == "49-50" and "next_offset" not in end
    past = await tool("file_read")(ctx, on="ws/bench", path="long.txt", offset=99)
    assert past["lines"] is None and "past its end" in past["note"]


async def test_a_long_line_is_cut_and_a_large_file_stops_at_a_whole_line(lane):
    ctx, repo = lane
    (repo / "wide.txt").write_text("x" * 5000 + "\nshort\n")
    out = await tool("file_read")(ctx, on="ws/bench", path="wide.txt")
    first = out["content"].splitlines()[0]
    assert first.endswith("[agentos: 3000 more characters on this line]") and "short" in out["content"]
    (repo / "big.txt").write_text("".join(f"{i:05d} " + "y" * 90 + "\n" for i in range(3000)))
    big = await tool("file_read")(ctx, on="ws/bench", path="big.txt")
    shown = big["content"].splitlines()
    assert len(big["content"]) <= files.READ_CHARS + 1000 and big["next_offset"] == len(shown) + 1
    assert shown[-1].split("\t")[1].startswith(f"{len(shown) - 1:05d} ")  # whole lines, in order


async def test_paths_reads_several_files_and_a_pattern_in_one_call(lane):
    """An agent that wants a small repository reads it in one call, not with a cat loop in the terminal
    (the evals, 2026-11-18: Haiku did `for f in src/* test/*; do cat "$f"; done` with file_read listed)."""
    ctx, repo = lane
    (repo / "src").mkdir()
    (repo / "src/a.js").write_text("const a = 1;\n")
    (repo / "src/b.js").write_text("const b = 2;\n")
    (repo / "src/notes.md").write_text("# notes\n")
    (repo / "package.json").write_text('{"name": "shop"}\n')
    out = await tool("file_read")(ctx, on="ws/bench", paths=["package.json", "src/*.js", "nope.txt", "test/*"])
    got = {f["path"].removeprefix(f"{repo}/"): f for f in out["files"]}
    assert list(got) == ["package.json", "src/a.js", "src/b.js", "nope.txt"]
    assert got["src/b.js"]["content"] == "     1\tconst b = 2;" and got["package.json"]["total_lines"] == 1
    assert got["nope.txt"]["error"].startswith("not_found: no file")
    assert out["no_match"] == [f"{repo}/test/*"] and "not_read" not in out
    assert len(ctx.calls) == 1 and ctx.calls[0]["effect"] is Effect.EXEC  # one approval for the call
    with pytest.raises(InvalidRequest, match="give path"):
        await tool("file_read")(ctx, on="ws/bench")


async def test_paths_stops_at_the_reply_budget_and_names_what_is_left(lane):
    ctx, repo = lane
    for i in range(3):
        (repo / f"big{i}.txt").write_text("".join("z" * 90 + "\n" for _ in range(300)))
    out = await tool("file_read")(ctx, on="ws/bench", paths=[f"big{i}.txt" for i in range(3)])
    assert len(out["files"]) == 2 and out["not_read"] == [f"{repo}/big2.txt"]
    assert "next_offset" not in out["files"][0]
    assert "next_offset" in out["files"][-1] and "file_read(paths=" in out["note"]


async def test_binary_missing_and_folders_are_refused_with_what_they_are(lane):
    ctx, repo = lane
    (repo / "img.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
    (repo / "latin.txt").write_bytes("caf\xe9\n".encode("latin-1"))
    with pytest.raises(InvalidRequest, match="binary file"):
        await tool("file_read")(ctx, on="ws/bench", path="img.png")
    with pytest.raises(InvalidRequest, match="not UTF-8"):
        await tool("file_read")(ctx, on="ws/bench", path="latin.txt")
    with pytest.raises(NotFound, match="no file"):
        await tool("file_read")(ctx, on="ws/bench", path="nope.txt")
    with pytest.raises(InvalidRequest, match="is a folder"):
        await tool("file_read")(ctx, on="ws/bench", path=".")
    with pytest.raises(InvalidRequest, match="binary file"):
        await tool("file_edit")(ctx, on="ws/bench", path="img.png", old="PNG", new="GIF")


async def test_edit_replaces_one_exact_match_and_refuses_several_or_none(lane):
    ctx, repo = lane
    (repo / "app.js").write_text("const a = 1;\nconst b = 1;\nconsole.log(a, b);\n")
    with pytest.raises(InvalidRequest, match="occurs 2 times"):
        await tool("file_edit")(ctx, on="ws/bench", path="app.js", old="= 1;", new="= 2;")
    assert (repo / "app.js").read_text().count("= 1;") == 2  # nothing changed
    with pytest.raises(InvalidRequest, match="was not found"):
        await tool("file_edit")(ctx, on="ws/bench", path="app.js", old="const c", new="const d")
    out = await tool("file_edit")(ctx, on="ws/bench", path="app.js", old="const b = 1;", new="const b = '$HOME';")
    assert out["replaced"] == 1 and "     2\tconst b = '$HOME';" in out["snippet"]
    assert (repo / "app.js").read_text() == "const a = 1;\nconst b = '$HOME';\nconsole.log(a, b);\n"
    every = await tool("file_edit")(ctx, on="ws/bench", path="app.js", old="const", new="let", replace_all=True)
    assert every["replaced"] == 2 and (repo / "app.js").read_text().startswith("let a = 1;\nlet b")
    with pytest.raises(InvalidRequest, match="the same"):
        await tool("file_edit")(ctx, on="ws/bench", path="app.js", old="let", new="let")


async def test_an_edit_does_not_write_over_a_file_that_changed_meanwhile(lane, monkeypatch):
    ctx, repo = lane
    if not any((p / "sha256sum").exists() for p in map(__import__("pathlib").Path, ["/usr/bin", "/bin",
                                                                                      "/usr/local/bin", "/opt/homebrew/bin"])):
        pytest.skip("no sha256sum here (the machines have it)")
    target = repo / "race.txt"
    target.write_text("one\n")
    real = files._run

    async def run(ctx_, mgr, on, script, **kw):
        if kw.get("stdin") is not None:  # the write: someone changed the file just before it
            target.write_text("someone else\n")
        return await real(ctx_, mgr, on, script, **kw)

    monkeypatch.setattr(files, "_run", run)
    with pytest.raises(InvalidRequest, match="changed on the machine"):
        await tool("file_edit")(ctx, on="ws/bench", path="race.txt", old="one", new="two")
    assert target.read_text() == "someone else\n"


@pytest.mark.parametrize("path, workdir, want", [
    ("src/a.py", "/work/shop", "/work/shop/src/a.py"),
    ("/etc/hosts", "/work/shop", "/etc/hosts"),
    ("../x/./y", "/work/shop", "/work/x/y"),
    ("a.txt", None, "a.txt"),
])
def test_paths_resolve_against_the_repository(path, workdir, want):
    assert files.resolve_path(path, workdir) == want


@pytest.mark.parametrize("path", ["", "a\nb", "~/x", "x\x00"])
def test_bad_paths_are_refused(path):
    with pytest.raises(InvalidRequest):
        files.resolve_path(path, "/work")


async def test_a_large_write_is_refused_before_anything_runs(lane):
    ctx, _ = lane
    with pytest.raises(InvalidRequest, match="at most"):
        await tool("file_write")(ctx, on="ws/bench", path="x", content="a" * (files.WRITE_MAX + 1))
    assert ctx.calls == []


# ------------------------------------------------------------------ terminal_exec and terminal_job_read sizes

def test_fit_keeps_both_streams_within_the_budget_and_stderr_its_share():
    out, err = Capture(100_000), Capture(100_000)
    out.feed(b"o" * 90_000)
    err.feed(b"e" * 30_000)
    o, e = fit(out, err, 16_000)
    kept = lambda c: len(c.head) + len(c.tail)
    assert kept(o) + kept(e) <= 16_000 and kept(e) >= 4000 and o.total == 90_000 and o.dropped == 90_000 - kept(o)
    text, _ = encode_output(o)
    assert "bytes truncated here, of 90000 in all" in text
    small_out, small_err = Capture(100), Capture(100)
    small_out.feed(b"hi")
    assert fit(small_out, small_err, 16_000) == (small_out, small_err)


async def test_lane_exec_returns_16_kb_by_default_and_max_bytes_more(tmp_path):
    mgr = EnvManager(tmp_path / "home", host=FakeHost(), launcher=DirectLauncher(),
                     settings=EnvSettings(proxy_port=0))
    mgr.create("ws/bench", preset="workbench", node_secrets_ok=True)
    try:
        ctx = Ctx(Runtime(envs=mgr))
        exec_ = {s.name: s for s in term_specs(ctx.runtime)}["terminal_exec"].handler
        cmd = f"{shlex.quote(sys.executable)} -c \"print('x' * 200000)\""
        out = await exec_(ctx, on="ws/bench", cmd=cmd)
        assert out["truncated"] and out["stdout_bytes"] == 200001 and len(out["stdout"]) < 16_500
        more = await exec_(ctx, on="ws/bench", cmd=cmd, max_bytes=60_000)
        assert 59_000 < len(more["stdout"]) < 60_500
        with pytest.raises(InvalidRequest):
            await exec_(ctx, on="ws/bench", cmd="true", max_bytes="lots")
    finally:
        await mgr.shutdown()


async def test_lane_job_read_until_exit_is_one_read_for_a_short_job(tmp_path):
    mgr = EnvManager(tmp_path / "home", host=FakeHost(), launcher=DirectLauncher(),
                     settings=EnvSettings(proxy_port=0))
    mgr.create("ws/bench", preset="workbench", node_secrets_ok=True)
    try:
        ctx = Ctx(Runtime(envs=mgr))
        by = {s.name: s.handler for s in term_specs(ctx.runtime)}
        job = await by["terminal_job_start"](ctx, on="ws/bench", cmd="for i in 1 2 3; do echo tick $i; sleep 0.3; done; exit 3")
        started = time.monotonic()
        out = await by["terminal_job_read"](ctx, job=job["id"], wait_s=10, until_exit=True)
        assert out["state"] == "exited" and out["exit_code"] == 3 and out["stdout"].count("tick") == 3
        assert time.monotonic() - started < 5
        job2 = await by["terminal_job_start"](ctx, on="ws/bench", cmd="echo first; sleep 2; echo second")
        await asyncio.sleep(0.3)
        early = await by["terminal_job_read"](ctx, job=job2["id"], wait_s=5)  # without it: the first output
        assert early["state"] == "running" and "second" not in early["stdout"]
        await by["terminal_job_cancel"](ctx, job=job2["id"])
    finally:
        await mgr.shutdown()


async def test_the_person_approving_a_write_or_an_edit_sees_its_text_not_only_its_hash(lane):
    """On a lane that reaches the node a write waits for a person: the card shows where it runs first,
    then the start of what goes in (an edit: what it replaces with what), clipped, line breaks shown."""
    ctx, repo = lane
    await tool("file_write")(ctx, on="ws/bench", path="keys.txt", content="ssh-ed25519 AAAA me\n" + "x" * 500)
    card = ctx.calls[-1]["summary"]
    assert card.startswith(f"Write 520 bytes to {repo}/keys.txt on the node itself")
    assert 'Text: "ssh-ed25519 AAAA me\\nxxx' in card and "(+280 more characters)" in card
    assert ctx.calls[-1]["content"]["content"].startswith("sha256:")  # the approval binds the whole text
    await tool("file_edit")(ctx, on="ws/bench", path="keys.txt", old="AAAA me", new="BBBB you")
    assert ctx.calls[-1]["summary"].endswith('Replace "AAAA me" with "BBBB you"')
