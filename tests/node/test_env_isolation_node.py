# ruff: noqa: F811  (pytest fixtures imported from test_env_node_support)
import json
from pathlib import Path

import pytest
from test_env_node_support import mgr, needs_node  # noqa: F401

pytestmark = [pytest.mark.node, needs_node]

PROBES = ["/root/agentos", str(Path(__file__).resolve().parents[2].parent),  # the dev root "/root/.ssh", "/var/lib/agentos", "/home", "/var/log/journal",
          "/etc/ssh/ssh_host_ed25519_key", "/etc/ssh/ssh_host_rsa_key", "/var/lib/cloud"]


async def test_sandbox_and_locked_cannot_see_node_secrets_or_other_lanes(mgr):
    home = mgr.paths.home
    (home / "secrets").mkdir(mode=0o700)
    (home / "secrets" / "proxies.json").write_text('{"lt1": {"password": "hunter2"}}')
    mgr.create("biz-a/other", preset="sandbox", overrides={"filesystem": "overlay"})
    r = await mgr.exec("biz-a/other", "echo marker-7f3a > /root/marker-7f3a")
    assert r.exit_code == 0
    other_upper = mgr.paths.upper_image("biz-a/other")
    probes = [*PROBES, str(home), str(home / "secrets"), str(home / "lanes"), str(other_upper),
              str(mgr.paths.run)]
    script = "; ".join(f"test -e '{p}' && echo VISIBLE {p}" for p in probes) + "; echo done"
    find = "find / -path /proc -prune -o -name 'marker-7f3a' -print 2>/dev/null; grep -rl hunter2 /etc /root /var 2>/dev/null; echo searched"
    mgr.create("biz-a/sb", preset="sandbox")
    mgr.create("biz-a/locked", preset="locked", allowed_programs=["sh"])
    for lane in ("biz-a/sb", "biz-a/locked"):
        r = await mgr.exec(lane, f"sh -c \"{script}\"" if lane.endswith("locked") else script)
        assert r.stdout.strip().endswith("done") and "VISIBLE" not in r.stdout, (lane, r.stdout)
        r = await mgr.exec(lane, f"sh -c \"{find}\"" if lane.endswith("locked") else find, timeout_s=120)
        assert r.stdout.strip() == "searched", (lane, r.stdout)


async def test_sandbox_root_is_reduced_and_filtered(mgr):
    mgr.create("biz-a/sb")
    checks = {
        "hostname": "hostname",
        "pid1": "cat /proc/1/comm",
        "nprocs": "ls /proc | grep -c '^[0-9]'",
        "caps": "grep CapEff /proc/self/status | cut -f2",
        "nnp": "grep NoNewPrivs /proc/self/status | cut -f2",
        "seccomp": "grep '^Seccomp:' /proc/self/status | cut -f2",
        "shadow": "cut -d: -f2 /etc/shadow | sort -u | tr '\\n' ' '",
        "core_pattern": "echo x > /proc/sys/kernel/core_pattern 2>/dev/null && echo writable || echo ro",
        "usr": "touch /usr/bin/agentos-x 2>/dev/null && echo writable || echo ro",
        "unshare": "unshare -U true 2>/dev/null && echo ok || echo blocked",
        "keyctl": "python3 -c 'import ctypes,ctypes.util; l=ctypes.CDLL(None, use_errno=True); "
                  "print(l.syscall(250, 0, -4, 0) == -1 and ctypes.get_errno())'",
        "mount": "mount -t tmpfs x /mnt 2>/dev/null && echo mounted || echo denied",
        "cgroup": "cat /proc/self/cgroup",
        "dev": "ls /dev | tr '\\n' ' '",
    }
    script = "; ".join(f"echo {k}=$({v})" for k, v in checks.items())
    r = await mgr.exec("biz-a/sb", script)
    got = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    assert got["hostname"] == "sb" and got["pid1"] == "bwrap"
    assert int(got["nprocs"]) <= 8
    assert int(got["caps"], 16) == 0x200004FB      # chown dac_override fowner fsetid kill setgid setuid bind audit
    assert got["nnp"] == "1" and got["seccomp"] == "2"
    assert got["shadow"].split() == ["*"]
    assert got["core_pattern"] == "ro" and got["usr"] == "ro" and got["unshare"] == "blocked"
    assert got["keyctl"] == "1"                     # EPERM from the seccomp filter
    assert got["mount"] == "denied" and got["cgroup"] == "0::/"
    assert "sda" not in got["dev"] and "null" in got["dev"]


async def test_lane_files_are_private_and_persistence_follows_the_filesystem_dial(mgr):
    mgr.create("biz-a/eph", preset="sandbox")
    mgr.create("biz-a/keep", preset="sandbox", overrides={"filesystem": "overlay", "disk_mb": 128})
    for lane in ("biz-a/eph", "biz-a/keep"):
        assert (await mgr.exec(lane, f"echo {lane} > /root/who; echo hi > /etc/agentos-test")).exit_code == 0
    r = await mgr.exec("biz-a/eph", "cat /root/who")
    assert r.stdout == "biz-a/eph\n"
    for lane in ("biz-a/eph", "biz-a/keep"):
        await mgr.cool(lane)
    r = await mgr.exec("biz-a/eph", "cat /root/who 2>&1 || echo gone")
    assert "gone" in r.stdout                       # ephemeral: wiped when cold
    r = await mgr.exec("biz-a/keep", "cat /root/who /etc/agentos-test")
    assert r.stdout == "biz-a/keep\nhi\n"           # overlay: persists
    import pathlib
    assert not pathlib.Path("/etc/agentos-test").exists()


async def test_describe_matches_mechanisms(mgr):
    info = mgr.create("biz-a/net", preset="sandbox", overrides={"network": "isolated",
                                                                "egress_allowlist": ["example.com"]})
    assert "only to example.com" in info.isolation and "seccomp" in info.isolation
    report = mgr.node_report()
    assert report["isolation"] and report["seccomp"]
    assert json.dumps(report, default=str)
