"""Which environment a call means, for EnvironmentManager (its store, clock and settings).

A title names one computer of the agent's grant family. A call without `on` goes, on a client that
sends an MCP session id, to the one that session opened or attached last (`bound`), else to the
family's only open one (`pick`); with several and no session it is which_environment. `named` finds
the one an `on` means (its title, id or env/<id>). environment_open attaches by title (`reattach`):
without a session it hands the open one back as it is, a session takes it to itself (from another
session only once that one has been quiet for QUIET).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from agentos.core.errors import InvalidRequest
from agentos.environments.errors import (
    EnvironmentAlreadyOpen,
    EnvironmentInUse,
    NoEnvironment,
    WhichEnvironment,
)
from agentos.environments.models import (
    HANDOFFS_KEPT,
    ID_RE,
    LIVE_STATES,
    QUIET,
    SIZES,
    WORKSPACE,
    Environment,
    Handoff,
)

log = logging.getLogger("agentos.environments")

OPENS_LISTED = 10


def _when(dt: datetime) -> str:
    """A time in a message for an agent: 2026-11-13 16:47 UTC."""
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def is_session(binding: str) -> bool:
    """Was the call made on a client that sends an MCP session id (one chat)? A 2026-07-28 client
    sends none, nor does stdio: every chat of the agent then binds to its grant."""
    return binding.startswith("session:")


def same_title(a: str, b: str) -> bool:
    return " ".join(a.split()).lower() == " ".join(b.split()).lower()


def current_client() -> str | None:
    """The MCP client of the call in flight ("claude-code 2.1.3", from its clientInfo), when it said."""
    from agentos.core.tools import current_elicitor

    e = current_elicitor()
    client = getattr(e, "client", None) if e is not None else None
    return str(client)[:80] if client else None


class Picking:
    """For EnvironmentManager (its store, clock, settings, check_keep and sizing)."""

    def bound(self, binding: str, family: str | None = None) -> Environment | None:
        """The open environment bound to this chat; with `family`, only one of that grant family (a
        session id another agent sends gets nothing of someone else's)."""
        env = self.store.bound(binding)
        if env is not None and family is not None and env.family != family:
            return None
        return env

    def open_for_family(self, family: str | None) -> list[Environment]:
        """The open environments of a grant family (None: every one, for the owner with no policy)."""
        return [e for e in self.store.open() if family is None or e.family == family]

    def named(self, ref: str, family: str | None) -> Environment | None:
        """The open environment of this family that `ref` names: its id, its lane env/<id> (or
        env/<id>-browser), or its title (any case). None when it names none. Lane ids of accounts
        always hold a "/" (workspace/name), so a ref without one is never a lane; a title with a "/"
        is named by its id."""
        ref = " ".join(str(ref or "").split())
        if not ref:
            return None
        mine = self.open_for_family(family)
        eid = ref.split("/", 1)[1].removesuffix("-browser") if ref.startswith(WORKSPACE + "/") else ref
        if ID_RE.match(eid):
            hit = [e for e in mine if e.id == eid]
            if hit:
                return hit[0]
        if "/" in ref:
            return None
        titled = [e for e in mine if same_title(e.title, ref)]
        live = [e for e in titled if e.state in LIVE_STATES] or titled
        if len(live) > 1:
            raise self.which(live, f"the title \"{ref}\" names {len(live)} open environments")
        return live[0] if live else None

    @staticmethod
    def on_words(env: Environment) -> str:
        """How an agent names it in a call: "Shop page" (on="env/e2abc...")."""
        return f"\"{env.title}\" (on=\"{env.terminal_lane}\")"

    def which(self, envs: list[Environment], why: str | None = None) -> WhichEnvironment:
        listed = ", ".join(self.on_words(e) + (f", {self._repo_words(e.repo)}" if e.repo else "")
                           for e in envs[:OPENS_LISTED])
        why = why or (f"{len(envs)} environments of your agent are open, and this client sends no session id, "
                      "so a call without `on` could go to any of them")
        return WhichEnvironment(f"{why}: {listed}. Call again with on set to the one you opened or attached "
                                "(its title works too). Never act on one another chat of yours works in; "
                                "environment_open(title=...) with a new title opens your own")

    def pick(self, binding: str, family: str | None, kind: str | None = None) -> Environment:
        """The environment a call without `on` goes to. A session (a client that sends an MCP session
        id): the one it opened or attached last, as before. No session: the family's only open one;
        with several, which_environment. With none, no_environment."""
        env = self.pick_or_none(binding, family)
        if env is None:
            raise self.no_environment(binding, family, kind)
        if env.state == "closing":
            raise NoEnvironment(f"environment {env.id} is closing; open a new one with environment_open(title=...)")
        return env

    def pick_or_none(self, binding: str, family: str | None) -> Environment | None:
        """pick, with None for no environment (which_environment still raises)."""
        if is_session(binding):
            return self.bound(binding, family)
        mine = [e for e in self.open_for_family(family) if not e.binding.startswith("kept:")]  # kept: let go
        live = [e for e in mine if e.state in LIVE_STATES]
        if len(live) > 1:
            raise self.which(live)
        return live[0] if live else (mine[0] if len(mine) == 1 else None)

    @staticmethod
    def _last_call_words(env: Environment) -> str:
        return f"last call {_when(env.last_call_at)}" if env.last_call_at is not None else "no call yet"

    def no_environment(self, binding: str, family: str | None, kind: str | None = None) -> NoEnvironment:
        """What a call without `on` hears in a chat with no environment: that another chat took it
        over, or the agent's open ones (a reconnected chat takes its own back by title). A browser
        call also hears that an account lane needs `on`."""
        mine = self.open_for_family(family)
        quiet = int(QUIET.total_seconds() // 60)
        lanes = (" A browser call meant for a logged-in account lane the human made needs `on` "
                 "(on=\"personal/reddit\"; lanes_list lists them).") if kind == "browser" else ""
        for env in mine:
            gone = [h for h in env.handoffs if h.binding == binding]
            if gone and env.binding != binding:
                return NoEnvironment(
                    f"another chat of this agent took environment {env.id} (\"{env.title}\") over at "
                    f"{_when(gone[-1].at)}, after this chat had made no call for {quiet} minutes. "
                    f"environment_open(title=\"{env.title}\") takes it back once that chat has been quiet as "
                    "long; or open a new one with environment_open(title=...)." + lanes)
        if mine:
            listed = "; ".join(f"{e.id} \"{e.title}\" ({e.repo + ', ' if e.repo else ''}{self._last_call_words(e)})"
                               for e in mine[:OPENS_LISTED])
            return NoEnvironment(f"this chat has no environment. Open ones of this agent: {listed}. "
                                 "environment_open(title=...) with one of these titles takes yours back (after a "
                                 "reconnect), a new title opens a new one; or name a lane with `on`."
                                 + lanes)
        if kind == "browser":
            return NoEnvironment("this chat has no environment, and a browser call without `on` goes to one. To "
                                 "build and test, open one first with environment_open(title=...)." + lanes)
        return NoEnvironment("this chat has no environment. Open one first with environment_open(title=...), "
                             "or name a lane with `on`")

    def resolve(self, binding: str, kind: str, family: str | None = None) -> str:
        """The lane a call without `on` goes to: the picked environment's terminal or browser lane."""
        env = self.pick(binding, family, kind)
        return env.browser_lane if kind == "browser" else env.terminal_lane

    async def check_open_args(self, *, size: str | None, hours: float | None, keep: str | None) -> None:
        """environment_open's own argument errors, before anything about what is open: an unknown keep,
        keep "live" the owner has not allowed, a size past the node's biggest or one it does not know,
        too many hours. The most useful error wins: a size past the limit is never hidden by an attach."""
        if keep is not None and (keep != "live" or not self.settings.allow_keep_live):
            self.check_keep(keep)  # its value; "live" only for the owner's opt-in (the count is the open's)
        if size:
            name = str(size).strip().lower()
            if name in SIZES:
                self._check_size(name)
            elif name not in self.vms().settings.profiles:
                await self._check_server_type(name)
        if hours is not None and keep != "live":
            self._hours(hours)

    def live_bound(self, binding: str, family: str | None) -> Environment | None:
        """The environment this chat has that is alive (starting or ready), else None: never one that
        is closing, failed or closed."""
        env = self.bound(binding, family)
        return env if env is not None and env.state in LIVE_STATES else None

    @staticmethod
    def _repo_words(repo: str | None) -> str:
        return repo if repo else "no repository"

    async def reattach(self, binding: str, title: str, family: str | None, repo: str | None = None
                       ) -> Environment | None:
        """The live environment of this grant family with this title (starting or ready, never one
        closing, failed or closed), for environment_open to attach to. With no `repo`, the one with this
        title (whatever it cloned); with a `repo`, only one with that repository: a title held by one
        with another repository is environment_already_open (a second environment with the same title
        would make the next attach by title a guess). No session (a client that sends no session id):
        it is handed back as it is, and other chats of the family keep reaching it. A session binds it
        to itself (its calls without `on` go there); from another session only once that one has made
        no call for QUIET (10 minutes), else environment_in_use, with its last call."""
        env = self._titled(title, family, repo)
        if env is None:
            return env
        now = self._clock()
        if not is_session(binding):
            if env.binding.startswith("kept:"):  # a kept one this agent let go: its chats reach it again
                return self.store.update(env.id, lambda e: setattr(e, "binding", binding))
            return env
        if env.binding == binding:
            return self.store.update(env.id, lambda e: setattr(e, "bound_at", now))
        last = env.last_activity()
        if now - last < QUIET and is_session(env.binding):  # one no session holds (kept, or no session): at once
            raise EnvironmentInUse(
                f"environment {env.id} (\"{env.title}\") is in use by another chat of this agent (its last "
                f"call {_when(last)}); it moves to this chat only after {int(QUIET.total_seconds() // 60)} "
                "minutes with no call there. Open your own with another title: environment_open(title=...)")
        return self._take_over(env, binding, now)

    def _titled(self, title: str, family: str | None, repo: str | None) -> Environment | None:
        """The live environment of this family with this title (and `repo`, when one is named)."""
        found = [e for e in self.open_for_family(family) if same_title(e.title, title) and e.state in LIVE_STATES]
        if not found:
            return None
        if repo is None:
            if len(found) == 1:
                return found[0]
            listed = ", ".join(f"{e.id} ({self._repo_words(e.repo)})" for e in found[:OPENS_LISTED])
            raise InvalidRequest(f"the title \"{title}\" names {len(found)} open environments: {listed}. Name the "
                                 "one you want with its repo: environment_open(title=..., repo=...)")
        same = [e for e in found if (e.repo or None) == repo]
        if same:
            return same[0]
        env = found[0]
        raise EnvironmentAlreadyOpen(
            f"the title \"{env.title}\" names environment {env.id}, which is open with "
            f"{self._repo_words(env.repo)}, and you asked for {repo}. A title names one computer, so nothing "
            f"was opened. For {repo}, call environment_open with another title. To work in this one, call "
            f"environment_open(title=\"{env.title}\") without repo")

    def _take_over(self, env: Environment, binding: str, now: datetime) -> Environment:
        """Move `env` to this chat. The move counts as a call, so the chat it came from cannot take it
        straight back (it is quiet for QUIET only after this chat is)."""
        moved_from = env.binding

        def take(e: Environment) -> bool:
            if e.binding != moved_from or not e.open:
                return False  # it moved, or closed, meanwhile
            e.binding = binding
            e.bound_at = now
            e.last_call_at = now
            e.handoffs.append(Handoff(binding=moved_from, at=now))
            del e.handoffs[:-HANDOFFS_KEPT]
            return True

        env = self.store.update(env.id, take)
        if env.binding != binding:
            raise EnvironmentInUse(f"environment {env.id} (\"{env.title}\") just moved to another chat; "
                                   "open your own with another title: environment_open(title=...)")
        log.info("environment %s taken over by %s from %s", env.id, binding, moved_from)
        return env

    def resolve_or_none(self, binding: str, family: str | None = None) -> Environment | None:
        return self.pick_or_none(binding, family)
