"""The owner's view of the node's own machines, for the app: the prepared machines and what they cost,
the warm pool with its price and the most it can cost, what this month's opens started from, and the
machine behind each save. A mixin of EnvironmentManager (its settings, store, saves(), vms(), clock,
prepared_store(), pool_view(), warm_vms(), prepared_view() and _scrub())."""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
from datetime import datetime
from typing import Any

from openajar.core import clock

MONTH_HOURS = 730.0        # an average month (365 * 24 / 12)
RECENT_OPENS = 40
PRICE_TTL_S = 300.0        # the pool's price is asked of the provider at most this often
PRICE_WAIT_S = 10.0
SERVICE_CMD_MAX = 200


def worst_eur_month(target: int, warm_minutes: int, price_hourly_eur: float | None) -> float | None:
    """The most the warm pool costs a month when no open ever claims a machine.

    Each of the `target` slots holds one warm machine at a time; the reaper deletes it after
    `warm_minutes` and the next chat or open rents another, so at worst a slot is never empty. The node
    books a machine by the hours it started (openajar.vms.models.VmRecord.billed_hours: Hetzner bills each
    started hour, and warm_minutes under 60 keeps each machine to one), so:

        worst = target * (MONTH_HOURS * 60 / warm_minutes) * ceil(warm_minutes / 60) * price_hourly_eur

    Docker's machines cost 0. None when the price is unknown."""
    if price_hourly_eur is None:
        return None
    if target <= 0 or warm_minutes <= 0:
        return 0.0
    lives = MONTH_HOURS * 60 / warm_minutes
    return round(target * lives * math.ceil(warm_minutes / 60) * price_hourly_eur, 2)


class Fleet:
    """For EnvironmentManager: GET /api/orgs/{org}/environments/prepared and the saves' machines."""

    async def prepared_overview(self) -> dict:
        """{machines, eur_month, eur_per_gb_month, pool, month} (docs/API.md, "Prepared machines")."""
        from openajar.environments.prepared import row

        now = self._clock()
        rate = self.settings.snapshot_eur_per_gb_month
        recs = sorted(self.prepared_store().all(), key=lambda r: r.started_at, reverse=True)
        pool = self.pool_view()
        price = await self.pool_price_hourly(pool)
        pool.update(price_hourly_eur=price, worst_eur_month=worst_eur_month(pool["target"], pool["minutes"], price),
                    month_eur=self.pool_month_eur(now))
        return {"machines": [row(r, rate, now) for r in recs], "eur_month": self.prepared_eur_month(),
                "eur_per_gb_month": rate, "pool": pool, "month": self.month_opens(now)}

    async def pool_price_hourly(self, pool: dict) -> float | None:
        """What one warm machine bills an hour: a live one's own price, else the price of what the pool
        would rent (the default size, as fill_pool plans it). None when the provider cannot say."""
        live = [m["price_hourly_eur"] for m in pool.get("machines") or [] if m.get("price_hourly_eur") is not None]
        if live:
            return float(live[0])
        cached = self.__dict__.get("_pool_price")
        if cached is not None and time.monotonic() - cached[0] < PRICE_TTL_S:
            return cached[1]
        price = None
        with contextlib.suppress(Exception):  # the page without the price rather than none at all
            price = await asyncio.wait_for(self._pool_plan_price(), timeout=PRICE_WAIT_S)
        self.__dict__["_pool_price"] = (time.monotonic(), price)
        return price

    async def _pool_plan_price(self) -> float | None:
        from openajar.environments.models import WORKSPACE
        from openajar.environments.warmpool import WARM_PREFIX

        profile, custom = self._size(None)
        plan = await self.vms().plan(f"{WORKSPACE}/{WARM_PREFIX}price", profile=profile, size=custom,
                                     ttl_minutes=self.settings.warm_minutes, image=await self._warm_image())
        price = plan.get("price_hourly_eur")
        return float(price) if price is not None else None

    def pool_month_eur(self, now: datetime) -> float:
        """This month's warm machines that no open took, what they cost so far (one an open claimed is
        the environment's, and counts there)."""
        from openajar.environments.warmpool import is_warm

        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        total = 0.0
        with contextlib.suppress(Exception):
            total = sum(r.cost_eur(now) or 0.0 for r in self.vms().store.since(start) if is_warm(r))
        return round(total, 4)

    def month_opens(self, now: datetime) -> dict:
        """The environments opened this calendar month: how many, how many booted a prepared machine or
        took a warm one, the setup seconds the prepared ones saved, and the last RECENT_OPENS that became
        ready (oldest first, `seconds` from the open to ready)."""
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        envs = [e for e in self.store.recent(1000) if e.created_at >= start]
        prepared = [e for e in envs if (e.prepared or {}).get("image")]
        saved = 0.0
        for e in prepared:
            with contextlib.suppress(Exception):
                saved += ((self.prepared_view(e).get("prepared") or {}).get("setup_s") or {}).get("saved") or 0.0
        ready = sorted((e for e in envs if e.ready_at is not None), key=lambda e: e.created_at)[-RECENT_OPENS:]
        return {"opens": len(envs), "from_prepared": len(prepared), "from_warm": sum(1 for e in envs if e.warm_claimed),
                "setup_saved_s": round(saved, 1),
                "recent": [{"at": clock.iso_or_none(e.created_at, z=True),
                            "seconds": round(max(0.0, (e.ready_at - e.created_at).total_seconds()), 1),
                            "from": _opened_from(e)} for e in ready]}

    # ================================================================ the saves' machines
    def saved_rows(self, visible: Any = None) -> list[dict]:
        """saved_for's rows, each with the machine it wakes on: env_id, provider, server_type,
        price_hourly_eur (from its environment's record while the node keeps it) and the services it
        starts again."""
        rows = self.saved_for(None, visible=visible)
        if not rows:
            return rows
        manifests = {m.id: m for m in self.saves().all()}
        for r in rows:
            m = manifests.get(r["id"])
            if m is not None:
                r.update(self.save_machine(m))
        return rows

    def save_machine(self, m: Any) -> dict:
        env = None
        with contextlib.suppress(Exception):
            env = self.store.get(m.env_id) if self.store.has(m.env_id) else None
        held = m.paused or m.snapshot
        provider = (held.provider if held is not None else "") or (env.machine.provider if env is not None else "")
        server_type = m.server_type or (held.server_type if held is not None else "")
        return {"env_id": m.env_id, "provider": provider or None, "server_type": server_type or None,
                "price_hourly_eur": env.price_hourly_eur if env is not None else None,
                "services": [{"cmd": self._scrub(s.cmd, max_len=SERVICE_CMD_MAX), "port": s.port} for s in m.services]}


def _opened_from(env: Any) -> str:
    if env.warm_claimed:
        return "warm"
    return "prepared" if (env.prepared or {}).get("image") else "cold"
