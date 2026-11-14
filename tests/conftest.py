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
- AGENTOS_TEST_SHARD=<i>/<n> runs only shard i of n (CI splits the unit tests over n runners). Whole
  files go to one shard, the largest first to the shard with the fewest tests, so the n shards
  together run every selected test exactly once.
"""

import os
import signal
from collections import Counter

import _netguard  # tests/_netguard.py: this directory is on sys.path while its conftest loads
import pytest

os.environ.pop("GITHUB_ACTIONS", None)
os.environ.pop("FORCE_COLOR", None)
os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"
os.environ["TERMINAL_WIDTH"] = "200"
os.environ.setdefault("NO_COLOR", "1")

if signal.getsignal(signal.SIGINT) == signal.SIG_IGN:
    signal.signal(signal.SIGINT, signal.default_int_handler)


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
