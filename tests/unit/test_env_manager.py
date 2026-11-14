import pytest

from agentos.core.errors import (
    InvalidRequest,
    LaneExists,
    LaneQuarantined,
    NotFound,
    PolicyDenied,
    ServiceUnavailable,
)
from agentos.core.runtime import Runtime
from agentos.env import images
from agentos.env.host import FakeHost
from agentos.env.manager import EnvManager
from agentos.env.models import ImageTemplate
from agentos.env.paths import EnvSettings
from agentos.env.sandbox import DirectLauncher


def make(tmp_path, **kw):
    return EnvManager(tmp_path / "home", host=FakeHost(), launcher=DirectLauncher(),
                      settings=EnvSettings(proxy_port=0, install_proxy_port=0), **kw)


@pytest.fixture
async def mgr(tmp_path):
    m = make(tmp_path)
    yield m
    await m.shutdown()


def test_create_list_and_conflicts(tmp_path):
    m = make(tmp_path)
    info = m.create("biz-a/dev", preset="sandbox")
    assert info.state.value == "cold" and "Not a VM" in info.isolation
    assert [i.lane.id for i in m.list()] == ["biz-a/dev"]
    with pytest.raises(LaneExists):
        m.create("biz-a/dev")
    (m.paths.lane_dir("biz-a/instagram") / "profile").mkdir(parents=True)
    with pytest.raises(LaneExists):
        m.create("biz-a/instagram")
    with pytest.raises(InvalidRequest, match="account preset"):
        m.create("biz-a/acct", preset="account")
    with pytest.raises(InvalidRequest, match="browser lanes"):
        m.create("biz-a/web", kind="browser")
    with pytest.raises(InvalidRequest, match="need a command"):
        m.create("biz-a/svc", kind="service", preset="service")
    with pytest.raises(PolicyDenied, match="agents may create only"):
        m.create("biz-a/wb", preset="workbench", by_agent=True)
    with pytest.raises(NotFound, match="images build dev-box"):
        m.create("biz-a/img", overrides={"base_image": "dev-box"})
    with pytest.raises(ValueError):
        m.create("Bad/ID")


def test_browser_lane_in_the_runtime_blocks_the_id(tmp_path):
    class Lanes:
        def get(self, lane_id):
            return object()
    m = make(tmp_path, runtime=Runtime(lanes=Lanes()))
    with pytest.raises(LaneExists):
        m.create("personal/reddit")


async def test_second_owner_of_a_home_is_refused(tmp_path, mgr):
    await mgr.start()
    other = make(tmp_path)
    with pytest.raises(ServiceUnavailable, match="another agentos process"):
        await other.start()


async def test_orphan_sweep_on_start(tmp_path):
    host = FakeHost()
    m = EnvManager(tmp_path / "home", host=host, launcher=DirectLauncher(), settings=EnvSettings(proxy_port=0))
    stale = m.paths.run / "ws.old" / "mnt" / "usr"
    stale.mkdir(parents=True)
    host.mounted.add(str(stale))
    (m.paths.images / ".build-x@1-abc").mkdir(parents=True)
    await m.start()
    assert ("umount", str(stale)) in host.calls and ("cgroup-sweep",) in host.calls
    assert not (m.paths.run / "ws.old").exists() and not list(m.paths.images.glob(".build-*"))
    assert m.last_sweep["mounts"] == 1
    await m.shutdown()


async def test_lifecycle_states_and_quarantine(mgr):
    mgr.create("ws/sb")
    assert mgr.state("ws/sb").value == "cold"
    await mgr.warm("ws/sb")
    assert mgr.state("ws/sb").value == "warm"
    s = await mgr.shell_open("ws/sb", argv=["/bin/sh"])
    assert mgr.state("ws/sb").value == "active"
    await mgr.shell_close(s.id)
    await mgr.cool("ws/sb")
    mgr.quarantine("ws/sb", "disk damaged")
    with pytest.raises(LaneQuarantined, match="disk damaged"):
        await mgr.exec("ws/sb", "true")
    info = await mgr.reset("ws/sb")
    assert info.state.value == "cold"
    await mgr.remove("ws/sb")
    assert not mgr.has("ws/sb") and not mgr.paths.lane_dir("ws/sb").exists()


async def test_install_policy(mgr):
    mgr.create("ws/sb")
    mgr.create("ws/svc", kind="service", preset="service", service={"command": ["sleep", "1"]})
    mgr.create("ws/locked", preset="locked")
    with pytest.raises(InvalidRequest, match="not a valid apt"):
        await mgr.install("ws/sb", ["git; rm -rf /"], "apt")
    with pytest.raises(InvalidRequest, match="not a valid pip"):
        await mgr.install("ws/sb", ["--index-url=http://evil"], "pip")
    with pytest.raises(PolicyDenied, match="pinned program set"):
        await mgr.install("ws/svc", ["jq"], "apt")
    with pytest.raises(PolicyDenied, match="signed apt"):
        await mgr.install("ws/locked", ["left-pad"], "npm")
    with pytest.raises(PolicyDenied, match="allowlist"):
        await mgr.install("ws/locked", ["jq"], "apt")


async def test_build_image_versions_and_hashes(mgr, tmp_path):
    mgr.paths.templates.mkdir(parents=True)
    (mgr.paths.templates / "notes.toml").write_text(
        'description = "files only"\nfiles = { "/etc/notes.txt" = "hello\\n", "/opt/tool/run" = "x" }\n'
        'env = { NOTES = "1" }\n')
    one = await mgr.build_image("notes")
    two = await mgr.build_image("notes")
    assert (one.ref, two.ref) == ("notes@1", "notes@2")
    assert one.content_hash == two.content_hash and one.template_hash == two.template_hash
    layer = mgr.paths.image_dir("notes", 1) / "layer"
    assert (layer / "etc" / "notes.txt").read_text() == "hello\n" and (layer / "opt" / "tool" / "run").exists()
    info = mgr.create("ws/withimg", overrides={"base_image": "notes"})
    assert info.lane.profile.base_image == "notes@2"
    rt = await mgr.warm("ws/withimg")
    etc = next(o for o in rt.plan.overlays if o.point.path == "etc")
    assert etc.lowers[0] == mgr.paths.image_dir("notes", 2) / "layer" / "etc"
    assert rt.base_env()["NOTES"] == "1"
    with pytest.raises(InvalidRequest, match="placeholder"):
        await mgr.build_image("browser")


def test_template_and_tree_hashing(tmp_path):
    t = ImageTemplate(name="x", apt=["git"])
    assert images.template_hash(t) == images.template_hash(ImageTemplate(name="x", apt=["git"], description="d"))
    assert images.template_hash(t) != images.template_hash(ImageTemplate(name="x", apt=["jq"]))
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "f").write_text("1")
    (tmp_path / "a" / "l").symlink_to("f")
    h1, size = images.tree_hash(tmp_path)
    assert size == 1 and h1 == images.tree_hash(tmp_path)[0]
    (tmp_path / "a" / "f").write_text("2")
    assert images.tree_hash(tmp_path)[0] != h1
    assert images.point_for("/var/lib/dpkg/status")[0].path == "var/lib/dpkg"
    assert images.point_for("/etc/x")[1] == "x"
    with pytest.raises(InvalidRequest):
        images.point_for("/home/x")
    assert images.parse_ref("dev-box@3") == ("dev-box", 3) and images.parse_ref("dev-box") == ("dev-box", None)
    assert set(images.BUILTIN_TEMPLATES) == {"dev-box", "render", "brain", "browser"}
