"""The warm pool: machines booted ahead of an open, so an open that matches one skips the rent and the
boot. Off by default (`warm = 0` in environments.toml: nothing bills until an agent opens a machine).
A mixin of EnvironmentManager.

What is warmed: a VM rented from the image the newest open booted (a prepared machine's, or the base),
with the default size and location, up to SSH-ready, and its setup checked, the apt wrappers in place
and swap on. Nothing that belongs to a chat or a repository: no lane of an environment, no tunnel, no
token, no clone. Its lane is `env/warm-<hex>`, its time limit `warm_minutes` (on Hetzner, which bills
each started hour, it is deleted before its first hour ends).

When: a chat connects (session_hint, from agentos.mcp.http, for a grant that may open machines), and
after each open (after_ready), always within the node's limits: a warm machine counts against the
machines at once and books one hour against the month's budget. It yields to any open: a full node
deletes a warm machine that does not match before it rents (make_room), and opens do not count warm
machines as taken (Sizing.machines).

Claim (claim_warm, in BringUp._rent): a ready warm machine with the open's image, server type and
location becomes the environment's. The environment's record names the machine first, then the VM's
record takes the environment's lane, purpose, creator and time limit, then the terminal lane moves. A
crash between the steps leaves the VM recorded and live, so either the environment's failed bring-up
deletes it or the VM reaper does at its time limit (warm_minutes): nothing bills for ever.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from datetime import datetime
from types import SimpleNamespace
from typing import Any

from agentos.core import clock
from agentos.core.errors import AgentosError
from agentos.environments import machine
from agentos.environments.models import WORKSPACE, Environment

log = logging.getLogger("agentos.environments")

WARM_PREFIX = "warm-"
HINT_EVERY_S = 30.0   # session hints closer together than this are one


def is_warm(rec: Any) -> bool:
    return str(getattr(rec, "lane_id", "")).startswith(f"{WORKSPACE}/{WARM_PREFIX}")


def is_side(rec: Any) -> bool:
    """A machine of the node's own (warm, or a prepared machine's build), not of an environment."""
    from agentos.environments.prepbuild import BUILD_PREFIX

    return is_warm(rec) or str(getattr(rec, "lane_id", "")).startswith(f"{WORKSPACE}/{BUILD_PREFIX}")


class WarmPool:
    """For EnvironmentManager: its settings, store, clock, _get, vms(), envs(), machines(), count_cap(),
    booked_eur(), prepared_store(), _image() and _wait_setup()."""

    def _pool_lock(self) -> asyncio.Lock:
        """Held by a claim and by a drop (short), never while a warm machine is rented."""
        return self.__dict__.setdefault("_warm_lock", asyncio.Lock())

    def _fill_lock(self) -> asyncio.Lock:
        """One fill at a time."""
        return self.__dict__.setdefault("_warm_fill_lock", asyncio.Lock())

    def _pool_tasks(self) -> set[asyncio.Task]:
        return self.__dict__.setdefault("_warm_tasks", set())

    def _pool_settings(self) -> Any:
        """The settings, with `warm` and `warm_minutes` read again when environments.toml changed, so
        `agentos environments allow --warm N` takes effect on a running node."""
        from agentos.environments.models import SETTINGS_FILE, load_settings

        try:
            stamp = (self.home / SETTINGS_FILE).stat().st_mtime_ns
        except OSError:
            return self.settings
        if self.__dict__.get("_pool_stamp") != stamp:
            self.__dict__["_pool_stamp"] = stamp
            with contextlib.suppress(Exception):  # a file agentosd would not start with: keep what it has
                fresh = load_settings(self.home)
                self.settings.warm, self.settings.warm_minutes = fresh.warm, fresh.warm_minutes
        return self.settings

    def side_tasks(self) -> list[asyncio.Task]:
        """The pool's and the builds' tasks, for the manager's shutdown."""
        builds = self.__dict__.get("_prepared_builds") or {}
        return [*self._pool_tasks(), *builds.values()]

    def warm_vms(self) -> list[Any]:
        """The warm machines alive now, oldest first."""
        with contextlib.suppress(AgentosError):
            return sorted((r for r in self.vms().store.live() if is_warm(r)), key=lambda r: r.created_at)
        return []

    def side_booked_eur(self, now: datetime) -> float:
        """This month's warm machines and builds, each live one booked to its time limit: they count
        against the month's budget like an environment does."""
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        total = 0.0
        with contextlib.suppress(AgentosError):
            for r in self.vms().store.since(start):
                if not is_side(r):
                    continue
                spent = r.cost_eur(now) or 0.0
                if r.live and r.price_hourly_eur is not None:
                    hours = max(1, -(-int(max(0.0, (r.expires_at - r.created_at).total_seconds())) // 3600))
                    spent = max(spent, hours * r.price_hourly_eur)
                total += spent
        return round(total, 4)

    # ================================================================ filling
    def session_hint(self, token: str | None) -> None:
        """A chat connected (a new MCP session): fill the pool when it is on and the token may open machines.
        Cheap and never raises: the work runs in the background."""
        try:
            if self._pool_settings().warm <= 0:
                return
            now = time.monotonic()  # a client with no session id looks new on every call: at most one hint a while
            if now - self.__dict__.get("_hinted", -HINT_EVERY_S) < HINT_EVERY_S or not self._may_open(token):
                return
            self.__dict__["_hinted"] = now
            self.pool_refill_soon()
        except Exception:
            log.debug("warm pool: the session hint failed", exc_info=True)

    def _may_open(self, token: str | None) -> bool:
        policy = self._get("policy")
        if policy is None:
            return True  # no policy on this node: every caller is the owner
        from agentos.core.models import Effect

        try:
            _, grant = policy.resolve(token)
            return bool(grant.effect_allowed(Effect.VM))
        except Exception:  # noqa: BLE001 - a token that does not resolve opens nothing
            return False

    def pool_refill_soon(self) -> None:
        if self._pool_settings().warm <= 0:
            return
        if any(not t.done() for t in self._pool_tasks()):
            return
        task = asyncio.create_task(self.fill_pool(), name="environments-warm-fill")
        self._pool_tasks().add(task)
        task.add_done_callback(self._pool_tasks().discard)

    async def _warm_image(self) -> str:
        """The image the newest open booted, unless it was a prepared machine's that is gone since (a newer
        build replaced it): then the base image."""
        base = await self._image()
        last = self.prepared_store().last_image()
        ready = {r.image for r in self.prepared_store().all() if r.state == "ready" and r.image}
        if last is None or (last not in ready and last not in (base, machine.BASE_IMAGE, machine.SNAPSHOT_IMAGE)):
            return base
        return last

    async def fill_pool(self) -> list[str]:
        """Rent warm machines up to `warm`, within the limits. The VM ids made. A warm machine of another
        image than the newest open's is replaced."""
        made: list[str] = []
        async with self._fill_lock():
            want = self._pool_settings().warm
            if want <= 0:
                return made
            image = await self._warm_image()
            for r in self.warm_vms():
                if r.image != image:
                    await self._drop_warm(r, "the newest open used another image")
            while len(self.warm_vms()) < want:
                vm_id = await self._rent_warm(image)
                if vm_id is None:
                    break
                made.append(vm_id)
        return made

    async def _rent_warm(self, image: str) -> str | None:
        vms = self.vms()
        lane = f"{WORKSPACE}/{WARM_PREFIX}{secrets.token_hex(5)}"
        try:
            profile, custom = self._size(None)
            plan = await vms.plan(lane, profile=profile, size=custom, ttl_minutes=self.settings.warm_minutes,
                                  image=image)
        except AgentosError as e:
            log.info("warm pool: no plan: %s", e.message)
            return None
        why = self._room_for(plan, minutes=self.settings.warm_minutes)
        if why is not None:
            log.info("warm pool: no warm machine now: %s", why)
            return None
        cloud = machine.cloud_config() if image == machine.BASE_IMAGE else ""
        try:
            rec = await vms.create(lane, plan=plan, purpose="warm machine for the next open", created_by="owner",
                                   wait_s=0, cloud_config=cloud)
        except AgentosError as e:
            log.info("warm pool: the rent was refused: %s", e.message)
            return None
        log.info("warm pool: %s warming (%s, %s)", rec.id, rec.server_type, image)
        task = asyncio.create_task(self._prework(rec.id, lane), name=f"environments-warm-{rec.id}")
        self._pool_tasks().add(task)
        task.add_done_callback(self._pool_tasks().discard)
        return rec.id

    async def _prework(self, vm_id: str, lane: str) -> None:
        """Up to SSH-ready, then what any environment's bring-up does first and no chat owns: the setup
        check, the apt wrappers, swap. Stops when it was claimed meanwhile (its lane moved)."""
        vms, envs = self.vms(), self.envs()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + vms.settings.boot_timeout_s + 30
        while loop.time() < deadline:
            r = vms.store.get(vm_id)
            if not r.live or r.lane_id != lane:
                return
            if r.state == "ready":
                break
            await asyncio.sleep(1.0)
        else:
            return
        with contextlib.suppress(Exception):
            await self._wait_setup(SimpleNamespace(terminal_lane=lane, id=vm_id))  # type: ignore[arg-type]
            await envs.exec(lane, machine.apt_ready(), timeout_s=machine.APT_READY_S)
            await envs.exec(lane, machine.SWAP_ON, timeout_s=machine.SWAP_ON_S)
            log.info("warm pool: %s ready for an open", vm_id)

    async def _drop_warm(self, rec: Any, why: str) -> None:
        async with self._pool_lock():
            await self._drop_warm_now(rec, why)

    async def _drop_warm_now(self, rec: Any, why: str) -> None:
        """Delete it, unless an open claimed it meanwhile (under the pool lock)."""
        with contextlib.suppress(Exception):
            fresh = self.vms().store.get(rec.id)
            if not fresh.live or not is_warm(fresh):
                return
            await self.vms().destroy(rec.id, reason=f"warm machine: {why}")
            log.info("warm pool: %s deleted: %s", rec.id, why)

    # ================================================================ claiming
    async def claim_warm(self, env: Environment, plan: dict) -> Any | None:
        """A ready warm machine for this open's plan (same image, server type and location), bound to the
        environment; None when there is none."""
        async with self._pool_lock():
            vms = self.vms()
            found = next((r for r in self.warm_vms() if r.state == "ready" and r.image == plan["image"]
                          and r.server_type == plan["server_type"] and r.location == plan["location"]), None)
            if found is None:
                return None
            old = found.lane_id

            def bound(e: Environment) -> None:  # the environment names it first: a crash after this deletes it
                e.vm_id, e.warm_claimed = found.id, True
                if found.price_hourly_eur is not None:
                    e.price_hourly_eur = found.price_hourly_eur
                e.machine.server_type, e.machine.location = found.server_type, found.location

            self.store.update(env.id, bound)
            rec = vms.store.get(found.id)
            rec.lane_id, rec.environment, rec.created_by = env.terminal_lane, env.id, env.grant_id
            rec.purpose = f"environment {env.id}: {env.title}"[:300]
            rec.expires_at, rec.kept = env.expires_at, env.keep == "live"
            vms.store.put(rec)
            envs = self.envs()
            with contextlib.suppress(Exception):
                if envs.has(old):
                    await envs.remove(old, vm_ok=True)
            if not envs.has(env.terminal_lane):
                envs.create_remote(env.terminal_lane, vms.remote(rec))
            await self._relabel(rec)
            log.info("environment %s: claimed warm machine %s (warm for %.0f s)", env.id, rec.id,
                     (self._clock() - rec.created_at).total_seconds())
            return rec

    async def _relabel(self, rec: Any) -> None:
        """The provider's labels follow (agentos-lane), where it can change them (Hetzner; not docker).
        The reaper's reconcile reads only agentos-vm, so a provider that cannot is fine."""
        prov = self.vms().provider()
        relabel = getattr(prov, "update_labels", None)
        labels_of = getattr(self.vms(), "_labels", None)
        if not callable(relabel) or not callable(labels_of) or not rec.provider_id:
            return
        with contextlib.suppress(Exception):
            await relabel(rec.provider_id, labels_of(rec.id, rec.lane_id))

    async def make_room(self) -> None:
        """Before an open rents: when the node runs as many VMs as it may and a warm machine holds a slot,
        that warm machine goes first (an open always wins over the pool)."""
        vms = self.vms()
        async with self._pool_lock():
            warm = self.warm_vms()
            if warm and len(vms.store.live()) >= vms.max_live:
                await self._drop_warm_now(warm[0], "its slot went to an open")

    # ================================================================ the reaper
    async def pool_reap(self, now: datetime | None = None) -> list[str]:
        """Warm machines past warm_minutes or past the `warm` count go; the ones kept are touched, so the
        VM reaper's idle rule leaves them to their time limit. The VM ids deleted."""
        now = now or self._clock()
        gone: list[str] = []
        warm = self.warm_vms()
        keep = max(0, self._pool_settings().warm)
        for i, r in enumerate(reversed(warm)):  # newest first: the oldest go when there are too many
            old = (now - r.created_at).total_seconds() >= self.settings.warm_minutes * 60
            if old or i >= keep:
                await self._drop_warm(r, "unclaimed for warm_minutes" if old else "more than warm in the pool")
                gone.append(r.id)
                continue
            touch = getattr(getattr(self._get("envs"), "store", None), "touch", None)
            if callable(touch):
                with contextlib.suppress(Exception):
                    touch(r.lane_id, now)
        return gone

    def pool_view(self) -> dict:
        """The pool for report(), environment() and the CLI."""
        self._pool_settings()
        now = self._clock()
        rows = []
        for r in self.warm_vms():
            rows.append({"vm": r.id, "state": r.state, "since": clock.iso_or_none(r.created_at, z=True),
                         "image": r.image, "server_type": r.server_type, "location": r.location,
                         "cost_eur": r.cost_eur(now), "price_hourly_eur": r.price_hourly_eur})
        return {"warm": len(rows), "target": self.settings.warm, "minutes": self.settings.warm_minutes,
                "machines": rows}
