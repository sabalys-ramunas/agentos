"""Bringing an environment up after it is created: the VM, its setup, the tunnels, git, the browser
lane and the clone, in that order, until it is `ready` or `failed`. A mixin of EnvironmentManager."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shlex
from typing import Any

from agentos.core.errors import AgentosError, LaneExists
from agentos.core.models import LaneSpec
from agentos.environments import machine
from agentos.environments.errors import (
    EnvironmentFailed,
    EnvironmentLimit,
)
from agentos.environments.models import (
    WORKSPACE,
    Environment,
)
from agentos.environments.probes import CHECK_TIMEOUT_S
from agentos.environments.tunnels import Tunnel

log = logging.getLogger("agentos.environments")

SETUP_POLL_S = 5.0


VM_POLL_S = 0.25


CLONE_TIMEOUT_S = 900.0


# Writes the VM's git config for the gateway, read from stdin (agentos.git.service.vm_gitconfig), to
# a file of its own that ~/.gitconfig includes once. Nothing secret is on the command line.
GIT_SETUP = ("umask 077 && mkdir -p ~/.config/agentos && cat > ~/.config/agentos/gitconfig && "
             "(git config --global --get-all include.path 2>/dev/null | grep -qx '~/.config/agentos/gitconfig' || "
             "git config --global --add include.path '~/.config/agentos/gitconfig')")


class BringUp:
    """The steps from `starting` to `ready`, for EnvironmentManager (its store, tunnels, tasks and services)."""

    def _spawn(self, env_id: str, coro: Any) -> None:
        task = asyncio.create_task(coro, name=f"environment-{env_id}")
        self._tasks[env_id] = task
        task.add_done_callback(lambda _t, i=env_id: self._tasks.pop(i, None) if self._tasks.get(i) is _t else None)

    async def wait_ready(self, env_id: str, wait_s: float) -> Environment:
        task = self._tasks.get(env_id)
        if task is not None and wait_s > 0:
            with contextlib.suppress(TimeoutError, asyncio.CancelledError, Exception):
                await asyncio.wait_for(asyncio.shield(task), timeout=wait_s)
        return self.store.get(env_id)

    async def _bring_up(self, env_id: str, plan: dict | None) -> None:
        env = self.store.get(env_id)
        try:
            vms = self.vms()
            if env.vm_id is None:
                if plan is None:
                    raise EnvironmentFailed("the node restarted before the machine was rented")
                cloud = machine.cloud_config() if plan["image"] == machine.BASE_IMAGE else ""
                try:
                    rec = await vms.create(env.terminal_lane, plan=plan, purpose=f"environment {env.id}: {env.title}",
                                           created_by=env.grant_id, wait_s=0, cloud_config=cloud, environment=env.id)
                except AgentosError as e:
                    if e.code == "vm_limit":
                        raise EnvironmentLimit(e.message) from None
                    raise
                def rented(e: Environment) -> None:
                    e.vm_id = rec.id
                    if rec.price_hourly_eur is not None:
                        e.price_hourly_eur = rec.price_hourly_eur
                    e.machine.server_type = rec.server_type

                env = self.store.update(env_id, rented)
            await self._wait_vm(env)
            await self._wait_setup(env)
            with contextlib.suppress(AgentosError):  # pnpm on an older snapshot (machine.TOOLS_TOP_UP)
                await self.envs().exec(env.terminal_lane, machine.TOOLS_TOP_UP, timeout_s=machine.TOOLS_TOP_UP_S)
            await self._start_tunnel(env)
            await self._make_browser(env)
            if env.repo:
                await self._clone(env)
            else:
                await self.envs().exec(env.terminal_lane, f"mkdir -p {machine.WORK}", timeout_s=CHECK_TIMEOUT_S)
            def ready(e: Environment) -> bool:
                if e.state != "starting":
                    return False
                e.state, e.ready_at = "ready", self._clock()
                return True

            env = self.store.update(env_id, ready)
            if env.state == "ready":
                log.info("environment %s ready (%s)", env.id, env.workdir)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            msg = str(getattr(e, "message", e)) or type(e).__name__
            log.warning("environment %s failed to come up: %s", env_id, msg)
            await self._fail(env_id, msg, code=e.code if isinstance(e, AgentosError) else None)

    async def _fail(self, env_id: str, why: str, *, code: str | None = None) -> None:
        def failed(e: Environment) -> None:
            e.error = why[:1200]
            e.error_code = code or "environment_failed"

        env = self.store.update(env_id, failed)
        await self._teardown(env, reason=f"failed to start: {why[:200]}", final="failed")

    async def _wait_vm(self, env: Environment) -> None:
        vms = self.vms()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + vms.settings.boot_timeout_s + 30
        while True:
            rec = vms.store.get(env.vm_id)
            if rec.state == "ready":
                return
            if not rec.live:
                raise EnvironmentFailed(f"the machine did not come up: {rec.error or rec.state}")
            if loop.time() > deadline:
                raise EnvironmentFailed(f"the machine did not answer SSH within {vms.settings.boot_timeout_s} s")
            await asyncio.sleep(VM_POLL_S)

    async def _wait_setup(self, env: Environment) -> None:
        envs = self.envs()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.setup_timeout_s
        said = ""
        ran = False
        while loop.time() < deadline:
            try:
                res = await envs.exec(env.terminal_lane, machine.SETUP_STATE, timeout_s=CHECK_TIMEOUT_S)
                said = (res.stdout or res.stderr or "").strip()
            except AgentosError as e:
                said = e.message
            first = said.split("\n", 1)[0]
            if first == "ready":
                return
            if "error" in first:
                raise EnvironmentFailed(f"the machine's setup failed: {said[-600:]}")
            if not ran and machine.boot_done_without_setup(first):
                # booted and done, but nothing ran the setup: a snapshot made another way (`agentos vm
                # image build` without the environment's script) or an image with no cloud-init
                ran = True
                left = max(60.0, deadline - loop.time())
                try:
                    out = await envs.exec(env.terminal_lane, "sh -s", stdin=machine.setup_script(), timeout_s=left)
                except AgentosError as e:
                    raise EnvironmentFailed(f"the machine's setup could not run: {e.message}") from None
                if out.exit_code != 0:
                    tail = (out.stderr or out.stdout or "").strip()[-600:]
                    raise EnvironmentFailed(f"the machine's setup failed (exit {out.exit_code}): {tail}")
                continue
            await asyncio.sleep(SETUP_POLL_S)
        raise EnvironmentFailed(f"the machine's setup did not finish within {self.settings.setup_timeout_s} s "
                                f"({said[-200:] or 'no answer'})")

    async def _start_tunnel(self, env: Environment) -> None:
        vms = self.vms()
        rec = vms.store.get(env.vm_id)
        old = self._tunnels.get(env.id)
        if old is not None and old.alive:
            return
        git = self._get("git")
        gateway = None
        if git is not None and callable(getattr(git, "vm_listener", None)):
            try:  # the gateway's listener for this VM alone: its token is good only through its own tunnel
                gateway = await git.vm_listener(env.terminal_lane)
            except Exception as e:  # noqa: BLE001 - the environment works without git through the node
                log.warning("environment %s: no git gateway listener: %s", env.id, e)
        tunnel = Tunnel(vms.remote(rec), gateway_port=gateway, socks_port=old.socks_port if old else None)
        await tunnel.start()
        self._tunnels[env.id] = tunnel
        self._wrap_proxies_loader()
        from agentos.node.interaction import VM_PROXIED_LANES

        VM_PROXIED_LANES.add(env.browser_lane)  # its localhost is the VM's: a page there may open it
        if gateway is not None:
            await self._git_setup(env)

    async def _git_setup(self, env: Environment) -> None:
        """Give the VM the one token it holds: a gateway token that fetches and pushes env.repo and
        nothing else, good only through this VM's own tunnel, revoked when the environment closes.
        No declared repository: no token at all. It reaches the VM's git config through the
        command's stdin, never a command line (which `ps` on the node shows)."""
        git = self._get("git")
        if git is None or not env.repo or not callable(getattr(git, "vm_token", None)):
            return
        from agentos.git.service import vm_gitconfig

        token = git.vm_token(env.terminal_lane, grant_id=env.grant_id, repo=env.repo)
        if token is None:
            return  # not declared for env: a public repository, cloned straight from the VM
        res = await self.envs().exec(env.terminal_lane, GIT_SETUP, stdin=vm_gitconfig(env.repo, token),
                                     timeout_s=CHECK_TIMEOUT_S)
        if res.exit_code != 0:
            raise EnvironmentFailed(f"setting up git on the machine failed: {(res.stderr or res.stdout).strip()[-300:]}")

    async def _make_browser(self, env: Environment) -> None:
        """A scratch browser lane made by the node: no account, no vault entry, no keepalive; its
        proxy is the environment's tunnel, so every request leaves from the VM."""
        lanes = self._get("lanes")
        if lanes is None:
            return
        with contextlib.suppress(Exception):
            lanes.get(env.browser_lane)
            return
        spec = LaneSpec(id=env.browser_lane, proxy_label=f"env-{env.id}")
        try:
            await lanes.create(spec)
        except LaneExists:
            return

    async def _clone(self, env: Environment) -> None:
        from agentos.git.wire import parse_remote

        remote = parse_remote(env.repo or "")
        name = remote.repo.rsplit("/", 1)[-1]
        dest = f"{machine.WORK}/{name}"
        git = self._get("git")
        declared = git is not None and git.remotes.find(remote, [env.terminal_lane, WORKSPACE]) is not None
        url = f"https://{remote.host}/{remote.repo}.git"
        q = shlex.quote
        checkout = f" && git -C {q(dest)} checkout -q {q(env.ref)}" if env.ref else ""
        cmd = (f"mkdir -p {machine.WORK} && if [ ! -d {q(dest)}/.git ]; then "
               f"git clone -q {q(url)} {q(dest)}{checkout}; fi && cd {q(dest)} && "
               "(git config --global user.name >/dev/null || git config --global user.name "
               f"{q(env.agent[:60] or 'agentos agent')}) && (git config --global user.email >/dev/null || "
               "git config --global user.email agent@agentos.invalid) && git rev-parse --abbrev-ref HEAD")
        # a declared repository goes through the gateway by the VM's own git config (_git_setup)
        res = await self.envs().exec(env.terminal_lane, cmd, timeout_s=CLONE_TIMEOUT_S)
        if res.exit_code != 0:
            from agentos.environments.repos import clone_failure

            raise clone_failure(remote.slug, (res.stderr or "") + "\n" + (res.stdout or ""), via_gateway=declared)
        branch = (res.stdout.strip().splitlines() or [None])[-1]

        def cloned(e: Environment) -> None:
            e.workdir, e.via_gateway, e.branch = dest, declared, branch

        self.store.update(env.id, cloned)
