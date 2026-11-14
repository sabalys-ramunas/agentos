"""The controller ceiling: what the node's owner lets a controller do on this node.

Set in node.toml, never from the controller (docs/CONTROLLER.md, "The controller ceiling"):

- `controller_max_effect`: the highest effect on the ladder a grant from the controller's org
  may use here (read, draft, commit, pay, irreversible);
- `controller_specials`: which specials such a grant may use (secret, exec, install_overlay,
  install_base, service, brain, memory, vm: every special a grant can carry, core.models.SPECIALS). It is
  an allowlist: a special not listed, including one added to the grants later, is refused;
- `controller_streams`: what the controller may open: `view` (watch a lane's live view, and read
  its screenshot, an approval's preview and a run's proof as `file` streams), `control` (take a
  lane over and type into a live view or a terminal), `terminal` (attach to a terminal lane's
  shell; typing into it also needs control, and `exec` in `controller_specials`, since typing
  runs commands) and `logs` (read the node's own log lines, redacted
  on the node; off by default, because logs are where a secret is likeliest to slip out).

It applies to everything that comes from the org issuer or the controller: a grant presented on
`invoke` is clamped to it (the policy service sets `Grant.limit`, and a call above it is refused
with the reason `controller_ceiling`), `lane_control` `take` needs control, and `stream_open`
needs its kind. A controller's `approval_decision` may approve only an action within the ceiling
(denying is never limited). A node's own grants are not affected.

The default is the hosted one: effects up to commit, no specials, live view to watch only. A
self-hosted controller on the node's own box (`agentos up`) writes the full ceiling when it joins
the node, and `agentos node join --self-hosted` does the same for the owner's own controller
elsewhere. `controller_ceiling_by` records who wrote it, since node.toml is saved whole on any
change and the keys' presence says nothing (`upgradable`). The node reports its ceiling in `hello` so a panel can show it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentos.core.ceiling import (
    DEFAULT_MAX_EFFECT,
    DEFAULT_SPECIALS,
    DEFAULT_STREAMS,
    STREAMS,
)
from agentos.core.models import SPECIALS, Effect
from agentos.policy.errors import Denied

REASON = "controller_ceiling"


@dataclass(frozen=True)
class ControllerCeiling:
    max_effect: Effect = Effect.COMMIT
    specials: frozenset[str] = field(default_factory=frozenset)
    streams: frozenset[str] = field(default_factory=lambda: frozenset(DEFAULT_STREAMS))

    @classmethod
    def from_config(cls, config: Any) -> ControllerCeiling:
        """From node.toml (a NodeConfig); the hosted default when there is none."""
        if config is None:
            return cls()
        return cls(Effect(getattr(config, "controller_max_effect", DEFAULT_MAX_EFFECT)),
                   frozenset(getattr(config, "controller_specials", DEFAULT_SPECIALS)),
                   frozenset(getattr(config, "controller_streams", DEFAULT_STREAMS)))

    @classmethod
    def full(cls) -> ControllerCeiling:
        """Everything: for the owner's own controller."""
        return cls(Effect.IRREVERSIBLE, frozenset(SPECIALS), frozenset(STREAMS))

    def settings(self, by: str | None = None) -> dict[str, Any]:
        """As node.toml settings; with `by`, also who set it (controller_ceiling_by)."""
        report = self.report()
        out = {f"controller_{name}": report[name] for name in ("max_effect", "specials", "streams")}
        return out if by is None else {**out, "controller_ceiling_by": by}

    @classmethod
    def upgradable(cls, config: Any) -> bool:
        """Whether `agentos up` may write the full ceiling over node.toml's: only when nobody chose it.
        That is a join to someone else's controller, or no marker and still the hosted default (a
        node from before the ceiling); a ceiling the owner wrote by hand, or lowered after `up`
        wrote it, stays."""
        by = getattr(config, "controller_ceiling_by", "default")
        return by == "join" or (by == "default" and cls.from_config(config) == cls())

    def report(self) -> dict[str, Any]:
        """As sent in `hello.ceiling`."""
        return {"max_effect": self.max_effect.value, "specials": [s for s in SPECIALS if s in self.specials],
                "streams": [s for s in STREAMS if s in self.streams]}

    # ---- grants ----
    def allows_effect(self, effect: Effect) -> bool:
        if effect.rank is not None:
            return effect.rank <= (self.max_effect.rank or 0)
        return effect.value in self.specials  # an allowlist: a special it does not name is refused

    def allows_special(self, name: str) -> bool:
        return name in self.specials

    def check_effect(self, effect: Effect) -> None:
        if not self.allows_effect(effect):
            if effect.rank is not None:
                raise Denied(REASON, f"this node's owner lets a controller go up to {self.max_effect.value}, "
                                     f"not {effect.value}; it is raised in node.toml on the node")
            raise Denied(REASON, f"this node's owner does not let a controller use {effect.value}; it is "
                                 "allowed in node.toml on the node")

    def check_approval(self, effect: str | None) -> None:
        """A controller may approve only an action it could have taken itself: one whose effect is
        within the ceiling. Above it, the owner decides on the node (panel, push, CLI). Denying is
        never limited, and a question has no effect."""
        try:
            eff = Effect(effect) if effect is not None else None
        except ValueError:
            eff = None
        if eff is None or not self.allows_effect(eff):
            raise Denied(REASON, f"this node's owner does not let a controller approve "
                                 f"{effect or 'this action'}; decide it on the node (its panel, a push or "
                                 "agentos approvals)")

    # ---- streams and lane control ----
    def stream_needs(self, kind: str, mode: str) -> set[str]:
        """What opening a stream of `kind` in `mode` needs from `controller_streams`."""
        if kind == "terminal":
            return {"terminal", "control"} if mode == "control" else {"terminal"}
        if kind == "vnc":
            return {"control"} if mode == "control" else {"view"}
        if kind == "file":
            return {"view"}
        if kind == "logs":
            return {"logs"}
        return set()

    def check_stream(self, kind: str, mode: str) -> None:
        if kind == "terminal" and mode == "control" and "exec" not in self.specials:
            # typing into a shell runs commands, so it needs exec as the MCP shell tools do
            raise Denied(REASON, "this node's owner does not let a controller type into a terminal "
                                 "(controller_specials needs exec in node.toml on the node)")
        missing = sorted(self.stream_needs(kind, mode) - self.streams)
        if missing:
            if kind in ("terminal", "vnc"):
                what = ("a terminal" if kind == "terminal" else "the live view") + f" in {mode} mode"
            else:
                what = "the node's logs" if kind == "logs" else "a lane's screenshot, preview or proof"
            raise Denied(REASON, f"this node's owner does not let a controller open {what} "
                                 f"(controller_streams needs {', '.join(missing)} in node.toml on the node)")

    def check_take(self) -> None:
        if "control" not in self.streams:
            raise Denied(REASON, "this node's owner does not let a controller take a lane over "
                                 "(controller_streams needs control in node.toml on the node)")
