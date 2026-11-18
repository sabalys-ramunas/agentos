"""Test-wide environment: the same results on a laptop, on the node and in CI.

- Typer forces a coloured, width-limited terminal when it sees GITHUB_ACTIONS, which breaks
  assertions on --help output; tests read plain, wide help everywhere.
- Lane managers in unit tests never read the host's free memory or disk: on a busy Linux machine
  (the node, CI) the real probes refused `mgr.open` with ResourcesLow. Tests of the floors pass
  their own probes; node tests (`-m node`) keep the real ones.
- Unit tests never reach the network: a socket may connect or send only to loopback (127.0.0.0/8,
  ::1, "localhost") or a Unix socket. Anything else raises NetworkBlocked and fails the test, even
  when the code under test swallows the error. Tests that really need the network say so with
  `@pytest.mark.network`; node tests (`-m node`) are not guarded (their browser is walled off by
  its own context routes, and CI runs them with outbound traffic dropped). AGENTOS_TEST_NETWORK=1
  turns the guard off for a local experiment.
- A process that starts with SIGINT ignored (some CI shells do this) passes the ignore on to
  every child, so a test shell's Ctrl-C would never interrupt `sleep`. Production is not
  affected: agentosd installs its own SIGINT handler, and handled signals reset on exec.
- http.server's HTTPServer looks up its own name (socket.getfqdn) when it binds. On GitHub's macOS
  runners that lookup of 127.0.0.1 takes about 35 s, which the first test server of each shard
  paid. Test servers skip it; the name is only used for CGI.
- A session fails when it held many more open files than it started with (FD_MARGIN below), and
  names the test files where the count grew: a leak that Linux hides and macOS does not.
- AGENTOS_TEST_SHARD=<i>/<n> runs only shard i of n (CI splits the unit tests over n runners). Whole
  files go to one shard, the largest first to the shard with the fewest tests, so the n shards
  together run every selected test exactly once.
"""

import gc
import http.server
import os
import signal
import socketserver
from collections import Counter
from typing import Any

import _netguard  # tests/_netguard.py: this directory is on sys.path while its conftest loads
import pytest

os.environ.pop("GITHUB_ACTIONS", None)
os.environ.pop("FORCE_COLOR", None)
os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"
os.environ["TERMINAL_WIDTH"] = "200"
os.environ.setdefault("NO_COLOR", "1")

if signal.getsignal(signal.SIGINT) == signal.SIG_IGN:
    signal.signal(signal.SIGINT, signal.default_int_handler)


def _bind_without_a_name_lookup(self: http.server.HTTPServer) -> None:
    socketserver.TCPServer.server_bind(self)
    self.server_name, self.server_port = self.server_address[:2]


http.server.HTTPServer.server_bind = _bind_without_a_name_lookup  # type: ignore[method-assign]


def shard_of(counts: Counter[str], shards: int) -> dict[str, int]:
    """Each file's shard (0-based): the file with the most tests first, each to the shard that has
    the fewest so far (ties to the lower shard), the same on every runner."""
    load = [0] * shards
    out = {}
    for path, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        shard = min(range(shards), key=lambda i: (load[i], i))
        out[path] = shard
        load[shard] += count
    return out


@pytest.hookimpl(trylast=True)  # after -m and -k have deselected, so the shards share what runs
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    spec = os.environ.get("AGENTOS_TEST_SHARD")
    if not spec:
        return
    index, _, total = spec.partition("/")
    if not (index.isdigit() and total.isdigit() and 1 <= int(index) <= int(total)):
        raise pytest.UsageError(f"AGENTOS_TEST_SHARD must be <i>/<n> with 1 <= i <= n (got {spec!r})")
    files = [item.nodeid.split("::", 1)[0] for item in items]
    shards = shard_of(Counter(files), int(total))
    mine = int(index) - 1
    keep = [item for item, path in zip(items, files, strict=True) if shards[path] == mine]
    drop = [item for item, path in zip(items, files, strict=True) if shards[path] != mine]
    if drop:
        config.hook.pytest_deselected(items=drop)
    items[:] = keep


# ---- open files: what the session holds, against what it held at its start ---------------------

# Descriptors a session may hold on top of its start: at its end, once garbage is collected (a
# shard ends 20 to 200 up), and at any point between two test files, garbage included (a shard
# peaks 1,050 to 1,400 up; macOS gives a process 10,240). AGENTOS_TEST_FD_MARGIN="<end>,<peak>".
FD_MARGIN = (500, 4000)
_fds: dict[str, Any] = {"start": None, "file": None, "at": 0, "peak": 0, "grew": Counter()}


def open_fds() -> int:
    """How many file descriptors this process has open (/dev/fd on macOS and Linux)."""
    try:
        return len(os.listdir("/dev/fd")) - 1  # less the one listdir opened
    except OSError:
        return -1


def pytest_sessionstart(session: pytest.Session) -> None:
    _fds["start"] = _fds["at"] = _fds["peak"] = open_fds()


def pytest_runtest_logfinish(nodeid: str, location: tuple) -> None:
    path = nodeid.split("::", 1)[0]
    if path != _fds["file"]:  # a file ended: what it left open (garbage not yet collected counts too)
        now = open_fds()
        if _fds["file"] is not None:
            _fds["grew"][_fds["file"]] += now - _fds["at"]
        _fds["file"], _fds["at"], _fds["peak"] = path, now, max(_fds["peak"], now)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail a session that held many more open files than it started with: a leak (a store, a
    socket, an event loop never closed) that Linux's 65,536 open files hide and macOS's 10,240 do
    not. Seen: "Too many open files" at the end of a macOS shard, from dev nodes that stayed
    reachable after their tests (agentos.dev.lanes, FakeLaneManager.shutdown)."""
    start = _fds["start"]
    if start is None or start < 0 or session.config.option.collectonly:
        return
    if _fds["file"] is not None:
        now = open_fds()
        _fds["grew"][_fds["file"]] += now - _fds["at"]
        _fds["peak"] = max(_fds["peak"], now)
    gc.collect()  # what only waits for the collector is counted in the peak, not at the end
    _fds["end"] = open_fds()
    wanted = os.environ.get("AGENTOS_TEST_FD_MARGIN")
    end_margin, peak_margin = (int(x) for x in wanted.split(",")) if wanted else FD_MARGIN
    if _fds["end"] - start > end_margin or _fds["peak"] - start > peak_margin:
        _fds["failed"] = (end_margin, peak_margin)
        if session.exitstatus == pytest.ExitCode.OK:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED


def pytest_terminal_summary(terminalreporter: Any) -> None:
    if "failed" not in _fds:
        return
    end_margin, peak_margin = _fds["failed"]
    start = _fds["start"]
    worst = ", ".join(f"{path} (+{n})" for path, n in _fds["grew"].most_common(8) if n > 0)
    terminalreporter.section("open files", sep="=", red=True, bold=True)
    terminalreporter.line(
        f"this session opened files, sockets or event loops and did not close them. It started with {start} "
        f"open, held {_fds['peak']} at most (margin +{peak_margin}) and ended with {_fds['end']} once garbage was "
        f"collected (margin +{end_margin}). Where the count grew most: {worst}", red=True)


@pytest.fixture(autouse=True)
def _no_real_agent_config(monkeypatch):
    """`agentos connect` writes Codex's config.toml and Claude Code's skills folder: with these two
    set, a test that points HOME at a temp dir would still reach the owner's real ones."""
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    # `agentos up` connects this machine's own Claude Code and Codex when they are installed: never in tests
    from agentos.dev import cli as dev_cli

    monkeypatch.setattr(dev_cli, "connect_clients", lambda port, token: [])
    try:
        from agentos.controller import cli as controller_cli
    except ModuleNotFoundError:
        return
    monkeypatch.setattr(controller_cli, "_here", lambda here: False)


@pytest.fixture(autouse=True)
def _no_network(request, monkeypatch):
    if (request.node.get_closest_marker("node") or request.node.get_closest_marker("network")
            or os.environ.get("AGENTOS_TEST_NETWORK") == "1"):
        yield
        return
    attempts = request.node.stash[_netguard.ATTEMPTS] = []
    _netguard.install(monkeypatch, attempts)
    yield
    if attempts:
        pytest.fail("the test tried to reach the network (tests/conftest.py): " + ", ".join(attempts),
                    pytrace=False)


@pytest.fixture(autouse=True)
def _no_host_resources(request, monkeypatch):
    if request.node.get_closest_marker("node") is None:
        from agentos.node.lanes import resources  # where the lane manager reads its probes

        monkeypatch.setattr(resources, "available_memory_mb", lambda: None)
        monkeypatch.setattr(resources, "disk_free_bytes", lambda path: None)


# Modules that use a commit as the call that waits for a person, to test what happens around that
# wait (approvals, standing approvals, pushes). They predate "Ask me first" (agentos.policy.ask_first).
# They run on the shipped default, as if a person had turned Ask me first on for each lane they use,
# so every assertion in them holds on the shipped table with a real switch. Everything else runs with
# no switch at all. AGENTOS_TEST_NO_SWITCHES=1 runs these with none too, which is what
# the ask-first default audit classifies.
SWITCHED_ON = frozenset({
    "test_api_members", "test_approval_cards", "test_channel_e2e", "test_confirm_paths", "test_channel_member_call_limits",
    "test_channel_member_calls", "test_channel_node", "test_channel_standing", "test_connector_approvals",
    "test_controller_standing", "test_node_panel", "test_policy_approvals", "test_policy_authorize",
    "test_policy_cli_api", "test_policy_edge_api", "test_policy_edge_authorize", "test_policy_edge_runner",
    "test_policy_members", "test_policy_memory_labels", "test_policy_notifier", "test_policy_notify_members",
    "test_policy_principals", "test_policy_security_authz", "test_policy_security_runner",
    "test_policy_standing_binding", "test_policy_stored_grants", "test_policy_taint_downgrade",
    "test_policy_wrap", "test_qa_channel_node", "test_qa_teams_b2", "test_runner_safety",
})


@pytest.fixture(autouse=True)
def _a_person_switched_it_on(request, monkeypatch):
    if request.module.__name__.rsplit(".", 1)[-1] not in SWITCHED_ON or os.environ.get("AGENTOS_TEST_NO_SWITCHES") == "1":
        return
    from agentos.policy.ask_first import AskFirst

    for_call = AskFirst.for_call

    def switched_on(self, flow_id, lane_id, through=None):
        if lane_id and self.get("lane", lane_id) is None:
            self.set("lane", lane_id, True, by="owner", person=True)
        return for_call(self, flow_id, lane_id, through)

    monkeypatch.setattr(AskFirst, "for_call", switched_on)


@pytest.fixture(autouse=True)
def _no_raw_files(monkeypatch):
    """An open reads a public repository's .agentos/environment.toml from GitHub's raw files; in tests,
    GitHub does not answer (the file is then read in the checkout), unless a test gives raw_fetch."""
    from agentos.environments import preparing

    async def no_answer(url):
        return 0, b""

    monkeypatch.setattr(preparing, "raw_get", no_answer)


@pytest.fixture(autouse=True)
def _no_preflight_waits(monkeypatch):
    """A network check before an open is tried again over about 15 s; in tests, at once."""
    from agentos.environments import manager as env_manager
    from agentos.vms import manager as vm_manager

    monkeypatch.setattr(env_manager, "REPO_CHECK_WAITS_S", (0.0, 0.0, 0.0))
    monkeypatch.setattr(vm_manager, "PREFLIGHT_WAITS_S", (0.0, 0.0, 0.0))
