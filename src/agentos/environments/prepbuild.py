"""Building a prepared machine, and keeping few of them. A mixin of EnvironmentManager.

A build runs on a machine of its own, never an agent's: rented from the base image (the one an open
boots), its setup checked, the repository cloned (through the git gateway for a declared one, as an
environment's clone; straight from the host for a public one) into /work/<name> with its canonical
remote URL, the repository's `setup` run there, then everything that must not be in a snapshot
removed (SCRUB: the gateway's git config and token, credentials, shell history, the login keys,
the host keys and the machine id, swap), and checked: no byte of the gateway token is left
on the disk. The token is revoked before the snapshot is taken, and the build machine is deleted after.

A build counts against the node's limits like any machine (max_live, the month's budget, one hour
booked at least); with no room it is not started and the open says so. Its lane is `env/prep-<hex>`,
which the git gateway serves like an open environment's terminal while the build runs
(building_lane).

Whose build it is: the grant that started it. An agent's open starts one under the agent's own grant
(never the owner's), `agentos environments prepare` under the owner's. Its gateway token is that grant's
and fetches only (no push, whatever the declaration allows), and the build stops (its machine deleted,
no snapshot) within GRANT_CHECK_S once that grant is revoked, a freeze included, or the environments
are frozen.

Retention (prepared_reap, from the environments' reaper): a newer build of a repository and ref (the
default branch, or one other ref: repoconfig.prepared_key) replaces the older one; one no open used for prepared_days goes; at most prepared_max on the node (the least
recently used go first); a build the node lost (a restart) is marked failed and its machine deleted.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import shlex
import time
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

from agentos.core.errors import AgentosError
from agentos.environments import machine
from agentos.environments.models import WORKSPACE
from agentos.environments.prepared import Prepared, eur_month, row
from agentos.environments.repoconfig import RepoConfig
from agentos.policy.grants import OWNER_GRANT_ID

log = logging.getLogger("agentos.environments")

BUILD_PREFIX = "prep-"
BUILD_MINUTES = 90                 # a build machine's time limit (its VM is deleted past it, whatever happened)
BUILD_WAIT_S = 600.0               # for the build machine to answer SSH
FAILED_KEEP = timedelta(days=1)    # a failed build's record (and its error) stays this long
SETUP_TIMEOUT_S = 1800.0
CLONE_TIMEOUT_S = 900.0
STEP_TIMEOUT_S = 120.0
GRANT_CHECK_S = 5.0                # how often a running step checks the build's grant still holds

# What must never be in a snapshot. The node writes each new machine's login key and host key itself
# (cloud-init's ssh_keys with ssh_deletekeys on a VM, the docker entrypoint in a container), so the old
# ones go too; machine-id is made again at boot. No browser profile is ever on a machine (a "vm" browser's
# lives on the node), and caches stay: they are what makes the next refresh fast. Swap goes so no
# process memory is kept (machine.SWAP_OFF).
SCRUB = f"""set -u
git config --global --unset-all include.path '~/.config/agentos/gitconfig' 2>/dev/null || true
rm -rf ~/.config/agentos ~/.git-credentials ~/.config/gh ~/.netrc ~/.docker/config.json
rm -f ~/.bash_history ~/.zsh_history ~/.python_history ~/.node_repl_history ~/.lesshst ~/.wget-hsts ~/.viminfo
rm -rf /tmp/agentos-* /run/agentos-* /var/tmp/agentos-*
rm -f /root/.ssh/authorized_keys /etc/ssh/ssh_host_* /etc/agentos-env/*
truncate -s 0 /etc/machine-id 2>/dev/null || true
{machine.UPGRADE_WAIT}; {machine.SWAP_OFF}
sync
echo ok=1
"""
# Is any byte of the token (on stdin) left anywhere a snapshot keeps? Prints the files that hold it.
TOKEN_LEFT = ("grep -rlF -f - /root /work /etc /home /tmp /var/tmp /opt /usr/local/etc 2>/dev/null | head -n 5; "
              "true")


class BuildFailed(Exception):
    """A build that did not make its image: its words go on the prepared machine's record (never to an agent)."""


def _tail(text: str, n: int = 1500) -> str:
    return text.strip()[-n:]


class PrepBuild:
    """For EnvironmentManager: its settings, clock, _get, vms(), envs(), machines(), count_cap(),
    booked_eur(), prepared_store(), _wait_setup() and _tail()."""

    def _builds(self) -> dict[str, asyncio.Task]:
        return self.__dict__.setdefault("_prepared_builds", {})

    def _build_lanes(self) -> set[str]:
        return self.__dict__.setdefault("_prepared_lanes", set())

    def building_lane(self, lane_id: str) -> bool:
        """Is this a build machine's lane while its build runs (the git gateway serves it then)?"""
        return lane_id in self._build_lanes()

    def _room_for(self, plan: dict, *, minutes: int) -> str | None:
        """None when the node may rent one more machine of `plan` for `minutes` now; else why not."""
        open_, others = self.machines()
        warm = self.warm_vms() if callable(getattr(self, "warm_vms", None)) else []
        cap, n = self.count_cap(), len(open_) + len(others) + len(warm)
        if n >= cap:
            return (f"the node runs {n} machine{'s' if n != 1 else ''}, its limit at once (the owner raises it "
                    "with `agentos environments allow --max-live N`)")
        vms = self.vms()
        if len(vms.store.live()) >= vms.max_live:
            return f"the node runs {len(vms.store.live())} VMs, its limit at once (vms.toml max_live)"
        budget, price = self.settings.monthly_eur, plan.get("price_hourly_eur")
        if budget and price:
            cost = price * -(-minutes // 60)
            booked = self.booked_eur()
            if booked + cost > budget:
                return (f"EUR {cost:.2f} more would pass the monthly budget of EUR {budget:.2f} (EUR {booked:.2f} "
                        "spent and booked)")
        return None

    async def start_build(self, *, repo: str, ref: str | None, key: str, config: RepoConfig, base_image: str,
                          base_id: str, grant_id: str = OWNER_GRANT_ID, wait: bool = False,
                          force: bool = False) -> dict:
        """Start building the prepared machine `key` in the background (with `wait`, until it is done):
        {"started": True} or {"started": False, "why": ...}. The open never waits for it."""
        if not self.settings.prepared_builds or self.settings.prepared_max == 0:
            return {"started": False, "why": "prepared machines are off on this node (environments.toml "
                                             "prepared_builds, prepared_max)"}
        task = self._builds().get(key)
        if task is not None and not task.done():
            return {"started": False, "why": "one is being built already"}
        had = self.prepared_store().get(key)
        if had is not None and had.state == "ready":
            if not force:
                return {"started": False, "why": "it is ready already"}
            if not await self._drop_prepared(had, "built again by the owner"):
                return {"started": False, "why": "its old image could not be deleted; try again in a minute"}
        vms = self.vms()
        lane = f"{WORKSPACE}/{BUILD_PREFIX}{secrets.token_hex(5)}"
        try:
            plan = await vms.plan(lane, profile=self.settings.profile, ttl_minutes=BUILD_MINUTES, image=base_image)
        except AgentosError as e:
            return {"started": False, "why": f"the build machine could not be planned: {e.message[:200]}"}
        why = self._room_for(plan, minutes=BUILD_MINUTES)
        if why is not None:
            log.info("prepared machine of %s: no build now: %s", repo, why)
            return {"started": False, "why": f"no room for a build now: {why}"}
        rec = Prepared(key=key, repo=repo, ref=ref, base_image=base_id, started_at=self._clock(),
                       provider=getattr(vms.provider(), "name", ""), step="renting the build machine")
        self.prepared_store().put(rec)
        task = asyncio.create_task(self._build(rec, lane, plan, config, base_image, grant_id),
                                   name=f"prepared-{key[:12]}")
        self._builds()[key] = task
        task.add_done_callback(lambda t, k=key: self._builds().pop(k, None) if self._builds().get(k) is t else None)
        if wait:
            with contextlib.suppress(Exception):
                await asyncio.shield(task)
        return {"started": True, "key": key}

    def _step(self, key: str, words: str, **more: Any) -> None:
        def change(r: Prepared) -> None:
            r.step = words
            for k, v in more.items():
                setattr(r, k, v)

        self.prepared_store().update(key, change)
        log.info("prepared machine %s: %s", key[:12], words)

    async def _build(self, rec: Prepared, lane: str, plan: dict, config: RepoConfig, base_image: str,
                     grant_id: str) -> None:
        vms, envs = self.vms(), self.envs()
        began = time.monotonic()
        vm_id: str | None = None
        tunnel: Any = None
        self._build_lanes().add(lane)
        try:
            cloud = machine.cloud_config() if base_image == machine.BASE_IMAGE else ""
            vrec = await vms.create(lane, plan=plan, purpose=f"prepared machine of {rec.repo}", created_by="owner",
                                    wait_s=0, cloud_config=cloud)
            vm_id = vrec.id
            self._step(rec.key, "the build machine boots", build_vm=vm_id)
            await self._build_vm_ready(vm_id)
            await self._wait_setup(SimpleNamespace(terminal_lane=lane, id=lane))  # type: ignore[arg-type]
            with contextlib.suppress(AgentosError):
                await envs.exec(lane, machine.apt_ready(), timeout_s=machine.APT_READY_S)
            with contextlib.suppress(AgentosError):
                await envs.exec(lane, machine.SWAP_ON, timeout_s=machine.SWAP_ON_S)
            self._step(rec.key, "cloning the repository")
            self._check_grant(grant_id, lane)
            tunnel, token = await self._build_git(lane, rec.repo, vms.store.get(vm_id), grant_id)
            dest, commit = await self._while_granted(grant_id, lane, self._build_clone(lane, rec.repo, rec.ref))
            self._step(rec.key, "running setup", commit=commit)
            t = time.monotonic()
            res = await self._while_granted(grant_id, lane, envs.exec(
                lane, f"cd {shlex.quote(dest)} && CI=true COREPACK_ENABLE_DOWNLOAD_PROMPT=0 bash -c "
                f"{shlex.quote(config.setup or 'true')}", timeout_s=SETUP_TIMEOUT_S))
            setup_s = round(time.monotonic() - t, 1)
            if res.exit_code != 0:
                raise BuildFailed(f"setup failed with exit code {res.exit_code}: "
                                  f"{self._tail((res.stdout or '') + chr(10) + (res.stderr or ''))}")
            self._step(rec.key, "removing credentials before the snapshot", setup_s=setup_s)
            tunnel = await self._build_git_down(lane, tunnel)
            await self._scrub_for_snapshot(lane, token)
            self._check_grant(grant_id, lane)
            self._step(rec.key, "taking the snapshot")
            made = await vms.snapshot(vm_id, description=f"agentos prepared machine of {rec.repo}"[:200],
                                      labels={"agentos-prepared": rec.key[:16], "agentos-repo": rec.repo})
            await self._build_vm_delete(vm_id, "prepared machine built")
            cost = self._build_cost(vm_id)
            now = self._clock()

            def ready(r: Prepared) -> None:
                r.state, r.image, r.gb, r.built_at, r.step, r.error = "ready", made["id"], made.get("gb"), now, "", None
                r.build_s, r.build_vm, r.build_cost_eur = round(time.monotonic() - began, 1), None, cost

            if self.prepared_store().update(rec.key, ready) is None:  # forgotten while it was built
                with contextlib.suppress(Exception):
                    await vms.delete_image(made["id"])
                return
            log.info("prepared machine of %s ready: %s (%.0f s, setup %.0f s)", rec.repo, made["id"],
                     time.monotonic() - began, setup_s)
            await self._retire_older(rec)
        except BaseException as e:
            words = (str(e) if isinstance(e, BuildFailed) else e.message if isinstance(e, AgentosError)
                     else f"{type(e).__name__}: {e}")
            if isinstance(e, asyncio.CancelledError):
                words = "the build was stopped (the node shut down)"
            log.warning("prepared machine of %s failed: %s", rec.repo, words)
            self._step(rec.key, "", state="failed", error=_tail(str(words)), build_vm=None,
                       build_s=round(time.monotonic() - began, 1))
            with contextlib.suppress(BaseException):
                await self._build_git_down(lane, tunnel)
            if vm_id is not None:
                with contextlib.suppress(BaseException):
                    await self._build_vm_delete(vm_id, "prepared machine build failed")
            if isinstance(e, asyncio.CancelledError):
                raise
        finally:
            self._build_lanes().discard(lane)

    def _build_cost(self, vm_id: str) -> float | None:
        with contextlib.suppress(Exception):
            return self.vms().store.get(vm_id).cost_eur(self._clock())
        return None

    async def _build_vm_ready(self, vm_id: str) -> None:
        vms = self.vms()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + min(BUILD_WAIT_S, vms.settings.boot_timeout_s + 30)
        while True:
            r = vms.store.get(vm_id)
            if r.state == "ready":
                return
            if not r.live:
                raise BuildFailed(f"the build machine did not come up: {r.error or r.state}")
            if loop.time() > deadline:
                raise BuildFailed("the build machine did not answer SSH in time")
            await asyncio.sleep(0.5)

    async def _build_vm_delete(self, vm_id: str, reason: str) -> None:
        vms = self.vms()
        r = vms.store.get(vm_id)
        if r.live:
            await vms.destroy(vm_id, reason=reason)

    def _check_grant(self, grant_id: str, lane: str) -> None:
        """BuildFailed when the grant that started the build no longer holds (revoked, a freeze's revocation
        included) or the environments are frozen. The owner's own build (the CLI) is not an agent's."""
        if grant_id == OWNER_GRANT_ID:
            return
        policy = self._get("policy")
        if policy is not None and callable(getattr(policy, "grant_by_id", None)):
            try:
                policy.grant_by_id(grant_id)
            except AgentosError as e:
                raise BuildFailed(f"the build was stopped: the grant that started it no longer holds ({e.message[:200]})"
                                  ) from None
        freeze = self._get("freeze")
        if freeze is not None and callable(getattr(freeze, "frozen_for", None)) \
                and freeze.frozen_for(lane, WORKSPACE) is not None:
            raise BuildFailed("the build was stopped: the environments are frozen")

    async def _while_granted(self, grant_id: str, lane: str, step: Any) -> Any:
        """Run one step of the build, checking every GRANT_CHECK_S that its grant still holds (_check_grant);
        the step is cancelled when it does not (the failed build then deletes the machine)."""
        task = asyncio.ensure_future(step)
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=GRANT_CHECK_S)
                if done:
                    return task.result()
                self._check_grant(grant_id, lane)
        finally:
            if not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task

    async def _build_git(self, lane: str, repo: str, vrec: Any, grant_id: str) -> tuple[Any, str | None]:
        """A declared repository: the build machine reaches the git gateway through its own tunnel, with
        a token for this repository alone, of the build's grant and fetch-only (written to its git config
        through stdin, removed and revoked before the snapshot). A public one: nothing, it clones straight
        from the host."""
        from agentos.environments import bringup

        git = self._get("git")
        if git is None or not callable(getattr(git, "vm_listener", None)):
            return None, None
        from agentos.git.wire import parse_remote

        if git.remotes.find(parse_remote(repo), [lane, WORKSPACE]) is None:
            return None, None
        port = await git.vm_listener(lane)
        tunnel = bringup.Tunnel(self.vms().remote(vrec), gateway_port=port)
        await tunnel.start()
        token = git.vm_token(lane, grant_id=grant_id, repo=repo, fetch_only=True)
        if token is None:
            return tunnel, None
        from agentos.git.service import vm_gitconfig

        res = await self.envs().exec(lane, bringup.GIT_SETUP, stdin=vm_gitconfig(repo, token), timeout_s=STEP_TIMEOUT_S)
        if res.exit_code != 0:
            raise BuildFailed(f"setting up git on the build machine failed: {_tail(res.stderr or res.stdout, 300)}")
        return tunnel, token

    async def _build_git_down(self, lane: str, tunnel: Any) -> None:
        """The build machine's token ends, and its tunnel and listener close (before the snapshot)."""
        if tunnel is not None:
            with contextlib.suppress(Exception):
                await tunnel.stop()
        git = self._get("git")
        if git is not None and callable(getattr(git, "vm_down", None)):
            with contextlib.suppress(Exception):
                await git.vm_down(lane)

    async def _build_clone(self, lane: str, repo: str, ref: str | None) -> tuple[str, str]:
        """The clone, with the canonical https URL as its remote (an environment's own git config, written
        at its bring-up, decides where that goes), at `ref`. (folder, commit)."""
        from agentos.git.wire import parse_remote

        remote = parse_remote(repo)
        dest = f"{machine.WORK}/{remote.repo.rsplit('/', 1)[-1]}"
        q = shlex.quote
        checkout = f" && git checkout -q {q(ref)}" if ref else ""
        cmd = (f"mkdir -p {machine.WORK} && rm -rf {q(dest)} && GIT_TERMINAL_PROMPT=0 git clone -q "
               f"{q(f'https://{remote.host}/{remote.repo}.git')} {q(dest)} && cd {q(dest)}{checkout} && git rev-parse HEAD")
        res = await self.envs().exec(lane, cmd, timeout_s=CLONE_TIMEOUT_S)
        if res.exit_code != 0:
            raise BuildFailed(f"the clone failed: {_tail((res.stderr or '') + (res.stdout or ''), 600)}")
        return dest, (res.stdout or "").strip().splitlines()[-1] if (res.stdout or "").strip() else ""

    async def _scrub_for_snapshot(self, lane: str, token: str | None) -> None:
        envs = self.envs()
        res = await envs.exec(lane, SCRUB, timeout_s=STEP_TIMEOUT_S)
        if "ok=1" not in (res.stdout or ""):
            raise BuildFailed(f"the build machine could not be cleaned for its snapshot: {_tail(res.stderr or '', 300)}")
        if token:
            left = await envs.exec(lane, TOKEN_LEFT, stdin=token + "\n", timeout_s=STEP_TIMEOUT_S)
            files = [f for f in (left.stdout or "").splitlines() if f.strip()]
            if files:
                raise BuildFailed(f"the gateway token is still on the build machine ({', '.join(files[:3])}), so no "
                                  "snapshot was taken; the setup command wrote it there")

    # ================================================================ retention
    async def _drop_prepared(self, rec: Prepared, why: str) -> bool:
        """Delete its image at the provider and its record. False when the provider refused (tried again)."""
        if rec.image:
            try:
                await self.vms().delete_image(rec.image)
            except Exception as e:  # noqa: BLE001 - kept, and tried again on the next pass
                log.warning("prepared machine of %s: its image %s stays for now: %s", rec.repo, rec.image, e)
                return False
        self.prepared_store().remove(rec.key)
        log.info("prepared machine of %s (%s) deleted: %s", rec.repo, rec.image or "no image", why)
        return True

    async def _retire_older(self, new: Prepared) -> None:
        for rec in self.prepared_store().of_repo(new.repo):
            if rec.key != new.key and rec.ref == new.ref and rec.state == "ready":
                await self._drop_prepared(rec, "a newer build replaced it")

    async def prepared_reap(self, now: Any = None) -> dict:
        """One pass of the retention rules (the reaper's): {"deleted": [repo, ...], "lost": [...]}."""
        now = now or self._clock()
        out: dict[str, list[str]] = {"deleted": [], "lost": []}
        store = self.prepared_store()
        for rec in store.all():
            if rec.state == "building" and rec.key not in self._builds():
                await self._lost_build(rec, out)
            elif rec.state == "failed" and now - rec.started_at > FAILED_KEEP:
                store.remove(rec.key)
        ready = sorted((r for r in store.all() if r.state == "ready"), key=lambda r: r.last_used(), reverse=True)
        newest: dict[tuple[str, str], Prepared] = {}
        for rec in sorted(ready, key=lambda r: r.built_at or r.started_at, reverse=True):
            newest.setdefault((rec.repo, rec.ref or ""), rec)
        keep = 0
        for rec in ready:
            if newest.get((rec.repo, rec.ref or "")) is not rec:
                why = "a newer build replaced it"
            elif now - rec.last_used() > timedelta(days=self.settings.prepared_days):
                why = f"no open used it for {self.settings.prepared_days} days"
            elif keep >= self.settings.prepared_max:
                why = f"the node keeps at most {self.settings.prepared_max} (prepared_max)"
            else:
                keep += 1
                continue
            if await self._drop_prepared(rec, why):
                out["deleted"].append(rec.repo)
        return out

    async def _lost_build(self, rec: Prepared, out: dict) -> None:
        """A build no task runs (the node restarted during it): failed, and its machine deleted."""
        self._step(rec.key, "", state="failed", error="the node restarted during the build", build_vm=None)
        if rec.build_vm:
            with contextlib.suppress(Exception):
                await self._build_vm_delete(rec.build_vm, "prepared machine build lost in a restart")
        out["lost"].append(rec.repo)

    async def forget_prepared(self, repo: str) -> list[str]:
        """Delete every prepared machine of `repo` (`agentos environments prepared forget`). The keys deleted."""
        gone = []
        for rec in self.prepared_store().of_repo(repo):
            if rec.state == "building" and rec.key in self._builds():
                self._builds()[rec.key].cancel()
                continue
            if await self._drop_prepared(rec, "forgotten by the owner"):
                gone.append(rec.key)
        return gone

    async def prepare_now(self, repo: str, ref: str | None = None, *, force: bool = False) -> dict:
        """`agentos environments prepare`: read the repository's file and start its build now (agentosd
        runs it; the CLI follows the store). Raises InvalidRequest when there is nothing to build."""
        from agentos.core.errors import InvalidRequest
        from agentos.git.wire import parse_remote

        repo = parse_remote(repo).slug
        p = await self.prepared_for("prepare", repo, ref, grant_id="owner")
        if not p.get("config") or not p.get("key"):
            raise InvalidRequest(f"nothing to prepare for {repo}: " + self._why_not_prepared(p))
        out = await self.start_build(repo=repo, ref=ref, key=p["key"], config=RepoConfig(**p["config"]),
                                     base_image=p.get("base_image") or "", base_id=p.get("base_id") or "",
                                     force=force)
        if not out.get("started") and out.get("why") != "one is being built already":
            raise InvalidRequest(f"no build of {repo}: {out.get('why')}"
                                 + ("; `--force` builds it again" if out.get("why") == "it is ready already" else ""))
        return {"repo": repo, "key": p["key"], "started": bool(out.get("started")), "why": out.get("why")}

    def prepared_rows(self) -> list[dict]:
        now = self._clock()
        rate = self.settings.snapshot_eur_per_gb_month
        return [row(r, rate, now) for r in sorted(self.prepared_store().all(), key=lambda r: r.repo)]

    def prepared_eur_month(self) -> float:
        rate = self.settings.snapshot_eur_per_gb_month
        return round(sum(eur_month(r, rate) for r in self.prepared_store().all() if r.state == "ready"), 4)
