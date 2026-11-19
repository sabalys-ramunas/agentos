"""`agentos dev up`: the daemon's own composition over the dev fakes (docs/DEVELOPING.md).

build_runtime is agentos.node.daemon.build_runtime with the browser parts swapped for agentos.dev:
the lane manager (DevLaneManager), the interaction (DevInteraction) and the flow driver (which
replays on the lane's own tab, through the same lane gate). Everything after that is the daemon's:
the feature services in their order (policy, vault, workspaces, runner, brains, env, health), the
web app at / and MCP at /mcp, and daemon.serve with its signal handling and clean shutdown.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import shutil
import signal
import tempfile
from collections.abc import Callable
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomli_w

from agentos.api import create_login_code
from agentos.core.config import NodeConfig
from agentos.core.errors import AgentPaused, HumanInControl
from agentos.core.models import LaneSpec, Principal
from agentos.core.runtime import Runtime
from agentos.dev import fakecli
from agentos.dev.health import seed_health
from agentos.dev.lanes import DevLaneManager
from agentos.dev.web import CHALLENGE_URL, DevInteraction, frame_of_page, new_tab
from agentos.node import egress
from agentos.node.daemon import FEATURE_SERVICES, build_plugins, build_services, node_runtime, serve
from agentos.node.flow_driver import LaneAgentPaused, LaneTakenOver, install_take_over_pause
from agentos.node.flow_driver import _using as flow_using
from agentos.runner.fakes import BASE, post_flow
from agentos.vault import crypto
from agentos.vault.service import keyfile_path
from agentos.vault.store import VaultStore
from agentos.workspaces.errors import WorkspaceConflict

OWNER = Principal(kind="human", id="owner", owner="owner")
AGENT = Principal(kind="agent", id="claude-code", thread="T-91")
AGENT_CODEX = Principal(kind="agent", id="codex", thread="T-14")
LANES = ("biz-a/social", "biz-a/shop", "biz-b/social", "biz-b/forum")
SEED_SUMMARY = "Post 1 photo with caption to social.example as biz-a"
SEED_CAPTION = "Autumn menu is here. Pumpkin flat white is back until December."
SEED_QUESTION = "Two cover images fit the post. Use the bright one or the dark one?"


@dataclass(frozen=True)
class DevNode:
    """What `agentos dev up` tells its user once the node is listening."""

    home: Path
    temporary: bool
    port: int
    token: str  # the node token: the owner's, never printed or handed to an agent
    agent_token: str  # the agent grant `claude_add` carries (as `agentos connect` issues on a real node)
    panel_link: str  # one sign-in, works once, valid for 10 minutes
    mcp_url: str
    claude_add: str  # the `claude mcp add` line, with the agent grant's token
    panel_built: bool
    lanes: Any = field(default=None, repr=False, compare=False)  # the lane manager, for in-process tests


def panel_built() -> bool:
    """Is the web app built to serve (web/dist, or $AGENTOS_PANEL_DIR)?"""
    env = os.environ.get("AGENTOS_PANEL_DIR")
    repo = Path(__file__).resolve().parents[3]
    root = Path(env) if env else repo / "web" / "dist"
    return (root / "index.html").is_file()


AGENT_SUBJECT = "claude-code@dev"
AGENT_CEILING = "commit"  # agentos connect's default: posts go, and after untrusted text they ask first
AGENT_TTL_S = 7 * 86400
# as agentos connect gives: memory_read (memory_context works, writing does not) and environments
# (vm, exec and secret on lanes env/* only: the agent may open its own cloud computer, run commands there
# and sign its browser in with the logins the owner bound to it)
AGENT_SPECIALS = {"memory": {}, "vm": {"lanes": ["env/*"]}, "exec": {"lanes": ["env/*"]},
                  "secret": {"lanes": ["env/*"]}}


def issue_agent_grant(runtime: Runtime) -> tuple[str, str]:
    """(grant id, token) of the agent grant the printed `claude mcp add` line carries: every
    workspace, up to commit, memory read only, as `agentos connect` issues for Claude Code. The node
    token stays the owner's and is never printed. A kept home revokes the grants earlier starts
    printed first (a grant only works while its node runs, so that is when it has to end)."""
    policy = runtime.policy
    for record in policy.list_grants():
        if (record.get("subject") or {}).get("id") == AGENT_SUBJECT and record.get("id"):
            policy.revoke(record["id"], by="owner", reason="agentos dev up started again: it printed a new line")
    grant, token = policy.issue(subject={"kind": "agent", "id": AGENT_SUBJECT}, workspaces=["*"],
                                        ceiling=AGENT_CEILING, specials=AGENT_SPECIALS, ttl_s=AGENT_TTL_S, by="owner",
                                        note="agentos dev up: the Claude Code line it printed")
    return grant.id, token


def claude_add_line(port: int, token: str) -> str:
    return (f'claude mcp add --scope user --transport http agentos-dev http://127.0.0.1:{port}/mcp '
            f'--header "Authorization: Bearer {token}"')


def flow_driver_factory(lanes: DevLaneManager):
    """lane id -> async context manager yielding the lane's tab, as the node's flow driver opens it
    (agentos.node.flow_driver): the lane as a flow principal, inside a run its implicit claim for
    the whole run, and the tab's guard told a flow drives it (so the run's own approval covers its
    publish, and a take over or a hold stops it as it stops production flows)."""

    @asynccontextmanager
    async def open_lane(lane_id: str):
        async with AsyncExitStack() as stack:
            try:
                page = await stack.enter_async_context(flow_using(lanes, lane_id))
            except HumanInControl as exc:
                raise LaneTakenOver(exc.message) from exc
            except AgentPaused as exc:
                raise LaneAgentPaused(exc.message) from exc
            guard = egress.guard_for(page)
            if guard is not None:
                await stack.enter_async_context(guard.flow(page))
            yield page.driver

    return open_lane


def build_runtime(config: NodeConfig, *, lane_manager: type[DevLaneManager] = DevLaneManager,
                  ) -> tuple[Runtime, DevLaneManager]:
    """`lane_manager`: agentos studio up passes one with no lanes (agentos.studio.local_cli.NoLanes)."""
    lanes = lane_manager(config, make_page=new_tab, frame=frame_of_page)
    interaction = DevInteraction()
    runtime = node_runtime(config, lanes, lanes.watch(interaction))
    runtime.register("flow_driver", flow_driver_factory(lanes))
    build_services(runtime, FEATURE_SERVICES)
    build_plugins(runtime)  # the browser worker (browser_task), as on a node
    runner = runtime.get("runner")
    if runner is not None:
        install_take_over_pause(runner)
    return runtime, lanes


def prepare_home(home: Path) -> None:
    """What a node keeps in its home, made once: a vault with a key file, and brains that run the
    fake CLIs (agentos.dev.fakecli answers from fixed scripts and never reaches a model)."""
    if not home.exists():
        home.mkdir(parents=True)
        home.chmod(0o700)
    store = VaultStore(home / "vault.db")
    if store.header() is None:
        master = os.urandom(crypto.KEY_LEN)
        crypto.write_keyfile(keyfile_path(home), master)
        store.initialize(mode="keyfile", master=master, kdf=None)
    clis = fakecli.install(home / "dev-bin")
    settings = home / "brains" / "settings.toml"
    if not settings.exists():
        settings.parent.mkdir(parents=True, exist_ok=True)
        settings.write_text(tomli_w.dumps({"claude_bin": clis["claude"], "codex_bin": clis["codex"]}))


def seed_surfaces(runtime: Runtime) -> None:
    """The connector surfaces two accounts act through, saved through the runner's own writer."""
    runner = runtime.get("runner")
    if runner is not None and not runner.surfaces.list():
        for surface in (
            {"id": "biz-a/social", "site": "instagram", "lane": "biz-a/social",
             "acts_as": {"kind": "ig_business", "id": "17841400000001", "name": "@biza.coffee"}},
            {"id": "biz-b/forum", "site": "reddit", "lane": "biz-b/forum",
             "acts_as": {"kind": "account", "user_id": "biz_b_coffee", "name": "u/biz_b_coffee"}},
        ):
            runner.surfaces.put(surface, by=OWNER)


async def seed(runtime: Runtime, lanes: DevLaneManager) -> None:
    """Two workspaces with lanes, a saved flow, a captcha that holds one lane, a lane under a
    human's control, a pending approval and an open question, so every screen has something. Each
    lane gets a profile, a pin and its past holds (agentos.dev.health), so lane health grades them."""
    for ws_id, name, color in (("biz-a", "Biz A", "#6c8cff"), ("biz-b", "Biz B", "#e0a050")):
        try:
            runtime.workspaces.create_workspace(ws_id, name=name, color=color)
        except WorkspaceConflict:
            pass
    fresh = [lane_id for lane_id in LANES if lane_id not in lanes.lanes]  # lanes live in memory: new each start
    for lane_id in fresh:
        await lanes.create(LaneSpec(id=lane_id))
    seed_health(lanes, fresh)  # a profile and a pin each, and past holds, for lane health to grade

    runtime.flows.save(post_flow(id="biz-a.social.post", lane="biz-a/social", title="Post a photo"), by=OWNER)

    interaction = runtime.interaction
    if "biz-a/social" in fresh:
        async with lanes.using("biz-a/social") as page:
            await interaction.navigate(page, f"{BASE}/")
    if "biz-b/social" in fresh:
        async with lanes.using("biz-b/social") as page:
            await interaction.navigate(page, CHALLENGE_URL)  # the captcha holds this lane for a human
    if "biz-b/forum" in fresh:
        async with lanes.using("biz-b/forum") as page:  # open, as a lane a person took over is
            await interaction.navigate(page, f"{BASE}/")
        await lanes.take_control("biz-b/forum", by="owner")

    seed_surfaces(runtime)
    approvals = runtime.approvals
    pending = approvals.list(state="pending")
    if not any(a.summary == SEED_SUMMARY for a in pending):
        approvals.create_action(
            effect="commit", tool="browser_act", lane="biz-a/social", domain="social.example",
            summary=SEED_SUMMARY, content_hash="sha256:" + hashlib.sha256(SEED_CAPTION.encode()).hexdigest(),
            preview=SEED_CAPTION, requester=AGENT, grant_id=None, grant_path=None, reason=None)
    if not any(a.summary == SEED_QUESTION for a in pending):
        approvals.create_question(question=SEED_QUESTION, options=["bright", "dark"], requester=AGENT_CODEX,
                                  grant_id=None, grant_path=None)


STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)


def _stop_on_signals(stop: asyncio.Event) -> None:
    """Ctrl-C and SIGTERM set `stop`. serve() takes these signals while it runs and gives them back
    when it returns, and before it starts there is no handler at all: a SIGTERM then ends the
    process with the temporary home still in place."""
    loop = asyncio.get_running_loop()
    for sig in STOP_SIGNALS:
        with suppress(NotImplementedError, RuntimeError, ValueError):
            loop.add_signal_handler(sig, stop.set)


def _release_signals() -> None:
    loop = asyncio.get_running_loop()
    for sig in STOP_SIGNALS:
        with suppress(NotImplementedError, RuntimeError, ValueError):
            loop.remove_signal_handler(sig)


async def run(*, home: Path | None, port: int, seed_lanes: bool, announce: Callable[[DevNode], None],
              stop: asyncio.Event | None = None, lane_manager: type[DevLaneManager] = DevLaneManager,
              instructions: str | None = None) -> None:
    """Serve the dev node until `stop` is set, Ctrl-C, or SIGTERM. `announce` gets the node's address
    and sign-in link once it listens. Without `home`, the node lives in a temporary folder, removed
    on every way out, signals included. A `home` the caller passed in is never removed.
    `instructions` replaces the node's MCP instructions (agentos studio up serves its own)."""
    stop = asyncio.Event() if stop is None else stop
    temporary = home is None
    _stop_on_signals(stop)  # before the folder exists: nothing between here and the try can be killed
    root = Path(tempfile.mkdtemp(prefix="agentos-dev-")) if temporary else home.expanduser()
    lanes: DevLaneManager | None = None
    try:
        prepare_home(root)
        token = secrets.token_urlsafe(32)
        config = NodeConfig(name="dev", home=root, mcp_host="127.0.0.1", mcp_port=port, mcp_token=token)
        runtime, lanes = build_runtime(config, lane_manager=lane_manager)
        if seed_lanes:
            await seed(runtime, lanes)
        _, agent_token = issue_agent_grant(runtime)

        def started(bound: int) -> None:
            link = create_login_code(root, base_url=f"http://localhost:{bound}")
            announce(DevNode(home=root, temporary=temporary, port=bound, token=token, agent_token=agent_token,
                             panel_link=link.url, mcp_url=f"http://127.0.0.1:{bound}/mcp",
                             claude_add=claude_add_line(bound, agent_token), panel_built=panel_built(),
                             lanes=lanes))

        await serve(config, runtime, stop=stop, on_started=started, panel=True, instructions=instructions)
    finally:
        _stop_on_signals(stop)  # serve() has given its signals back: a signal now only sets `stop` again
        try:
            if lanes is not None:
                await lanes.shutdown()  # idempotent: serve() has closed them already, unless it never ran
        finally:
            if temporary:
                shutil.rmtree(root, ignore_errors=True)  # runs even if the lane shutdown was interrupted
            _release_signals()
