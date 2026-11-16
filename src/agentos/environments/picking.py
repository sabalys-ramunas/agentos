"""Which environment a call means, for EnvironmentManager (its store, clock and settings).

One chat holds a live machine: the owner's rule of 2026-11-16 ("only one agent chat should have
access to the same machine at the same time, even if it is the same project"). environment_open
gives the calling chat the machine's handle, `on` ("<id>.<16 random letters and digits>"); only
calls that carry it reach the machine (`reach`). The node keeps only its sha256 (`Environment.lease`),
so no record, listing or log can show it. A client that sends an MCP session id holds its machines by
the session too (`bound`), so its calls without `on` go to the one it opened last, as before. A client
that sends none (MCP 2026-07-28, stdio) cannot be told apart from the agent's other chats: its calls
without `on` are refused with which_environment, even with one machine open.

A title no longer reaches a live machine: environment_open with the title of one another chat holds is
environment_in_use. Once that chat has made no call for take_over_minutes (10), environment_open(title,
take_over=true) moves it to the caller with a fresh handle, and the old one stops working (`give`). A
machine kept live and let go (binding "kept:<id>") is nobody's: its title gets it back at once.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
import secrets
import string
from datetime import UTC, datetime, timedelta

from agentos.core.errors import InvalidRequest, NotFound
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
    SIZES,
    WORKSPACE,
    Environment,
    Handoff,
)

log = logging.getLogger("agentos.environments")

OPENS_LISTED = 10
HANDLE_SECRET = 16  # letters and digits after the id: about 82 bits
HANDLE_RE = re.compile(r"^(e[a-z0-9]{3,15})\.([a-z0-9]{" + str(HANDLE_SECRET) + "})$")
_SECRET_CHARS = string.ascii_lowercase + string.digits
ON_FIX = "pass the on from your environment_open"


def _when(dt: datetime) -> str:
    """A time in a message for an agent: 2026-11-13 16:47 UTC."""
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def new_handle(env_id: str) -> str:
    """A fresh handle for a machine: its id, a dot and a secret. Given once, to the chat that holds it."""
    return f"{env_id}.{''.join(secrets.choice(_SECRET_CHARS) for _ in range(HANDLE_SECRET))}"


def lease_of(handle: str) -> str:
    """What the node keeps of a handle: its sha256, never the handle."""
    return hashlib.sha256(handle.encode()).hexdigest()


def handle_env(ref: str | None) -> str | None:
    """The environment id a handle names, or None when `ref` is not shaped as one."""
    m = HANDLE_RE.match(" ".join(str(ref or "").split()))
    return m.group(1) if m else None


def is_session(binding: str) -> bool:
    """Was the call made on a client that sends an MCP session id (one chat)? A 2026-07-28 client
    sends none, nor does stdio: every chat of the agent then binds to its grant."""
    return binding.startswith("session:")


def is_let_go(env: Environment) -> bool:
    """A keep "live" machine its chat let go (environment_close): nobody holds it."""
    return env.binding.startswith("kept:")


def same_title(a: str, b: str) -> bool:
    return " ".join(a.split()).lower() == " ".join(b.split()).lower()


def current_client() -> str | None:
    """The MCP client of the call in flight ("claude-code 2.1.3", from its clientInfo), when it said."""
    from agentos.core.tools import current_elicitor

    e = current_elicitor()
    client = getattr(e, "client", None) if e is not None else None
    return str(client)[:80] if client else None


def _minutes(td: timedelta) -> int:
    return max(0, int(td.total_seconds() // 60))


class Picking:
    """For EnvironmentManager (its store, clock, settings, check_keep and sizing)."""

    def take_over_after(self) -> timedelta:
        """How long the holder must have made no call before another chat may take its machine over."""
        return timedelta(minutes=self.settings.take_over_minutes)

    def bound(self, binding: str, family: str | None = None) -> Environment | None:
        """The open environment bound to this chat's session; with `family`, only one of that grant
        family (a session id another agent sends gets nothing of someone else's)."""
        env = self.store.bound(binding)
        if env is not None and family is not None and env.family != family:
            return None
        return env

    def open_for_family(self, family: str | None) -> list[Environment]:
        """The open environments of a grant family (None: every one, for the owner with no policy)."""
        return [e for e in self.store.open() if family is None or e.family == family]

    @staticmethod
    def holds(env: Environment, binding: str, on: str | None = None) -> bool:
        """Does this caller hold the machine: it passed the handle, or its session is the one bound?"""
        if on and env.lease and lease_of(" ".join(on.split())) == env.lease:
            return True
        return is_session(binding) and env.binding == binding

    # ---------------------------------------------------------------- reaching one with `on`
    def reach(self, ref: str | None, binding: str, family: str | None) -> Environment | None:
        """The environment of this family that `ref` names, when this caller holds it. A handle names
        its machine (closed too, for what it cost). An id or a lane env/<id> names an open one only for
        the session that holds it; a title never reaches a live one. Raises no_environment for a handle
        that is not the machine's now, which_environment for a live machine named any other way. None:
        `ref` names no environment (an account lane "workspace/name", or nothing)."""
        ref = " ".join(str(ref or "").split())
        if not ref:
            return None
        if (eid := handle_env(ref)) is not None:
            return self._by_handle(eid, ref, family)
        mine = self.open_for_family(family)
        eid = ref.split("/", 1)[1].removesuffix("-browser") if ref.startswith(WORKSPACE + "/") else ref
        if ID_RE.match(eid):
            hit = [e for e in mine if e.id == eid]
            if hit:
                if self.holds(hit[0], binding):
                    return hit[0]
                raise WhichEnvironment(f"{ref} names environment {eid} by its id, and only the on its "
                                       f"environment_open gave reaches it: {ON_FIX}. "
                                       + self._own_words(hit[0]))
        if "/" in ref:
            return None
        titled = [e for e in mine if same_title(e.title, ref)]
        if titled:
            raise WhichEnvironment(f"\"{ref}\" is the title of a live machine, and a title does not reach one: "
                                   f"{ON_FIX}. " + self._own_words(titled[0]))
        return None

    @staticmethod
    def _own_words(env: Environment) -> str:
        return (f"If it is not yours, environment_open(title=\"{env.title} 2\") opens your own; "
                "environment() lists your agent's machines")

    def _by_handle(self, eid: str, handle: str, family: str | None) -> Environment:
        try:
            env = self.store.get(eid)
        except (NotFound, ValueError):
            env = None
        gone = NoEnvironment("this on is not the handle of a machine of yours (it may have closed). environment() "
                             "lists your agent's machines; environment_open(title=...) opens your own")
        if env is None or (family is not None and env.family != family):
            raise gone
        lease = lease_of(handle)
        if env.lease == lease:
            return env
        took = [h for h in env.handoffs if h.lease == lease]
        if took and env.open:
            raise NoEnvironment(
                f"another chat of your agent took environment {env.id} (\"{env.title}\") over at {_when(took[-1].at)}, "
                f"after this chat had made no call for {self.settings.take_over_minutes} minutes, so this on no "
                "longer reaches it. Open your own with environment_open(title=...); your work on it stays there "
                "for that chat")
        if is_let_go(env) and env.open:
            raise NoEnvironment(f"environment {env.id} (\"{env.title}\") was let go (keep live), so this on no longer "
                                f"reaches it: environment_open(title=\"{env.title}\") takes it back")
        raise gone

    # ---------------------------------------------------------------- calls without `on`
    def pick(self, binding: str, family: str | None, kind: str | None = None) -> Environment:
        """The environment a call without `on` goes to: the session's own (a client that sends an MCP
        session id). No session: which_environment while the agent has a machine open (another
        chat's could be the one picked), else no_environment."""
        env = self.pick_or_none(binding, family, kind)
        if env is None:
            raise self.no_environment(binding, family, kind)
        if env.state == "closing":
            raise NoEnvironment(f"environment {env.id} is closing; open a new one with environment_open(title=...)")
        return env

    def pick_or_none(self, binding: str, family: str | None, kind: str | None = None) -> Environment | None:
        """pick, with None for no environment (which_environment still raises)."""
        if is_session(binding):
            return self.bound(binding, family)
        if [e for e in self.open_for_family(family) if not is_let_go(e)]:
            raise self.needs_on(kind)
        return None

    @staticmethod
    def needs_on(kind: str | None = None) -> WhichEnvironment:
        lanes = (" A logged-in account lane the human made always needs `on` too (on=\"personal/reddit\"; "
                 "lanes_list lists them).") if kind == "browser" else ""
        return WhichEnvironment(
            "this client sends no session id, so agentos cannot tell your chats apart, and a call without `on` "
            "never goes to a machine. Keep the on your environment_open gave and pass it on every call: "
            "on=\"<that on>\". No machine in this chat yet, or lost it? environment_open(title=...) with a new "
            "title opens your own." + lanes)

    def no_environment(self, binding: str, family: str | None, kind: str | None = None) -> NoEnvironment:
        """What a call without `on` hears in a chat with no environment: that another chat took it
        over, or the titles of the agent's machines in other chats (never a way into them). A browser
        call also hears that an account lane needs `on`."""
        mine = self.open_for_family(family)
        lanes = (" A browser call meant for a logged-in account lane the human made needs `on` "
                 "(on=\"personal/reddit\"; lanes_list lists them).") if kind == "browser" else ""
        for env in mine:
            gone = [h for h in env.handoffs if h.binding == binding]
            if gone and env.binding != binding:
                return NoEnvironment(
                    f"another chat of this agent took environment {env.id} (\"{env.title}\") over at "
                    f"{_when(gone[-1].at)}, after this chat had made no call for {self.settings.take_over_minutes} "
                    "minutes. Open your own with environment_open(title=...)." + lanes)
        if mine:
            listed = ", ".join(f"\"{e.title}\"" + (f" ({e.repo})" if e.repo else "") for e in mine[:OPENS_LISTED])
            return NoEnvironment(f"this chat has no environment. Open your own with environment_open(title=...) and "
                                 f"a new title. Your agent's other chats hold {listed}; only the chat that holds a "
                                 "machine reaches it." + lanes)
        if kind == "browser":
            return NoEnvironment("this chat has no environment, and a browser call without `on` goes to one. To "
                                 "build and test, open one first with environment_open(title=...)." + lanes)
        return NoEnvironment("this chat has no environment. Open one first with environment_open(title=...), "
                             "or name a lane with `on`")

    def resolve(self, binding: str, kind: str, family: str | None = None) -> str:
        """The lane a call without `on` goes to: the picked environment's terminal or browser lane."""
        env = self.pick(binding, family, kind)
        return env.browser_lane if kind == "browser" else env.terminal_lane

    def resolve_or_none(self, binding: str, family: str | None = None) -> Environment | None:
        return self.pick_or_none(binding, family)

    def live_bound(self, binding: str, family: str | None) -> Environment | None:
        """The environment this chat's session has that is alive (starting or ready), else None."""
        env = self.bound(binding, family)
        return env if env is not None and env.state in LIVE_STATES else None

    # ---------------------------------------------------------------- environment_open
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

    @staticmethod
    def _repo_words(repo: str | None) -> str:
        return repo if repo else "no repository"

    def use_words(self, env: Environment, now: datetime) -> str:
        """Who has a machine, for a list shown to a chat that does not hold it (never its handle)."""
        if is_let_go(env):
            return f"let go: environment_open(title=\"{env.title}\") takes it"
        quiet = now - env.last_activity()
        if quiet >= self.take_over_after():
            return (f"quiet for {_minutes(quiet)} min: environment_open(title=\"{env.title}\", take_over=true) "
                    "takes it over")
        return f"in use by another chat (last call {_minutes(quiet)} min ago)"

    def in_use(self, env: Environment, now: datetime) -> EnvironmentInUse:
        """environment_open of a title another chat holds: refused, with how to open one's own, and
        when take_over=true would work."""
        last = env.last_activity()
        quiet, limit = now - last, self.take_over_after()
        words = (f"\"{env.title}\" is in use by another chat of your agent (environment {env.id}, its last call "
                 f"{_when(last)}, {_minutes(quiet)} min ago). Only the chat that holds a machine reaches it, so "
                 f"nothing was opened. Open your own with another title: environment_open(title=\"{env.title} 2\").")
        if quiet >= limit:
            words += (f" It has been quiet for {_minutes(quiet)} min: if that chat is done with it, "
                      f"environment_open(title=\"{env.title}\", take_over=true) takes it over with its work on it.")
        else:
            left = max(1, math.ceil((limit - quiet).total_seconds() / 60))
            words += (f" If that chat is gone, environment_open(title=\"{env.title}\", take_over=true) takes it over "
                      f"once it has made no call for {_minutes(limit)} min ({left} min from now).")
        return EnvironmentInUse(words)

    def reattach(self, binding: str, title: str, family: str | None, repo: str | None = None, *,
                 take_over: bool = False) -> tuple[Environment, str] | None:
        """The live environment of this family with this title (starting or ready) that environment_open
        may give this chat, and how ("attached" or "taken_over"); None: no live one has the title. A
        session gets back its own; a machine let go is anybody's; one another chat holds is
        environment_in_use, unless take_over and its holder has made no call for take_over_minutes.
        Nothing changes here: `give` moves it once the call is authorized."""
        env = self._titled(title, family, repo)
        if env is None:
            return None
        if is_let_go(env) or (is_session(binding) and env.binding == binding):
            return env, "attached"
        now = self._clock()
        if not take_over or now - env.last_activity() < self.take_over_after():
            raise self.in_use(env, now)
        return env, "taken_over"

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
            f"was opened. For {repo}, open one with another title: environment_open(title=\"{env.title} 2\", "
            f"repo=\"{repo}\")")

    def give(self, env: Environment, binding: str, *, take: bool = False) -> tuple[Environment, str]:
        """Lease `env` to this chat: a fresh handle (returned; the node keeps its hash) and its session
        binding. Every handle given before stops working. `take`: from another chat, which is told so
        on its next call; the move counts as a call, so that chat cannot take it straight back."""
        handle, now = new_handle(env.id), self._clock()
        was_binding, was_lease = env.binding, env.lease

        def lease(e: Environment) -> bool:
            if e.binding != was_binding or e.lease != was_lease or not e.open:
                return False  # it moved, or closed, meanwhile
            if take:
                e.handoffs.append(Handoff(binding=was_binding, at=now, lease=was_lease))
                del e.handoffs[:-HANDOFFS_KEPT]
                e.last_call_at = now
            e.binding, e.bound_at, e.lease = binding, now, lease_of(handle)
            return True

        got = self.store.update(env.id, lease)
        if got.lease != lease_of(handle):
            raise EnvironmentInUse(f"environment {env.id} (\"{env.title}\") just moved to another chat; open your own "
                                   f"with another title: environment_open(title=\"{env.title} 2\")")
        if take:
            log.info("environment %s taken over by %s from %s", env.id, binding, was_binding)
        return got, handle
