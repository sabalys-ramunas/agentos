import inspect

import pytest

from openajar.core.errors import InvalidRequest, NotFound, PolicyDenied
from openajar.core.models import Effect, Principal
from openajar.core.runtime import Runtime
from openajar.core.tools import TOOL_NAME_RE
from openajar.env import tool_specs
from openajar.env.host import FakeHost
from openajar.env.manager import EnvManager
from openajar.env.paths import EnvSettings
from openajar.env.sandbox import DirectLauncher


class Ctx:
    def __init__(self, runtime, deny: set[Effect] | None = None):
        self.runtime = runtime
        self.principal = Principal(id="test-agent")
        self.calls: list[dict] = []
        self.traces: list[tuple] = []
        self.deny = deny or set()

    async def authorize(self, effect, *, lane_id=None, domain=None, summary=None, content=None, facts=None):
        self.calls.append({"effect": effect, "lane_id": lane_id, "summary": summary, "content": content,
                           "facts": facts})
        if effect in self.deny:
            raise PolicyDenied(f"{effect} denied")

    def trace(self, kind, **fields):
        self.traces.append((kind, fields))


@pytest.fixture
async def rt(tmp_path):
    mgr = EnvManager(tmp_path / "home", host=FakeHost(), launcher=DirectLauncher(),
                     settings=EnvSettings(proxy_port=0))
    mgr.create("ws/bench", preset="workbench", node_secrets_ok=True)
    mgr.create("ws/svc", kind="service", preset="service", service={"command": ["/bin/sh", "-c", "sleep 30"]})
    runtime = Runtime(envs=mgr)
    yield runtime
    await mgr.shutdown()


def specs(runtime):
    return {s.name: s for s in tool_specs(runtime)}


def test_specs_are_well_formed(rt):
    for s in tool_specs(rt):
        assert TOOL_NAME_RE.match(s.name) and s.description and "\u2014" not in s.description
        params = list(inspect.signature(s.handler).parameters.values())
        assert params[0].name == "ctx" and all(p.kind == p.KEYWORD_ONLY for p in params[1:])
        assert s.hidden_without == "envs"


async def test_exec_authorizes_with_the_exact_command(rt):
    ctx = Ctx(rt)
    out = await specs(rt)["terminal_exec"].handler(ctx, on="ws/bench", cmd="echo $((40+2))")
    assert out["stdout"] == "42\n"
    call = ctx.calls[0]
    assert call["effect"] == Effect.EXEC and call["lane_id"] == "ws/bench"
    assert "echo $((40+2))" in call["summary"] and "no isolation" in call["summary"]
    assert call["content"]["cmd"] == "echo $((40+2))"
    assert call["facts"]["isolated"] is False  # a workbench lane: a person every time, never standing
    assert "node token" in call["facts"]["runs_on_node"] and "ws/bench" in call["facts"]["runs_on_node"]


async def test_exec_tells_the_policy_an_isolated_lane_is_one(rt):
    rt.envs.create("ws/sb", preset="sandbox")
    ctx = Ctx(rt)
    await specs(rt)["terminal_exec"].handler(ctx, on="ws/sb", cmd="true")
    await specs(rt)["terminal_create"].handler(ctx, on="ws/sb2")
    assert [c["facts"] for c in ctx.calls] == [{"isolated": True}, {"isolated": True}]


async def test_denied_exec_runs_nothing(rt, tmp_path):
    ctx = Ctx(rt, deny={Effect.EXEC})
    marker = tmp_path / "ran"
    with pytest.raises(PolicyDenied):
        await specs(rt)["terminal_exec"].handler(ctx, on="ws/bench", cmd=f"touch {marker}")
    assert not marker.exists()


async def test_install_effect_depends_on_shared_filesystem(rt):
    ctx = Ctx(rt, deny={Effect.INSTALL_BASE, Effect.INSTALL_OVERLAY})
    with pytest.raises(PolicyDenied):
        await specs(rt)["terminal_install"].handler(ctx, on="ws/bench", packages=["jq"], source="apt")
    assert ctx.calls[0]["effect"] == Effect.INSTALL_BASE and "onto the node itself" in ctx.calls[0]["summary"]
    rt.envs.create("ws/sb")
    with pytest.raises(PolicyDenied):
        await specs(rt)["terminal_install"].handler(ctx, on="ws/sb", packages=["requests==2.32.3"], source="pip")
    call = ctx.calls[1]
    assert call["effect"] == Effect.INSTALL_OVERLAY and "requests==2.32.3" in call["summary"]
    assert "pypi.org" in call["summary"] and call["content"] == {"source": "pip", "packages": ["requests==2.32.3"]}


async def test_shell_tools_roundtrip(rt):
    ctx = Ctx(rt)
    sp = specs(rt)
    opened = await sp["shell_open"].handler(ctx, on="ws/bench")
    sid = opened["session"]
    await sp["shell_write"].handler(ctx, session=sid, text="echo tool-$((2*21))\n")
    text = ""
    for _ in range(20):
        r = await sp["shell_read"].handler(ctx, session=sid, wait_ms=500)
        text += r["output"]
        if "tool-42" in text:
            break
    assert "tool-42" in text
    assert [c["effect"] for c in ctx.calls[:3]] == [Effect.EXEC, Effect.EXEC, Effect.READ]
    assert "echo tool-$((2*21))" in ctx.calls[1]["summary"]
    assert (await sp["shell_close"].handler(ctx, session=sid))["closed"]


async def test_service_tools_use_the_service_effect(rt):
    ctx = Ctx(rt)
    sp = specs(rt)
    st = await sp["service_start"].handler(ctx, on="ws/svc", ttl_s=60)
    assert st["desired"] == "running"
    assert ctx.calls[0]["effect"] == Effect.SERVICE and "sleep 30" in ctx.calls[0]["summary"]
    assert "stops after 60s" in ctx.calls[0]["summary"]
    assert (await sp["service_status"].handler(ctx, on="ws/svc"))["lane"] == "ws/svc"
    assert "log" in await sp["service_logs"].handler(ctx, on="ws/svc")
    st = await sp["service_stop"].handler(ctx, on="ws/svc")
    assert st["state"] == "stopped"


async def test_lane_listing_create_remove_and_images(rt):
    ctx = Ctx(rt)
    sp = specs(rt)
    lanes = await sp["terminals_list"].handler(ctx)
    assert {lane["id"] for lane in lanes} == {"ws/bench", "ws/svc"}
    made = await sp["terminal_create"].handler(ctx, on="ws/tmp", network="isolated", allow_hosts=["example.com"])
    assert made["egress_allowlist"] == ["example.com"] and made["preset"] == "sandbox"
    with pytest.raises(PolicyDenied):
        await sp["terminal_create"].handler(ctx, on="ws/wb", preset="workbench")
    await sp["terminal_remove"].handler(ctx, on="ws/tmp")
    assert ctx.calls[-1]["effect"] == Effect.IRREVERSIBLE
    imgs = await sp["images_list"].handler(ctx)
    assert {t["name"] for t in imgs["templates"]} >= {"dev-box", "render", "brain", "browser"}


# ------------------------------------------------------------------ the desktop's view of a command


class RecordingTap:
    """openajar.environments.desklink.Tap's shape: what a terminal_exec handed it."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.chunks: list[tuple[str, bytes]] = []
        self.ended: list = []

    def feed(self, which, data):
        self.chunks.append((which, data))
        if self.fail:
            raise RuntimeError("the window is gone")

    def end(self, res, *, cancelled=False):
        self.ended.append((res, cancelled))
        if self.fail:
            raise RuntimeError("the window is gone")

    def job(self, job_id):
        self.ended.append(("job", job_id))
        if self.fail:
            raise RuntimeError("the window is gone")


CMD = "printf 'out\\n'; printf 'err\\n' >&2; exit 3"


def _result(out):
    return {k: out[k] for k in ("exit_code", "stdout", "stderr", "stdout_bytes", "stderr_bytes", "truncated",
                                "timed_out", "signal")}


async def test_a_mirrored_command_returns_exactly_what_it_does_without_the_mirror(rt):
    terminal_exec = specs(rt)["terminal_exec"].handler
    plain = await terminal_exec(Ctx(rt), on="ws/bench", cmd=CMD)
    taps: list[RecordingTap] = []

    def hook(lane_id, cmd, cwd):
        taps.append(RecordingTap())
        return taps[-1]

    rt.envs.exec_mirror = hook
    seen = await terminal_exec(Ctx(rt), on="ws/bench", cmd=CMD)
    assert _result(seen) == _result(plain) and seen["exit_code"] == 3
    [tap] = taps
    assert b"".join(d for w, d in tap.chunks if w == "o") == b"out\n"
    assert b"".join(d for w, d in tap.chunks if w == "e") == b"err\n"
    [(res, cancelled)] = tap.ended
    assert res.exit_code == 3 and cancelled is False


async def test_a_mirror_that_fails_never_fails_the_command(rt):
    terminal_exec = specs(rt)["terminal_exec"].handler
    plain = await terminal_exec(Ctx(rt), on="ws/bench", cmd=CMD)
    rt.envs.exec_mirror = lambda lane_id, cmd, cwd: RecordingTap(fail=True)
    assert _result(await terminal_exec(Ctx(rt), on="ws/bench", cmd=CMD)) == _result(plain)

    def broken(lane_id, cmd, cwd):
        raise RuntimeError("no desktop")

    rt.envs.exec_mirror = broken
    assert _result(await terminal_exec(Ctx(rt), on="ws/bench", cmd=CMD)) == _result(plain)


async def test_a_command_that_times_out_says_so_in_the_window(rt):
    tap = RecordingTap()
    rt.envs.exec_mirror = lambda lane_id, cmd, cwd: tap
    out = await specs(rt)["terminal_exec"].handler(Ctx(rt), on="ws/bench", cmd="sleep 5", timeout_s=1)
    assert out["timed_out"] is True and tap.ended[0][0].timed_out is True


async def test_commands_get_the_screen_while_the_desktop_runs(rt):
    terminal_exec = specs(rt)["terminal_exec"].handler
    rt.envs.screen_env = lambda lane_id: {"DISPLAY": ":99"} if lane_id == "ws/bench" else {}
    out = await terminal_exec(Ctx(rt), on="ws/bench", cmd='echo "[$DISPLAY]"')
    assert out["stdout"] == "[:99]\n"

    def broken(lane_id):
        raise RuntimeError("no desktop")

    rt.envs.screen_env = broken
    assert (await terminal_exec(Ctx(rt), on="ws/bench", cmd='echo "[$DISPLAY]"'))["stdout"] == "[]\n"


def test_on_an_environments_machine_a_command_starts_in_the_repository_as_the_file_tools_do():
    from types import SimpleNamespace

    from openajar.env.tools import start_in

    envs = SimpleNamespace(workdir_of=lambda lane_id: "/work/click" if lane_id == "env/e1" else None)
    services = {"environments": envs}
    ctx = SimpleNamespace(runtime=SimpleNamespace(get=services.get))
    vm = SimpleNamespace(remote=object())
    assert start_in(ctx, vm, "env/e1", None) == "/work/click"  # not /root
    assert start_in(ctx, vm, "env/e1", "src") == "/work/click/src"
    assert start_in(ctx, vm, "env/e1", "/work/other") == "/work/other"
    with pytest.raises(InvalidRequest):
        start_in(ctx, vm, "env/e1", "src'; rm -rf / #")  # one plain folder name, safe as one shell word
    local = SimpleNamespace(remote=None)
    assert start_in(ctx, local, "ws/bench", None) is None  # a node lane starts at its home, as before


async def test_a_cwd_that_is_not_there_says_so_and_runs_nothing(rt):
    terminal_exec = specs(rt)["terminal_exec"].handler
    with pytest.raises(NotFound, match="no folder nope"):
        await terminal_exec(Ctx(rt), on="ws/bench", cmd="echo a; touch ran", cwd="nope")
    assert "ran" not in (await terminal_exec(Ctx(rt), on="ws/bench", cmd="ls"))["stdout"]
    await terminal_exec(Ctx(rt), on="ws/bench", cmd="mkdir -p there")
    out = await terminal_exec(Ctx(rt), on="ws/bench", cmd="pwd; echo b", cwd="there")
    assert out["stdout"].endswith("/there\nb\n") and out["exit_code"] == 0


async def test_a_timed_out_command_says_what_to_do_instead(rt):
    out = await specs(rt)["terminal_exec"].handler(Ctx(rt), on="ws/bench", cmd="sleep 5", timeout_s=1)
    assert out["timed_out"] and out["exit_code"] is None and out["signal"]
    assert "terminal_job_start" in out["note"] and "still running after 1 s" in out["note"]
