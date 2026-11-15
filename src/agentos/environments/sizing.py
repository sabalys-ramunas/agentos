"""An environment's size, life and cost: the VM a plan rents, the node's limits (open at once, the
longest life, the month's budget) and what the month has cost so far. A mixin of EnvironmentManager."""

from __future__ import annotations

from datetime import datetime

from agentos.core.errors import AgentosError, InvalidRequest, NotFound
from agentos.environments import machine
from agentos.environments.errors import (
    EnvironmentLimit,
)
from agentos.environments.models import (
    SIZES,
    WORKSPACE,
    Environment,
)


class Sizing:
    """Sizes, hours and money, for EnvironmentManager (it reads self.settings, self.store and self.vms())."""

    def _size(self, size: str | None) -> tuple[str | None, dict | None]:
        """`size`: small, medium, large or xlarge (SIZES: the cheapest server type with at least
        that much, or on the docker provider a container with those limits), a vms.toml profile
        name, or a server type. None: the environments' profile, else the VM default (the cheapest
        that fits)."""
        if not size:
            return self.settings.profile, None
        size = str(size).strip().lower()
        vms = self.vms()
        if size in SIZES:
            self._check_size(size)
            if getattr(vms.provider(), "name", "") == "docker":
                return self.settings.profile, {"server_type": f"docker-{size}"}
            cpu, memory = SIZES[size]
            return self.settings.profile, {"min_cpu": cpu, "min_memory_gb": memory}
        if size in vms.settings.profiles:
            return size, None
        return self.settings.profile, {"server_type": size}

    def _check_size(self, size: str) -> None:
        names = list(SIZES)
        top = self.settings.max_size
        if names.index(size) > names.index(top):
            raise EnvironmentLimit(f"the biggest environment on this node is {top} ({self._size_words(top)}), so "
                                   f"{size} is past the limit. Ask for {top} or less, or tell the user: the owner "
                                   f"raises it with `agentos environments allow --max-size {size}`")

    def _size_names(self) -> str:
        """small, medium, large or xlarge: the sizes an agent may ask for here (up to max_size)."""
        names = list(SIZES)[:list(SIZES).index(self.settings.max_size) + 1]
        return names[0] if len(names) == 1 else f"{', '.join(names[:-1])} or {names[-1]}"

    async def _check_server_type(self, name: str) -> None:
        """A size that is no size and no profile must be a server type the provider rents, checked
        before anything is planned: an unknown one (a typo like "xxlarge") is invalid, not a VM error.
        When the provider's list cannot be read, the plan itself says what is wrong."""
        try:
            types = [str(t.get("name")) for t in await self.vms().provider().server_types()]
        except AgentosError:
            return
        if types and name not in types:
            example = next((t for t in types if not t.startswith("docker")), types[0])
            raise InvalidRequest(f"size {name!r} is not one this node knows. size is one of {self._size_names()}, "
                                 f"or a server type the provider rents, like {example}; leave it out for the "
                                 "default. Nothing was rented")

    @staticmethod
    def _size_words(size: str) -> str:
        cpu, memory = SIZES[size]
        return f"{cpu} CPU, {memory} GB"

    def _check_planned(self, plan: dict) -> None:
        """A server type or a profile asked for by name is bound by max_size too, by what it has."""
        specs = plan.get("specs") or {}
        top = self.settings.max_size
        cpu, memory = SIZES[top]
        if (specs.get("cores") or 0) > cpu or (specs.get("memory_gb") or 0) > memory:
            need = next((n for n, (c, m) in SIZES.items()
                         if c >= (specs.get("cores") or 0) and m >= (specs.get("memory_gb") or 0) - 0.5), None)
            more = (f"or tell the user: the owner raises the limit with `agentos environments allow --max-size {need}`"
                    if need else f"no environment may be bigger than {list(SIZES)[-1]}")
            raise EnvironmentLimit(f"{plan['server_type']} has {specs.get('cores')} CPU and {specs.get('memory_gb'):g} "
                                   f"GB, more than this node's biggest environment, {top} ({self._size_words(top)}). "
                                   f"Ask for {top} or a smaller server type; {more}. Nothing was rented")

    @staticmethod
    def size_of(cpu: float | None, memory_gb: float | None) -> str | None:
        """The biggest of SIZES a machine with this much has (None: less than small, or unknown)."""
        got = None
        for name, (c, m) in SIZES.items():
            if (cpu or 0) >= c and (memory_gb or 0) >= m - 0.5:  # a few hundred MB short still counts
                got = name
        return got

    async def bigger_sizes(self, env: Environment) -> list[dict]:
        """The sizes above the one `env` has, up to max_size, with what each would rent here and its
        price: what an agent reads before it asks for a bigger one. [] when none, or the provider
        does not answer."""
        names = list(SIZES)
        top = names.index(self.settings.max_size)
        got = env.machine.size
        start = names.index(got) + 1 if got in names else 0
        out = []
        for name in names[start:top + 1]:
            cpu, memory = SIZES[name]
            if (env.machine.cpu or 0) >= cpu and (env.machine.memory_gb or 0) >= memory:
                continue
            try:
                profile, custom = self._size(name)
                plan = await self.vms().plan(f"{WORKSPACE}/{env.id}", profile=profile, ttl_minutes=60, size=custom)
            except AgentosError:
                continue
            specs = plan.get("specs") or {}
            out.append({"size": name, "server_type": plan["server_type"], "cpu": specs.get("cores"),
                        "memory_gb": specs.get("memory_gb"), "price_hourly_eur": plan.get("price_hourly_eur")})
        return out

    def _hours(self, hours: float | None) -> int:
        h = float(hours if hours is not None else self.settings.default_hours)
        if h <= 0:
            raise InvalidRequest("hours is how long the environment may live, more than 0")
        cap = self._longest()
        minutes = max(5, round(h * 60))
        if minutes > cap:
            raise EnvironmentLimit(f"an environment lives at most {cap / 60:g} hours on this node, so {h:g} hours "
                                   f"is past the limit. Ask for fewer hours; {self._longest_raise()}")
        return minutes

    def _longest(self) -> float:
        """The longest life of an environment here, in minutes: max_hours, and the VMs' max_ttl_minutes."""
        return min(self.settings.max_hours * 60, self.vms().settings.max_ttl_minutes)

    def _longest_raise(self) -> str:
        """Which setting holds the longest life, and how the owner raises it."""
        if self.settings.max_hours * 60 <= self.vms().settings.max_ttl_minutes:
            return "the owner raises it with `agentos environments allow --max-hours H`"
        return "the owner raises it with max_ttl_minutes in vms.toml (the longest life of any VM on this node)"

    async def _image(self) -> str:
        if self.settings.image != "auto":
            return self.settings.image
        try:
            await self.vms().provider().resolve_image(machine.SNAPSHOT_IMAGE)
            return machine.SNAPSHOT_IMAGE
        except AgentosError:
            return machine.BASE_IMAGE

    def month_spend(self, now: datetime | None = None) -> float:
        now = now or self._clock()
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        total = 0.0
        for env in self.store.recent(1000):
            if env.closed_at is not None and env.closed_at < start:
                continue
            total += self.cost_now(env) or 0.0
        return round(total, 4)

    def booked_eur(self, now: datetime | None = None, *, until: dict[str, datetime] | None = None) -> float:
        """This month's spend on environments with every open one counted to its time limit (or to
        `until[id]`): what they can still cost if none closes early. The budget is checked against it."""
        now = now or self._clock()
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        total = 0.0
        for env in self.store.recent(1000):
            if env.closed_at is not None and env.closed_at < start:
                continue
            spent = self.cost_now(env) or 0.0
            if env.open and env.price_hourly_eur:
                end = (until or {}).get(env.id, env.expires_at)
                hours = max(1, -(-int(max(0.0, (end - env.created_at).total_seconds())) // 3600))
                spent = max(spent, hours * env.price_hourly_eur)
            total += spent
        return round(total, 4)

    def cost_now(self, env: Environment) -> float | None:
        if env.cost_eur is not None and not env.open:
            return env.cost_eur
        if not env.vm_id:
            return 0.0 if not env.open else None
        try:
            return self.vms().store.get(env.vm_id).cost_eur(self._clock())
        except (NotFound, AgentosError):
            return env.cost_eur

    async def plan(self, env_id: str, *, size: str | None, hours: float | None) -> dict:
        """What open would rent, for the limits and the record of the open (agentos.vms plan, with
        the image the environment boots)."""
        profile, custom = self._size(size)
        minutes = self._hours(hours)
        if custom and "server_type" in custom and size and str(size).strip().lower() not in SIZES:
            await self._check_server_type(custom["server_type"])
        try:
            plan = await self.vms().plan(f"{WORKSPACE}/{env_id}", profile=profile, ttl_minutes=minutes, size=custom)
        except AgentosError as e:
            if e.code != "vm_error":
                raise
            raise type(e)(f"{e.message.rstrip('.')}. Nothing was rented") from None  # only planned, never made
        if size and str(size).strip().lower() not in SIZES:
            self._check_planned(plan)  # a server type or a profile, by what it has (a named size: by its name)
        plan["image"] = await self._image()
        return plan

    def check_limits(self, plan: dict) -> None:
        vms = self.vms()
        live = len(self.store.open())
        cap = self.settings.max_live if self.settings.max_live is not None else vms.settings.max_live
        if live >= cap:
            raise EnvironmentLimit(f"this node already has {live} open environment{'s' if live != 1 else ''}, and "
                                   f"its limit is {cap} at once. Close one you no longer need with environment_close, "
                                   "or tell the user: the owner raises the limit with "
                                   "`agentos environments allow --max-live N`")
        budget = self.settings.monthly_eur
        price = plan.get("price_hourly_eur")
        if budget and price is not None:
            worst = max([price, *[q or 0 for _n, q in plan.get("fallbacks") or []]]) * -(-plan["ttl_minutes"] // 60)
            booked = self.booked_eur()
            if booked + worst > budget:
                raise EnvironmentLimit(f"this environment could cost up to EUR {worst:.2f}; with EUR {booked:.2f} "
                                       "spent and booked by open environments this month, that passes the monthly "
                                       f"budget of EUR {budget:.2f}. Close one, or ask for fewer hours; the owner "
                                       "raises the budget with `agentos environments allow --monthly-eur E`")
