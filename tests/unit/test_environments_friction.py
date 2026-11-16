"""What real agents tripped on in their first environments (2026-11-13), as tests: environment_open
hands back only a live environment with the repository asked for; it checks the repository before
it rents anything (repo_not_connected); sizes an agent can ask for; and local changes reach the VM
through an upload and a job's sync."""

from __future__ import annotations

from datetime import timedelta

import pytest
from test_environments import TITLE, node, opened  # noqa: F401  (node is a fixture)

from agentos.core import catalog
from agentos.core.models import utcnow
from agentos.policy.wrap import ToolCallError

REPO = "example.test/me/shop"


# ------------------------------------------------------------------ 3: only what is alive comes back


async def test_a_starting_environment_with_a_repo_is_named_by_its_title(node):  # noqa: F811
    """Report 3: the first open returned `starting`, the agent asked again without the repository.
    The title names that computer, so it attaches, and the result says so (opened, repo); another
    title opens the empty one it may have meant. Nothing tells it to close anything."""
    node["machine"].setup = "status: running"  # it stays starting
    first = await opened(node, repo=REPO, wait_s=0)
    assert first["state"] == "starting" and first["opened"] == "new"
    again = await opened(node, wait_s=0)
    assert again["id"] == first["id"] and again["opened"] == "attached" and again["repo"] == REPO
    assert "already open" in again["next"] and "environment_close" not in again["next"].split(".")[0]
    node["machine"].setup = "ready"
    fresh = await opened(node, title="Empty one")
    assert fresh["id"] != first["id"] and fresh["repo"] is None and fresh["state"] == "ready"
    assert fresh["opened"] == "new"


async def test_a_failed_environment_is_never_handed_back(node):  # noqa: F811
    node["machine"].setup = "status: error\nE: no space left on device"
    with pytest.raises(ToolCallError, match="environment_failed: setup: "):
        await opened(node, repo=REPO)
    failed = {"id": node["mgr"].store.recent(1)[0].id}
    node["machine"].setup = "ready"
    for session in ("chat-1", "chat-2"):  # the chat that opened it, and a resumed one
        out = await opened(node, session=session)
        assert out["id"] != failed["id"] and out["state"] == "ready" and out["repo"] is None
        await node["call"]("environment_close", session=session)


async def test_a_closing_environment_is_never_handed_back(node):  # noqa: F811
    a = await opened(node)
    node["mgr"].store.update(a["id"], lambda e: setattr(e, "state", "closing"))  # the CLI closed it
    out = await opened(node)
    assert out["id"] != a["id"] and out["state"] == "ready"


async def test_a_title_with_a_live_environment_of_another_repo_is_already_open(node):  # noqa: F811
    """Another chat names the title of a live environment with another repository: it neither gets
    that machine nor a second one with the same title (the next attach by title would be a guess).
    The error says to pick another title, and never to close or discard it."""
    a = await opened(node, repo=REPO)
    mgr = node["mgr"]
    mgr._clock = lambda: utcnow() + timedelta(minutes=11)  # chat-1 is quiet
    with pytest.raises(ToolCallError, match="environment_already_open") as err:
        await node["call"]("environment_open", title=TITLE, repo="example.test/me/other", session="chat-2")
    words = str(err.value)
    assert "another title" in words and f'environment_open(title="{TITLE}") without repo' in words
    assert "close" not in words and "discard" not in words
    lines = catalog.mcp_lines("environment_already_open", words)
    assert "another title" in lines[0] and "Never close it" in lines[0] and "discard" not in lines[0]
    assert mgr.get(a["id"]).binding == "session:chat-1" and len(mgr.store.open()) == 1
    other = await node["call"]("environment_open", title=TITLE, repo=REPO, session="chat-2")
    assert other["id"] == a["id"]


async def test_the_title_alone_takes_back_the_one_environment_with_it(node):  # noqa: F811
    """P2-9 of the 2026-11-15 test: a resumed chat that names only the title gets its environment,
    whatever repository it cloned, and the chat that lost it cannot take it straight back."""
    a = await opened(node, repo=REPO)
    mgr = node["mgr"]
    later = utcnow() + timedelta(minutes=11)
    mgr._clock = lambda: later  # chat-1 is quiet
    other = await node["call"]("environment_open", title=TITLE, session="chat-2")
    assert other["id"] == a["id"] and other["repo"] == REPO
    env = mgr.get(a["id"])
    assert env.binding == "session:chat-2" and env.last_call_at == later  # the takeover counts as a call
    with pytest.raises(ToolCallError, match="environment_in_use"):  # no ping-pong
        await node["call"]("environment_open", title=TITLE, session="chat-1")
    assert mgr.get(a["id"]).binding == "session:chat-2"


async def test_a_reconnected_chat_hears_the_repo_it_takes_back_with(node):  # noqa: F811
    a = await opened(node, repo=REPO)
    with pytest.raises(ToolCallError, match="no_environment") as err:
        await node["call"]("lane_exec", cmd="ls", session="chat-1-reconnected")
    assert a["id"] in str(err.value) and REPO in str(err.value)
    seen = await node["call"]("environment", session="chat-1-reconnected")
    assert seen["open"][0]["repo"] == REPO


# ------------------------------------------------------------------ 1: fail before renting


def _refusing(stderr: bytes):
    seen: list[tuple[str, dict]] = []

    async def probe(url, env, *, timeout_s=20, allow_file=False):
        seen.append((url, env))
        return False, stderr

    probe.seen = seen
    return probe


PRIVATE = b"fatal: could not read Username for 'https://example.test': terminal prompts disabled\n"


async def test_a_private_repo_nobody_connected_rents_nothing(node):  # noqa: F811
    """Report 1: the VM was rented, then the clone failed with `could not read Username`, twice.
    Now the node asks first, anonymously, and before it rents gives a card for the human
    (agentos.environments.connect): a link to paste a token in, and the command at the node."""
    node["mgr"].repo_probe = probe = _refusing(PRIVATE)
    card = await node["call"]("environment_open", title=TITLE, repo=REPO)
    assert card["status"] == "approval_pending" and card["kind"] == "repo_connect" and card["repo"] == REPO
    assert "Nothing was rented" in card["why"] and "not declared for environments" in card["why"]
    assert f"agentos environments repo add {REPO} --token" in card["fallback"] and "no admin rights" in card["fallback"]
    assert node["vms"].store.recent() == [] and node["mgr"].store.recent() == []
    # it asked with no credential: the URL carries none and nothing would prompt
    [(url, env)] = probe.seen
    assert url == f"https://{REPO}.git" and env["GIT_TERMINAL_PROMPT"] == "0"


async def test_a_node_without_approvals_still_says_the_fix(node):  # noqa: F811
    node["rt"].register("approvals", None)
    node["mgr"].repo_probe = _refusing(PRIVATE)
    with pytest.raises(ToolCallError, match="repo_not_connected") as err:
        await node["call"]("environment_open", title=TITLE, repo=REPO)
    words = str(err.value)
    assert "Nothing was rented" in words and "not declared for environments" in words
    for cmd in (f"agentos environments repo add {REPO}", "--token", "deploy key", "never you"):
        assert cmd in words, cmd


async def test_a_public_repo_opens_without_a_declaration(node):  # noqa: F811
    out = await opened(node, repo=REPO)
    assert out["state"] == "ready" and out["repo"] == REPO


async def test_a_host_the_node_cannot_reach_rents_nothing_either(node):  # noqa: F811
    node["mgr"].repo_probe = _refusing(b"fatal: unable to access: Could not resolve host: example.test\n")
    with pytest.raises(ToolCallError, match="environment_failed") as err:
        await node["call"]("environment_open", title=TITLE, repo=REPO)
    words = str(err.value)
    assert "could not reach example.test" in words and node["vms"].store.recent() == []
    assert words.startswith("environment_failed: network: the computer agentos runs on") and "4 tries" in words
    assert "Try again in 2 minutes" in words


async def test_a_repo_check_the_network_drops_is_tried_again_before_it_fails(node):  # noqa: F811
    """A laptop's network drops for a few seconds: the check goes again (over about 15 s) and the open works."""
    tries = []

    async def flaky(url, env, *, timeout_s=20.0, allow_file=False):
        tries.append(timeout_s)
        return (len(tries) > 2, b"" if len(tries) > 2 else b"fatal: unable to access: Connection timed out\n")

    node["mgr"].repo_probe = flaky
    out = await opened(node, repo=REPO)
    assert out["state"] == "ready" and tries == [20.0, 10.0, 10.0]  # the tries after the first are shorter


class TokenVault:
    """The vault as the git gateway reads it: one token for example.test/me/shop."""

    async def list_secrets(self):
        return [{"ref": "git:shop-token", "kind": "git_token", "label": "git token",
                 "scope": {"host": "example.test", "repos": "me/shop"}}]

    async def get_secret(self, ref, *, purpose=""):
        assert "before an environment opens" in purpose
        return "tok_" + "x" * 20


@pytest.fixture
async def declared(node):  # noqa: F811
    from agentos.git.service import GitService

    svc = GitService(node["rt"], node["mgr"].home, host_port=0)
    node["rt"].register("git", svc)
    svc.remotes.allow("env", REPO)
    yield svc
    await svc.stop()
    for lane in list(svc._vm_listeners):
        await svc.vm_down(lane)


async def test_a_declared_repo_with_a_refused_token_says_so_before_renting(node, declared):  # noqa: F811
    node["rt"].register("vault", TokenVault())
    node["mgr"].repo_probe = probe = _refusing(b"remote: Invalid username or password.\n"
                                               b"fatal: Authentication failed for 'https://example.test/me/shop.git/'\n")
    card = await node["call"]("environment_open", title=TITLE, repo=REPO)
    assert card["status"] == "approval_pending" and card["kind"] == "repo_connect"
    assert "token shop-token was refused" in card["why"] and "Nothing was rented" in card["why"]
    assert node["vms"].store.recent() == []
    [row] = node["mgr"].store.requests()
    assert row["reason"] == "refused"  # the card asks for a new token
    [(_url, env)] = probe.seen  # with the token, in the process's own config, never on a command line
    assert any(v.startswith("Authorization: Basic ") for k, v in env.items() if k.startswith("GIT_CONFIG_VALUE"))


async def test_a_declared_repo_without_a_credential_that_is_private_says_so(node, declared):  # noqa: F811
    node["mgr"].repo_probe = _refusing(PRIVATE)
    card = await node["call"]("environment_open", title=TITLE, repo=REPO)
    assert "no key or token in the vault" in card["why"]


async def test_a_declared_repo_with_a_working_credential_opens(node, declared):  # noqa: F811
    node["rt"].register("vault", TokenVault())
    out = await opened(node, repo=REPO)
    assert out["state"] == "ready", out


async def test_a_clone_the_gateway_refuses_names_the_key_and_the_fix(node, declared, monkeypatch):  # noqa: F811
    """The check passed, but the clone on the machine still failed (the deploy key was removed at the
    host meanwhile): the error quotes the gateway's own words from git's output, with the same fix."""
    from agentos.env.models import ExecResult

    plain = node["envs"].exec
    said = ("Cloning into '/work/shop'...\nfatal: remote error: agentos: example.test refused the deploy key "
            "shop-ssh (SHA256:abc); add its public key to example.test/me/shop as a deploy key\n")

    async def exec_(lane_id, cmd, **kw):
        if "git clone" in cmd:
            return ExecResult(exit_code=128, stdout="", stderr=said, duration_s=0.1)
        return await plain(lane_id, cmd, **kw)

    monkeypatch.setattr(node["envs"], "exec", exec_)
    with pytest.raises(ToolCallError, match="repo_not_connected: cloning example.test/me/shop") as err:
        await opened(node, repo=REPO)
    words = str(err.value)
    assert "refused the deploy key shop-ssh" in words and "agentos environments repo add" in words
    assert "word for word" in words and "truncated" not in words  # the whole fix fits in an MCP error
    env = node["mgr"].store.recent(1)[0]
    assert env.error_code == "repo_not_connected" and node["vms"].store.get(env.vm_id).state == "deleted"


def test_the_whole_fix_fits_in_an_mcp_error():
    """An MCP error's message is cut at 500 characters: the commands the human runs must all be in it."""
    from agentos.environments import repos
    from agentos.git.service import RepoAccess

    slug = "github.com/" + "o" * 39 + "/" + "n" * 49  # REPO_WORDS_FIT characters
    assert len(slug) == repos.REPO_WORDS_FIT
    for access in (RepoAccess(slug, False, None, declared=False, credential=None),
                   RepoAccess(slug, False, None, declared=True, credential=None),
                   RepoAccess(slug, False, None, declared=True, credential="github-" + "o" * 50 + "-token",
                              kind="token", why="github.com refused the token"),
                   RepoAccess(slug, False, None, declared=True, credential="k", kind="ssh", why="x" * 400)):
        words = repos.not_connected(access).message
        assert len(words) <= repos.MESSAGE_MAX, (len(words), words)
        assert words.endswith("never you.") and "\u2014" not in words


def test_a_clone_with_no_credential_is_not_connected_and_anything_else_failed():
    from agentos.environments.repos import clone_failure

    e = clone_failure(REPO, PRIVATE.decode(), via_gateway=False)
    assert e.code == "repo_not_connected" and "with no credential" in e.message
    e = clone_failure(REPO, "fatal: destination path exists and is not empty", via_gateway=False)
    assert e.code == "environment_failed"


# ------------------------------------------------------------------ 1: the owner's one command


@pytest.fixture
def cli_home(tmp_path, monkeypatch):
    from test_vault_support import make_vault, private_home

    h = private_home(tmp_path)
    make_vault(h)
    monkeypatch.setenv("AGENTOS_HOME", str(h))
    return h


def _cli(*args, input=None):
    from typer.testing import CliRunner

    from agentos.cli import app

    return CliRunner().invoke(app, list(args), input=input)


def test_repo_add_makes_the_key_declares_it_and_prints_what_to_paste(cli_home):
    from agentos.git.remotes import Remotes

    r = _cli("environments", "repo", "add", "github.com/me/shop")
    assert r.exit_code == 0, r.output
    assert "ssh-ed25519 AAAA" in r.output and "https://github.com/me/shop/settings/keys/new" in r.output
    assert "PRIVATE KEY" not in r.output and "Declared github.com/me/shop for environments" in r.output
    assert 'environment_open(title=..., repo="github.com/me/shop")' in r.output
    [rule] = Remotes(cli_home / "git" / "remotes.json").list()
    assert (rule.scope, rule.slug, rule.push) == ("env", "github.com/me/shop", True)
    # again: the key it made is reused and printed again, never a second one
    again = _cli("environments", "repo", "add", "github.com/me/shop")
    assert again.exit_code == 0 and "already" in again.output and "ssh-ed25519 AAAA" in again.output
    keys = _cli("vault", "git", "key", "list", "--json")
    assert len(__import__("json").loads(keys.output)) == 1
    listed = _cli("environments", "repo", "list")
    assert "github.com/me/shop" in listed.output and "deploy key github-me-shop-ssh" in listed.output


def test_repo_add_token_reads_it_from_stdin_and_never_prints_it(cli_home):
    token = "github_pat_" + "Q" * 40
    r = _cli("environments", "repo", "add", "github.com/me/site", "--token", "--stdin", input=token + "\n")
    assert r.exit_code == 0, r.output
    assert token not in r.output and "Stored token" in r.output and "Agents can open it" in r.output


def test_repo_remove_takes_the_declaration_and_its_own_key(cli_home):
    from agentos.git.remotes import Remotes

    _cli("environments", "repo", "add", "github.com/me/shop")
    _cli("environments", "repo", "add", "github.com/me/blog")
    Remotes(cli_home / "git" / "remotes.json").allow("personal", "github.com/me/blog")
    gone = _cli("environments", "repo", "remove", "github.com/me/shop")
    assert gone.exit_code == 0 and "Removed github-me-shop-ssh from the vault" in gone.output
    kept = _cli("environments", "repo", "remove", "github.com/me/blog")
    assert "Kept github-me-blog-ssh: personal still may use" in kept.output
    again = _cli("environments", "repo", "remove", "github.com/me/shop")
    assert again.exit_code == 1 and "not connected for environments" in again.output
    assert "No repository is connected" in _cli("environments", "repo", "list").output


def test_setup_names_the_one_command(cli_home):
    r = _cli("environments", "setup")
    assert "agentos environments repo add github.com/<owner>/<name>" in r.output


# ------------------------------------------------------------------ 2: sizes the agent can ask for


def _with_a_16_cpu_type(node):  # noqa: F811
    node["prov"].prices["cpx62"] = 0.09
    node["prov"].specs["cpx62"] = (16, 32.0, 640, "x86", "shared")


async def test_the_result_says_what_it_got_and_what_bigger_sizes_cost(node):  # noqa: F811
    """Report 2: the VM had 2 CPU and 4 GB, the type check needed about 12 GB, and the agent did not
    know how to ask for more."""
    _with_a_16_cpu_type(node)
    out = await opened(node)
    m = out["machine"]
    assert (m["server_type"], m["cpu"], m["memory_gb"], m["size"]) == ("cx23", 2, 4.0, "small")
    assert [(b["size"], b["server_type"], b["cpu"], b["memory_gb"], b["price_hourly_eur"]) for b in m["bigger"]] == [
        ("medium", "cpx42", 8, 16.0, 0.04), ("large", "cpx42", 8, 16.0, 0.04), ("xlarge", "cpx62", 16, 32.0, 0.09)]
    assert 'size="medium"' in m["next"]
    await node["call"]("environment_close")
    big = await opened(node, title="Type check", size="large")
    assert big["state"] == "ready" and big["machine"]["server_type"] == "cpx42", big
    assert big["machine"]["size"] == "large" and [b["size"] for b in big["machine"]["bigger"]] == ["xlarge"]
    rec = node["vms"].store.get(node["mgr"].get(big["id"]).vm_id)
    assert rec.server_type == "cpx42"


async def test_a_server_type_by_name_says_what_it_has(node):  # noqa: F811
    out = await opened(node, size="ccx23")
    assert (out["machine"]["server_type"], out["machine"]["cpu"], out["machine"]["memory_gb"]) == ("ccx23", 4, 16.0)
    assert out["machine"]["size"] == "medium"


async def test_the_node_bounds_the_size(node):  # noqa: F811
    _with_a_16_cpu_type(node)
    node["mgr"].settings.max_size = "medium"
    with pytest.raises(ToolCallError, match="environment_limit") as err:
        await node["call"]("environment_open", title=TITLE, size="large")
    assert "biggest environment on this node is medium" in str(err.value) and "--max-size" in str(err.value)
    with pytest.raises(ToolCallError, match="environment_limit"):
        await node["call"]("environment_open", title=TITLE, size="cpx62")  # 16 CPU by name: bound too
    assert node["vms"].store.recent() == []
    out = await opened(node, size="medium")
    assert out["state"] == "ready" and out["machine"]["bigger"] == []  # nothing bigger may be asked for


def test_the_default_max_size_is_xlarge_and_allow_changes_it(cli_home):
    from agentos.environments.models import EnvSettings, load_settings

    assert EnvSettings().max_size == "xlarge"
    r = _cli("environments", "allow", "--max-size", "large")
    assert r.exit_code == 0 and "machines up to large (8 CPU, 16 GB)" in r.output, r.output
    assert load_settings(cli_home).max_size == "large"
    assert _cli("environments", "allow", "--max-size", "huge").exit_code == 1


async def test_the_docker_provider_maps_sizes_to_its_limits(tmp_path):
    from agentos.core.runtime import Runtime
    from agentos.environments.manager import EnvironmentManager
    from agentos.environments.models import EnvSettings
    from agentos.vms.docker import DockerProvider
    from agentos.vms.manager import VmManager
    from agentos.vms.models import VmSettings
    from agentos.vms.provider import ServerSpec

    prov = DockerProvider(cpus=2, memory_gb=4)
    prov._info = {"NCPU": 10, "MemTotal": 24 * 2**30}  # this computer: 10 CPUs, 24 GB
    types = {t["name"]: (t["cores"], t["memory_gb"]) for t in await prov.server_types()}
    assert types == {"docker": (2, 4.0), "docker-small": (2, 4.0), "docker-medium": (4, 8.0),
                     "docker-large": (8, 16.0), "docker-xlarge": (10, 24.0)}  # never more than the computer
    spec = ServerSpec(name="m1", server_type="docker-large", image="agentos-env:latest", location="local",
                      user_data="", labels={})
    args = prov.run_args(spec)
    assert args[args.index("--cpus") + 1] == "8" and args[args.index("--memory") + 1] == str(16 * 2**30)
    assert "agentos-server-type=docker-large" in args
    rt = Runtime()
    vms = VmManager(tmp_path, runtime=rt, provider=prov, settings=VmSettings(provider="docker"))
    rt.register("vms", vms)
    mgr = EnvironmentManager(tmp_path, runtime=rt, settings=EnvSettings())
    try:
        default = await vms.plan("env/eabcdefghjk", ttl_minutes=60)
        assert default["server_type"] == "docker"  # no size: the default machine, not the biggest free one
        large = await mgr.plan("eabcdefghjk", size="large", hours=1)
        assert large["server_type"] == "docker-large" and large["specs"]["cores"] == 8
    finally:
        mgr.store.close()
        await vms.shutdown()
