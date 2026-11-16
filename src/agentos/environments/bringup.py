"""Bringing an environment up after it is created: the VM, its setup, the tunnels and git, then the
browser lane, the clone and the tools top-up at once, until it is `ready` or `failed`. Each step's
time is logged at INFO (Steps). A mixin of EnvironmentManager."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shlex
import time
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

SETUP_POLL_S = 1.0  # between setup checks; each check itself waits for cloud-init (machine.SETUP_WAIT_S)


VM_POLL_S = 0.25


CLONE_TIMEOUT_S = 900.0


# Writes the VM's git config for the gateway, read from stdin (agentos.git.service.vm_gitconfig), to
# a file of its own that ~/.gitconfig includes once. Nothing secret is on the command line.
GIT_SETUP = ("umask 077 && mkdir -p ~/.config/agentos && cat > ~/.config/agentos/gitconfig && "
             "(git config --global --get-all include.path 2>/dev/null | grep -qx '~/.config/agentos/gitconfig' || "
             "git config --global --add include.path '~/.config/agentos/gitconfig')")


class Steps:
    """How long each step of a bring-up took, logged at INFO as it ends (a failed one too), so a slow
    open says where its time went."""

    def __init__(self, env_id: str):
        self.env_id = env_id
        self.took: list[tuple[str, float]] = []
        self.started = time.monotonic()

    async def run(self, name: str, coro: Any) -> Any:
        t = time.monotonic()
        try:
            return await coro
        finally:
            took = time.monotonic() - t
            self.took.append((name, took))
            log.info("environment %s: %s took %.1f s", self.env_id, name, took)

    def total(self) -> float:
        return time.monotonic() - self.started

    def words(self) -> str:
        return ", ".join(f"{name} {took:.1f} s" for name, took in self.took)


async def together(*coros: Any) -> None:
    """Run the steps at once; the first that fails cancels the others and raises."""
    tasks = [asyncio.ensure_future(c) for c in coros]
    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


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
        steps = Steps(env_id)
        try:
            if env.vm_id is None:
                if plan is None:
                    raise EnvironmentFailed("the node restarted before the machine was rented")
                env = await steps.run("VM create", self._rent(env, plan))
            await steps.run("VM ready", self._wait_vm(env))
            tools = await steps.run("setup check", self._wait_setup(env))
            await self._set_up_work(env, steps, tools=tools)

            def ready(e: Environment) -> bool:
                if e.state != "starting":
                    return False
                e.state, e.ready_at = "ready", self._clock()
                return True

            env = self.store.update(env_id, ready)
            if env.state == "ready":
                log.info("environment %s ready (%s) in %.0f s: %s", env.id, env.workdir, steps.total(), steps.words())
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            from agentos.environments.failures import classify

            reason, msg = classify(e, env, self.settings.max_size)
            log.warning("environment %s failed to come up%s: %s", env_id, f" ({reason})" if reason else "", msg)
            code = e.code if isinstance(e, AgentosError) else None
            if code == "vm_error":
                code = None  # the provider's refusal is the environment's failure, with its reason
            if env.restore_from and code in (None, "environment_failed"):
                code = "restore_failed"  # its save is untouched: the next open with the title tries again
            await self._fail(env_id, msg, code=code, reason=reason)

    async def _rent(self, env: Environment, plan: dict) -> Environment:
        """Rent the machine (agentos.vms; its own fallbacks when the provider has no room) and record it."""
        cloud = machine.cloud_config() if plan["image"] == machine.BASE_IMAGE else ""
        try:
            rec = await self.vms().create(env.terminal_lane, plan=plan, purpose=f"environment {env.id}: {env.title}",
                                          created_by=env.grant_id, wait_s=0, cloud_config=cloud, environment=env.id,
                                          kept=env.keep == "live")
        except AgentosError as e:
            if e.code == "vm_limit":
                raise EnvironmentLimit(e.message) from None
            raise

        def rented(e: Environment) -> None:
            e.vm_id = rec.id
            if rec.price_hourly_eur is not None:
                e.price_hourly_eur = rec.price_hourly_eur
            e.machine.server_type, e.machine.location = rec.server_type, rec.location

        return self.store.update(env.id, rented)

    async def _top_up(self, env: Environment) -> None:
        with contextlib.suppress(AgentosError):  # pnpm on an older snapshot (machine.TOOLS_TOP_UP)
            await self.envs().exec(env.terminal_lane, machine.TOOLS_TOP_UP, timeout_s=machine.TOOLS_TOP_UP_S)

    async def _apt_ready(self, env: Environment) -> None:
        with contextlib.suppress(AgentosError):  # package lists, fetched in the background (machine.apt_ready)
            await self.envs().exec(env.terminal_lane, machine.apt_ready(), timeout_s=machine.APT_READY_S)

    async def _set_up_work(self, env: Environment, steps: Steps, *, tools: bool = True) -> None:
        """The tunnels first (the browser's traffic and the clone through the git gateway go over them),
        then the browser, the clone and the rest at once: each is a few seconds to a minute of its own.
        What needs no tunnel starts with them: the apt wrappers, the pnpm top-up (only where the setup
        check found no pnpm) and, in local mode, the agent's own ssh (agentos.environments.sshaccess).
        A resume waits for the browser and the tools first: its services start in it."""
        early = [asyncio.ensure_future(steps.run("apt ready", self._apt_ready(env)))]
        if tools:
            early.append(asyncio.ensure_future(steps.run("tools top-up", self._top_up(env))))
        if self.ssh_wanted():
            early.append(asyncio.ensure_future(steps.run("ssh", self.ssh_up(env))))
        try:
            await steps.run("tunnels and git", self._start_tunnel(env))
            if env.restore_from:
                await together(steps.run("browser", self._make_browser(env)), *early)
                await steps.run("restore", self._restore(env))
            elif env.repo:
                await together(steps.run("browser", self._make_browser(env)), steps.run("clone", self._clone(env)),
                               *early)
            else:
                mkdir = self.envs().exec(env.terminal_lane, f"mkdir -p {machine.WORK}", timeout_s=CHECK_TIMEOUT_S)
                await together(steps.run("browser", self._make_browser(env)), mkdir, *early)
        finally:
            for t in early:
                if not t.done():
                    t.cancel()
                    with contextlib.suppress(BaseException):
                        await t

    async def _fail(self, env_id: str, why: str, *, code: str | None = None, reason: str | None = None) -> None:
        def failed(e: Environment) -> None:
            e.error = why[:1200]
            e.error_code = code or "environment_failed"
            e.error_reason = reason if e.error_code == "environment_failed" else None

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
                raise EnvironmentFailed(f"the machine did not come up: {rec.error or rec.state}", reason="setup")
            if loop.time() > deadline:
                raise EnvironmentFailed(f"the machine did not answer SSH within {vms.settings.boot_timeout_s} s", reason="setup")
            await asyncio.sleep(VM_POLL_S)

    async def _wait_setup(self, env: Environment) -> bool:
        """Until the machine's setup is done. True when pnpm is missing there (the tools top-up runs)."""
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
                return machine.needs_pnpm(said)
            if "error" in first:
                raise EnvironmentFailed(f"the machine's setup failed: {said[-600:]}", reason="setup")
            if not ran and machine.boot_done_without_setup(first):
                # booted and done, but nothing ran the setup: a snapshot made another way (`agentos vm
                # image build` without the environment's script) or an image with no cloud-init
                ran = True
                left = max(60.0, deadline - loop.time())
                try:
                    out = await envs.exec(env.terminal_lane, "sh -s", stdin=machine.setup_script(), timeout_s=left)
                except AgentosError as e:
                    raise EnvironmentFailed(f"the machine's setup could not run: {e.message}", reason="setup") from None
                if out.exit_code != 0:
                    tail = (out.stderr or out.stdout or "").strip()[-600:]
                    raise EnvironmentFailed(f"the machine's setup failed (exit {out.exit_code}): {tail}", reason="setup")
                continue
            await asyncio.sleep(SETUP_POLL_S)
        raise EnvironmentFailed(f"the machine's setup did not finish within {self.settings.setup_timeout_s} s "
                                f"({said[-200:] or 'no answer'})", reason="setup")

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
        on_vm = {"vm_browser": True, "browser_port": old.browser_port if old else None} if env.browser == "vm" else {}
        tunnel = Tunnel(vms.remote(rec), gateway_port=gateway, socks_port=old.socks_port if old else None, **on_vm)
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
        proxy is the environment's tunnel, so every request leaves from the VM. With browser = "vm" the
        lane's browser runs on the machine instead (agentos.environments.vmbrowser)."""
        lanes = self._get("lanes")
        if lanes is None:
            return
        if env.browser == "vm":
            env = await self._browser_on_vm(env, lanes)
        if env.browser != "vm" and getattr(lanes, "makes_browsers", True) is False:
            return  # a node with no browser of its own (agentos local) and no browser on the machine: a terminal only
        from agentos.environments import signins

        signins.listen(self, lanes)  # a resume's sign-ins go into the browser when it starts
        with contextlib.suppress(Exception):
            lanes.get(env.browser_lane)
            return
        spec = LaneSpec(id=env.browser_lane, proxy_label=f"env-{env.id}")
        try:
            await lanes.create(spec)
        except LaneExists:
            return

    def vm_browsers(self) -> Any:
        """The machine side of "vm" browsers (agentos.environments.vmbrowser.VmBrowsers), made once. The
        local node installs its routers when it is built; any other node on its first "vm" browser."""
        if self._vm_browsers is None:
            from agentos.environments import vmbrowser

            self._vm_browsers = vmbrowser.VmBrowsers(self)
        return self._vm_browsers

    async def _browser_on_vm(self, env: Environment, lanes: Any) -> Environment:
        """Route the lane to the machine and make sure the machine has the browser. A lane manager that
        cannot be routed (the dev node's simulated lanes) keeps the browser on the node, and says so."""
        from agentos.environments import vmbrowser

        if not vmbrowser.install(lanes, self.vm_browsers()):
            log.warning("environment %s: this node's lanes cannot run a browser on the machine; it runs on the "
                        "node instead", env.id)

            def on_node(e: Environment) -> None:
                e.browser = "node"

            return self.store.update(env.id, on_node)
        try:
            await self._vm_browsers.ensure_installed(env)
        except AgentosError as e:
            raise EnvironmentFailed(e.message) from None
        return env

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
