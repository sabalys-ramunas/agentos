# ruff: noqa: F811  (pytest fixtures imported from test_env_node_support)
import subprocess

import pytest
from test_env_node_support import mgr, needs_node  # noqa: F401

pytestmark = [pytest.mark.node, needs_node]


def host_has(pkg: str) -> bool:
    return subprocess.run(["dpkg", "-s", pkg], capture_output=True, check=False).returncode == 0


async def test_apt_install_lands_in_the_lane_overlay_only(mgr):
    assert not host_has("sl"), "pick a package the node does not have"
    mgr.create("pg/a", overrides={"filesystem": "overlay", "disk_mb": 512})
    mgr.create("pg/b")
    out = await mgr.install("pg/a", ["sl"], "apt")
    assert out["ok"], out["output_tail"]
    assert "sl" in out["installed"] and out["manifest_hash"].startswith("sha256:")
    r = await mgr.exec("pg/a", "test -x /usr/games/sl && dpkg -s sl | grep -c 'install ok installed'")
    assert r.stdout.strip() == "1"
    r = await mgr.exec("pg/b", "test -e /usr/games/sl && echo present || echo absent")
    assert r.stdout.strip() == "absent"
    assert not host_has("sl")
    await mgr.cool("pg/a")
    r = await mgr.exec("pg/a", "test -x /usr/games/sl && echo persisted")
    assert r.stdout.strip() == "persisted"
    egress = [e for e in mgr.egress_log if e["lane"] == "pg/a#install"]
    assert egress and all(e["allowed"] for e in egress)


async def test_pip_install_goes_through_the_pinned_index(mgr):
    mgr.create("pg/py", overrides={"filesystem": "overlay", "disk_mb": 256})
    out = await mgr.install("pg/py", ["six==1.16.0"], "pip")
    assert out["ok"], out["output_tail"]
    assert out["installed"] == {"six": "1.16.0"}
    r = await mgr.exec("pg/py", "python3 -c 'import six; print(six.__version__)'")
    assert r.stdout == "1.16.0\n"
    hosts = {e["host"] for e in mgr.egress_log if e["lane"] == "pg/py#install" and e["allowed"]}
    assert hosts <= {"pypi.org", "files.pythonhosted.org"}


async def test_base_image_build_versions_and_use(mgr):
    mgr.paths.templates.mkdir(parents=True, exist_ok=True)
    (mgr.paths.templates / "tiny.toml").write_text(
        'description = "test"\napt = ["figlet"]\nfiles = { "/etc/tiny-image" = "tiny\\n" }\n'
        'env = { TINY = "1" }\n')
    one = await mgr.build_image("tiny")
    assert one.ref == "tiny@1" and one.content_hash.startswith("sha256:") and one.size_bytes > 0
    assert "figlet" in one.manifest["apt"]
    two = await mgr.build_image("tiny")
    assert two.ref == "tiny@2" and two.template_hash == one.template_hash
    assert (mgr.paths.image_dir("tiny", 1) / "layer" / "usr").is_dir()
    assert not host_has("figlet")
    mgr.create("pg/img", overrides={"base_image": "tiny@1"})
    r = await mgr.exec("pg/img", "figlet -v >/dev/null && cat /etc/tiny-image && echo TINY=$TINY && "
                                 "dpkg -s figlet | grep -c 'ok installed'")
    assert r.stdout.split() == ["tiny", "TINY=1", "1"], r.stderr
    mgr.create("pg/noimg")
    r = await mgr.exec("pg/noimg", "command -v figlet || echo none")
    assert r.stdout.strip() == "none"
